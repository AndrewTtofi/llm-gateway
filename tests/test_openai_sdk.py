"""The official openai SDK, pointed at the gateway, must work unchanged.

The SDK talks to the app in-process (httpx2 ASGITransport — openai 3.x is built on
httpx2); the gateway's upstream call is mocked with respx (httpx). Two different
HTTP libraries, so the mocks can't interfere.
"""

import json
from collections.abc import AsyncIterator

import httpx
import httpx2
import openai
import pytest
import respx

from app import main
from app.config import Registry
from tests.conftest import UPSTREAM
from tests.test_chat import COMPLETION, chunk, sse

URL = f"{UPSTREAM}/chat/completions"
MSGS: list[openai.types.chat.ChatCompletionMessageParam] = [{"role": "user", "content": "hi"}]


@pytest.fixture
async def sdk(registry: Registry, api_key: str) -> AsyncIterator[openai.AsyncOpenAI]:
    transport = httpx2.ASGITransport(app=main.app)
    async with httpx2.AsyncClient(transport=transport, base_url="http://gw") as http:
        yield openai.AsyncOpenAI(
            base_url="http://gw/v1", api_key=api_key, http_client=http, max_retries=0
        )


@respx.mock
async def test_sdk_non_streaming(sdk: openai.AsyncOpenAI) -> None:
    respx.post(URL).mock(return_value=httpx.Response(200, json=COMPLETION))
    resp = await sdk.chat.completions.create(model="local", messages=MSGS)
    assert resp.choices[0].message.content == "hello"
    assert resp.usage is not None and resp.usage.total_tokens == 4


@respx.mock
async def test_sdk_streaming(sdk: openai.AsyncOpenAI) -> None:
    usage = {
        **chunk(),
        "choices": [],
        "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
    }
    route = respx.post(URL).mock(
        return_value=httpx.Response(
            200, content=sse(chunk("hel"), chunk("lo"), chunk(finish="stop"), usage, "[DONE]")
        )
    )
    stream = await sdk.chat.completions.create(
        model="local", messages=MSGS, stream=True, stream_options={"include_usage": True}
    )
    text, last_usage = "", None
    async for c in stream:
        if c.choices:
            text += c.choices[0].delta.content or ""
        last_usage = c.usage or last_usage
    assert text == "hello"
    assert last_usage is not None and last_usage.total_tokens == 5
    assert json.loads(route.calls.last.request.content)["stream_options"] == {"include_usage": True}


async def test_sdk_raises_typed_errors(sdk: openai.AsyncOpenAI) -> None:
    with pytest.raises(openai.NotFoundError) as exc:
        await sdk.chat.completions.create(model="no-such-alias", messages=MSGS)
    assert "no-such-alias" in exc.value.message


@respx.mock
async def test_sdk_sees_mid_stream_errors(sdk: openai.AsyncOpenAI) -> None:
    respx.post(URL).mock(
        return_value=httpx.Response(200, content=sse(chunk("a"), {"error": {"message": "boom"}}))
    )
    stream = await sdk.chat.completions.create(model="local", messages=MSGS, stream=True)
    with pytest.raises(openai.APIError, match="failed mid-stream"):
        async for _ in stream:
            pass
