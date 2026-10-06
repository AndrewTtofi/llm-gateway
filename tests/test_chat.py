import json
from typing import Any

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from app import providers
from app.config import Registry
from app.providers import AdapterPool
from tests.conftest import UPSTREAM

URL = f"{UPSTREAM}/chat/completions"
MSGS = [{"role": "user", "content": "hi"}]

COMPLETION = {
    "id": "chatcmpl-1",
    "object": "chat.completion",
    "created": 1,
    "model": "tiny",
    "choices": [
        {
            "index": 0,
            "message": {"role": "assistant", "content": "hello"},
            "finish_reason": "stop",
        }
    ],
    "usage": {"prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4},
}


def chunk(content: str | None = None, finish: str | None = None) -> dict[str, Any]:
    delta = {"content": content} if content is not None else {}
    return {
        "id": "chatcmpl-1",
        "object": "chat.completion.chunk",
        "created": 1,
        "model": "tiny",
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
    }


def sse(*events: dict[str, Any] | str) -> bytes:
    out = ""
    for e in events:
        out += f"data: {e if isinstance(e, str) else json.dumps(e)}\n\n"
    return out.encode()


def sent_body(route: respx.Route) -> dict[str, Any]:
    body: dict[str, Any] = json.loads(route.calls.last.request.content)
    return body


# --- non-streaming ---------------------------------------------------------


@respx.mock
def test_forwards_and_returns_completion(client: TestClient) -> None:
    route = respx.post(URL).mock(return_value=httpx.Response(200, json=COMPLETION))
    resp = client.post(
        "/v1/chat/completions", json={"model": "local", "messages": MSGS, "temperature": 0.2}
    )
    assert resp.status_code == 200
    assert resp.json() == COMPLETION
    assert resp.headers["x-gateway-provider"] == "mock/tiny"
    body = sent_body(route)
    assert body["model"] == "tiny"  # alias swapped for the real model
    assert body["temperature"] == 0.2  # unknown params pass through
    assert body["stream"] is False


@respx.mock
def test_api_key_from_env_is_sent(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MOCK_API_KEY", "sk-test")
    route = respx.post(URL).mock(return_value=httpx.Response(200, json=COMPLETION))
    client.post("/v1/chat/completions", json={"model": "local", "messages": MSGS})
    assert route.calls.last.request.headers["authorization"] == "Bearer sk-test"


@respx.mock
def test_auth_header_only_for_providers_with_a_key(
    client: TestClient, registry: Registry, monkeypatch: pytest.MonkeyPatch
) -> None:
    route = respx.post(URL).mock(return_value=httpx.Response(200, json=COMPLETION))
    client.post("/v1/chat/completions", json={"model": "local", "messages": MSGS})
    assert route.calls.last.request.headers["authorization"] == "Bearer mock-test-key"
    # api_key_env: null (e.g. Ollama): no auth at all
    monkeypatch.setitem(registry.providers["mock"], "api_key_env", None)
    monkeypatch.setattr(providers, "pool", AdapterPool())
    client.post("/v1/chat/completions", json={"model": "local", "messages": MSGS})
    assert "authorization" not in route.calls.last.request.headers


def test_unknown_model_is_404(client: TestClient) -> None:
    resp = client.post("/v1/chat/completions", json={"model": "nope", "messages": MSGS})
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "model_not_found"


def test_unsupported_provider_type_is_501(client: TestClient) -> None:
    resp = client.post("/v1/chat/completions", json={"model": "unsupported", "messages": MSGS})
    assert resp.status_code == 501
    assert resp.json()["error"]["code"] == "provider_not_supported"


def test_invalid_body_uses_openai_error_shape(client: TestClient) -> None:
    resp = client.post("/v1/chat/completions", json={"model": "local"})
    assert resp.status_code == 400
    err = resp.json()["error"]
    assert err["type"] == "invalid_request_error"
    assert err["param"] == "messages"


SECRETISH = "Incorrect API key provided: sk-proj-****abcd. Organization org-SECRET42."


@pytest.mark.parametrize(
    ("upstream_status", "upstream_code", "client_status", "code"),
    [
        (400, None, 400, "upstream_rejected"),  # client's fault (bad params) → pass through
        (422, None, 422, "upstream_rejected"),
        (401, None, 502, "upstream_error"),  # our provider key is wrong → gateway's fault
        (403, None, 502, "upstream_error"),
        (404, None, 502, "upstream_error"),  # configured upstream model is wrong → ours
        (429, None, 429, "upstream_rate_limited"),
        (429, "insufficient_quota", 503, "upstream_quota_exhausted"),  # retrying won't help
        (503, None, 502, "upstream_error"),
    ],
)
@respx.mock
def test_upstream_errors_are_mapped(
    client: TestClient,
    upstream_status: int,
    upstream_code: str | None,
    client_status: int,
    code: str,
) -> None:
    respx.post(URL).mock(
        return_value=httpx.Response(
            upstream_status,
            json={"error": {"message": SECRETISH, "code": upstream_code}},
            headers={"retry-after": "7"},
        )
    )
    resp = client.post("/v1/chat/completions", json={"model": "local", "messages": MSGS})
    assert resp.status_code == client_status
    err = resp.json()["error"]
    assert err["code"] == code
    if upstream_status in (400, 422):  # describes the client's own input → useful to them
        assert SECRETISH in err["message"]
    else:  # provider text can carry key fragments, org IDs, internal hosts
        assert "sk-proj" not in resp.text and "org-SECRET42" not in resp.text
    assert ("retry-after" in resp.headers) == (client_status == 429)


@respx.mock
def test_non_json_success_body_is_502(client: TestClient) -> None:
    respx.post(URL).mock(return_value=httpx.Response(200, text="<html>proxy login</html>"))
    resp = client.post("/v1/chat/completions", json={"model": "local", "messages": MSGS})
    assert resp.status_code == 502
    assert "proxy login" not in resp.text
    assert resp.json()["error"]["message"] == "mock returned an invalid response"


@respx.mock
def test_error_with_null_message_does_not_crash(client: TestClient) -> None:
    respx.post(URL).mock(return_value=httpx.Response(500, json={"error": {"message": None}}))
    resp = client.post("/v1/chat/completions", json={"model": "local", "messages": MSGS})
    assert resp.status_code == 502


@respx.mock
def test_non_json_error_body_is_not_echoed(client: TestClient) -> None:
    respx.post(URL).mock(return_value=httpx.Response(502, text="dial tcp 10.0.3.7:11434"))
    resp = client.post("/v1/chat/completions", json={"model": "local", "messages": MSGS})
    assert resp.status_code == 502
    assert "10.0.3.7" not in resp.text


@respx.mock
def test_upstream_timeout_is_504(client: TestClient) -> None:
    respx.post(URL).mock(side_effect=httpx.ReadTimeout("slow"))
    resp = client.post("/v1/chat/completions", json={"model": "local", "messages": MSGS})
    assert resp.status_code == 504
    assert resp.json()["error"]["code"] == "upstream_timeout"


@respx.mock
def test_upstream_unreachable_is_502(client: TestClient) -> None:
    respx.post(URL).mock(side_effect=httpx.ConnectError("refused"))
    resp = client.post("/v1/chat/completions", json={"model": "local", "messages": MSGS})
    assert resp.status_code == 502


# --- streaming -------------------------------------------------------------


def stream_lines(client: TestClient, payload: dict[str, Any]) -> tuple[int, list[str]]:
    with client.stream("POST", "/v1/chat/completions", json=payload) as resp:
        return resp.status_code, [ln for ln in resp.iter_lines() if ln]


@respx.mock
def test_stream_relays_chunks_then_done(client: TestClient) -> None:
    route = respx.post(URL).mock(
        return_value=httpx.Response(
            200,
            content=sse(chunk("hel"), chunk("lo"), chunk(finish="stop"), "[DONE]"),
            headers={"content-type": "text/event-stream"},
        )
    )
    status, lines = stream_lines(client, {"model": "local", "messages": MSGS, "stream": True})
    assert status == 200
    assert lines[-1] == "data: [DONE]"
    deltas = [json.loads(ln[6:])["choices"][0]["delta"] for ln in lines[:-1]]
    assert "".join(d.get("content", "") for d in deltas) == "hello"
    assert sent_body(route)["stream"] is True


@respx.mock
def test_stream_ignores_sse_comments_and_keepalives(client: TestClient) -> None:
    raw = b": keep-alive\n\n" + sse(chunk("a")) + b"event: ping\n\n" + sse("[DONE]")
    respx.post(URL).mock(return_value=httpx.Response(200, content=raw))
    _, lines = stream_lines(client, {"model": "local", "messages": MSGS, "stream": True})
    assert len(lines) == 2 and lines[-1] == "data: [DONE]"


@respx.mock
def test_stream_error_before_first_chunk_is_a_real_http_error(client: TestClient) -> None:
    respx.post(URL).mock(return_value=httpx.Response(503, json={"error": {"message": "down"}}))
    resp = client.post(
        "/v1/chat/completions", json={"model": "local", "messages": MSGS, "stream": True}
    )
    assert resp.status_code == 502  # not a 200 with an error hidden in the stream
    assert resp.headers["content-type"].startswith("application/json")


@respx.mock
def test_stream_error_mid_stream_is_sent_in_band(client: TestClient) -> None:
    raw = sse(chunk("par"), {"error": {"message": "overloaded"}})
    respx.post(URL).mock(return_value=httpx.Response(200, content=raw))
    status, lines = stream_lines(client, {"model": "local", "messages": MSGS, "stream": True})
    assert status == 200  # headers were already sent
    assert json.loads(lines[-1][6:])["error"]["message"] == "mock failed mid-stream"
    assert "overloaded" not in lines[-1]  # provider text stays server-side
    assert "data: [DONE]" not in lines  # an errored stream must not look complete


@respx.mock
def test_stream_malformed_first_line_is_502(client: TestClient) -> None:
    respx.post(URL).mock(return_value=httpx.Response(200, content=b"data: {not json\n\n"))
    resp = client.post(
        "/v1/chat/completions", json={"model": "local", "messages": MSGS, "stream": True}
    )
    assert resp.status_code == 502


@respx.mock
def test_stream_malformed_line_mid_stream_is_sent_in_band(client: TestClient) -> None:
    raw = sse(chunk("a")) + b"data: {not json\n\n"
    respx.post(URL).mock(return_value=httpx.Response(200, content=raw))
    status, lines = stream_lines(client, {"model": "local", "messages": MSGS, "stream": True})
    assert status == 200
    assert "invalid response" in json.loads(lines[-1][6:])["error"]["message"]
    assert "data: [DONE]" not in lines


@respx.mock
def test_stream_with_no_chunks_still_completes(client: TestClient) -> None:
    respx.post(URL).mock(return_value=httpx.Response(200, content=sse("[DONE]")))
    _, lines = stream_lines(client, {"model": "local", "messages": MSGS, "stream": True})
    assert lines == ["data: [DONE]"]


@respx.mock
def test_stream_without_done_is_reported_as_truncated(client: TestClient) -> None:
    respx.post(URL).mock(return_value=httpx.Response(200, content=sse(chunk("par"))))
    status, lines = stream_lines(client, {"model": "local", "messages": MSGS, "stream": True})
    assert status == 200
    assert "ended early" in json.loads(lines[-1][6:])["error"]["message"]
    assert "data: [DONE]" not in lines


@respx.mock
def test_provider_without_its_key_falls_back_without_a_call(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("MOCK_API_KEY")
    route = respx.post(f"{UPSTREAM}/chat/completions").mock(
        return_value=httpx.Response(200, json=COMPLETION)
    )
    resp = client.post("/v1/chat/completions", json={"model": "mock-then-ok", "messages": MSGS})
    assert resp.status_code == 200 and resp.headers["x-gateway-provider"] == "chaos/ok"
    assert not route.called  # no request sent with missing credentials
