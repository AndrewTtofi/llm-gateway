"""Gateway entrypoint: routes. The pipeline for a chat request is

authenticate → model allowed? → budget left? → rate limits (estimate) →
route (retries, fallback, breakers) → reconcile tokens + add cost

Two client formats share it: OpenAI chat completions (`/v1/chat/completions`, also the
internal format) and Anthropic Messages (`/v1/messages`, translated at the edge, ADR 0010).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import secrets
import signal
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated, Any, Literal

import anyio
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response, StreamingResponse
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest, start_http_server
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app import config, extensions, messages_api, providers, services
from app.auth import ApiKey, EffectiveLimits
from app.errors import error_response, routing_error_response
from app.metering import Meter
from app.observability import live, metrics
from app.observability import logging as obs_log
from app.providers.base import ProviderAdapter
from app.ratelimit import estimate_prompt_tokens
from app.routing import policy, router
from app.routing.router import AllTargetsFailed, Routed, UnknownModel
from app.schemas import ChatCompletionRequest, StreamOptions

# Re-exported: tests and tools import these from app.main.
from app.streaming import (  # noqa: F401
    ClientDisconnected,
    SSEResponse,
    StreamFormat,
    cancel_on_disconnect,
    relay_sse,
)

log = logging.getLogger(__name__)


def reload_from_signal() -> None:
    try:
        reg = config.reload_registry()
        log.warning("config reloaded on SIGHUP: %d aliases", len(reg.aliases))
    except Exception:
        log.exception("config reload on SIGHUP failed; keeping the previous config")


async def poll_breakers(interval: float = 15.0) -> None:
    """Keep gateway_circuit_state current (the state lives in Redis, shared). Targets
    removed by a config reload are dropped, so they don't linger as "open"."""
    known: set[str] = set()
    while True:
        try:
            targets = {t for a in config.registry.aliases.values() for t in a.chain}
            for gone in known - targets:
                with contextlib.suppress(KeyError):
                    metrics.breaker.remove(gone)
            known = targets
            for t in targets:
                with contextlib.suppress(Exception):
                    state = await router.store.state(t)
                    metrics.breaker.labels(t).set(metrics.BREAKER_VALUE.get(str(state), 0))
        except Exception:
            log.exception("breaker poll failed")
        await asyncio.sleep(interval)


async def measure_loop_lag(interval: float = 0.5) -> None:
    """A timer that should fire every `interval`; how late it fires is how long the event
    loop couldn't run anything — blocking calls or CPU saturation show up here first."""
    loop = asyncio.get_running_loop()
    while True:
        start = loop.time()
        await asyncio.sleep(interval)
        metrics.loop_lag.observe(max(0.0, loop.time() - start - interval))


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    obs_log.configure(config.settings.log_level)
    await services.start()
    poller = asyncio.create_task(poll_breakers())
    lag = asyncio.create_task(measure_loop_lag())
    metrics_server = None
    if config.settings.metrics_port:
        # Separate port: /metrics can be firewalled off while the API stays public.
        metrics_server, _metrics_thread = start_http_server(
            config.settings.metrics_port, registry=metrics.registry
        )
    loop = asyncio.get_running_loop()
    with contextlib.suppress(NotImplementedError, RuntimeError, ValueError):
        loop.add_signal_handler(signal.SIGHUP, reload_from_signal)  # `kill -HUP <pid>`
    yield
    for task in (poller, lag):
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
    if metrics_server is not None:
        metrics_server.shutdown()
    with contextlib.suppress(NotImplementedError, RuntimeError, ValueError):
        loop.remove_signal_handler(signal.SIGHUP)
    await providers.pool.aclose()
    await services.stop()


app = FastAPI(title="LLM Gateway", version="1.1.0", lifespan=lifespan)


class RequestContext:
    """Pure ASGI middleware: request id (in logs, usage rows and `x-request-id`) and one
    access line per HTTP request. Pure ASGI rather than BaseHTTPMiddleware, which wraps
    `receive` and would break disconnect detection on streams."""

    def __init__(self, inner: Any) -> None:
        self.inner = inner

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.inner(scope, receive, send)
            return
        headers = dict(scope.get("headers") or [])
        incoming = headers.get(b"x-request-id", b"").decode("latin-1")
        rid = obs_log.new_request_id(incoming)
        token = obs_log.request_id.set(rid)
        start, status = time.perf_counter(), 0
        started_token = obs_log.request_started.set(start)

        async def send_with_id(message: Any) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                message.setdefault("headers", []).append((b"x-request-id", rid.encode()))
            await send(message)

        try:
            await self.inner(scope, receive, send_with_id)
        except BaseException:
            status = status or 500  # crashed before a response went out
            raise
        finally:
            if not str(scope.get("path", "")).startswith(("/metrics", "/healthz", "/readyz")):
                obs_log.access.info(
                    "http",
                    method=scope.get("method"),
                    path=scope.get("path"),
                    status=status,
                    ms=round((time.perf_counter() - start) * 1000),
                )
            obs_log.request_id.reset(token)
            obs_log.request_started.reset(started_token)


class BodyLimit:
    """Pure ASGI: refuse request bodies over `max_body_bytes` with a 413, before any
    handler reads them. A declared Content-Length is checked up front; a chunked body
    (no length) is read up to the limit and replayed to the app."""

    def __init__(self, inner: Any) -> None:
        self.inner = inner

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] != "http" or scope.get("method") in ("GET", "HEAD", "OPTIONS"):
            await self.inner(scope, receive, send)
            return
        limit = config.settings.max_body_bytes
        headers = dict(scope.get("headers") or [])
        declared = headers.get(b"content-length")
        if declared is not None:
            try:
                too_big = int(declared) > limit
            except ValueError:
                too_big = False  # the server rejects malformed lengths itself
            if too_big:
                await self._reject(scope, send, limit)
                return
            await self.inner(scope, receive, send)
            return
        body, size = [], 0
        disconnected: dict[str, Any] | None = None
        while True:
            message = await receive()
            if message["type"] != "http.request":
                disconnected = message  # the client went away mid-upload
                break
            size += len(message.get("body", b""))
            if size > limit:
                await self._reject(scope, send, limit)
                return
            body.append(message.get("body", b""))
            if not message.get("more_body"):
                break
        replayed = False

        async def replay() -> Any:
            nonlocal replayed
            if disconnected is not None:
                return disconnected  # never hand the app a truncated body as complete
            if not replayed:
                replayed = True
                return {"type": "http.request", "body": b"".join(body), "more_body": False}
            return await receive()  # disconnects still reach the app

        await self.inner(scope, replay, send)

    @staticmethod
    async def _reject(scope: Any, send: Any, limit: int) -> None:
        message = f"Request body is larger than {limit} bytes."
        path = str(scope.get("path", ""))
        if path == "/v1/messages" or path.startswith("/v1/messages/"):
            resp = messages_api.error_response(413, message)
        else:
            resp = error_response(413, message, code="request_too_large")
        await resp(scope, _no_receive, send)


async def _no_receive() -> dict[str, Any]:
    return {"type": "http.disconnect"}


app.add_middleware(BodyLimit)  # inside RequestContext: rejections still get a request id
app.add_middleware(RequestContext)


@app.get("/metrics", include_in_schema=False)
async def metrics_endpoint() -> Response:
    """Only when metrics_port is 0 (single-port setups, tests). Otherwise metrics are on
    their own port and this is a 404, so the public API doesn't expose them."""
    if config.settings.metrics_port:
        return Response(status_code=404)
    return Response(generate_latest(metrics.registry), media_type=CONTENT_TYPE_LATEST)


def _anthropic_client(request: Request) -> bool:
    path = request.url.path
    return path == "/v1/messages" or path.startswith("/v1/messages/")


@app.exception_handler(HTTPException)
async def http_error(request: Request, exc: HTTPException) -> JSONResponse:
    detail = exc.detail if isinstance(exc.detail, dict) else {"message": str(exc.detail)}
    if _anthropic_client(request):  # e.g. a bad key: the Anthropic SDK's error shape
        return messages_api.error_response(
            exc.status_code, str(detail.get("message")), headers=exc.headers
        )
    return error_response(
        exc.status_code,
        str(detail.get("message")),
        str(detail.get("type", "invalid_request_error")),
        detail.get("code", exc.status_code),
        headers=exc.headers,
    )


@app.exception_handler(RequestValidationError)
async def validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
    first = exc.errors()[0] if exc.errors() else {}
    param = ".".join(str(p) for p in first.get("loc", ()) if p != "body") or None
    message = f"Invalid request: {first.get('msg', 'bad body')}"
    if _anthropic_client(request):
        return messages_api.error_response(400, f"{param}: {message}" if param else message)
    return error_response(400, message, param=param)


# --- auth --------------------------------------------------------------------


def _bearer(authorization: str) -> str:
    scheme, _, token = authorization.partition(" ")
    return token.strip() if scheme.lower() == "bearer" else ""


def require_admin(authorization: Annotated[str, Header()] = "") -> None:
    key = config.settings.gateway_admin_key
    # Compare bytes: compare_digest raises TypeError on non-ASCII str (header bytes ≥ 0x80).
    presented = authorization.encode("utf-8", "surrogateescape")
    if not key or not secrets.compare_digest(presented, f"Bearer {key}".encode()):
        raise HTTPException(status_code=401, detail="unauthorized")


async def require_key(
    authorization: Annotated[str, Header()] = "", x_api_key: Annotated[str, Header()] = ""
) -> ApiKey:
    # OpenAI clients send `Authorization: Bearer`; Anthropic SDKs send `x-api-key`.
    try:
        key = await services.keys.authenticate(_bearer(authorization) or x_api_key.strip())
    except Exception as exc:  # store down and key not cached recently: fail closed
        # Type only: a DB error's message can include bound parameters (the key hash).
        log.error("API key lookup failed: %s", type(exc).__name__)
        raise HTTPException(
            503,
            detail={
                "message": "authentication backend unavailable",
                "type": "api_error",
                "code": "auth_unavailable",
            },
        ) from exc
    if key is None:
        metrics.rejected.labels("unauthenticated").inc()
        raise HTTPException(
            401,
            detail={
                "message": "Incorrect API key provided.",
                "type": "invalid_request_error",
                "code": "invalid_api_key",
            },
        )
    return key


def key_limits(key: ApiKey) -> EffectiveLimits:
    try:
        return key.limits(config.limits)
    except KeyError as exc:
        raise HTTPException(
            403,
            detail={
                "message": f"key tier {key.tier!r} is not configured",
                "type": "permission_error",
                "code": "tier_unknown",
            },
        ) from exc


Authenticated = Annotated[ApiKey, Depends(require_key)]
Admin = Annotated[None, Depends(require_admin)]


# --- public ------------------------------------------------------------------


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    """Liveness: the process is up and its event loop answers."""
    return {"status": "ok"}


@app.get("/readyz", response_model=None)
async def readyz() -> dict[str, Any] | JSONResponse:
    """Readiness: may this instance take traffic? Only its own state decides — not
    shared dependencies. If Redis being down made every replica "not ready", the load
    balancer would drop all of them at once and a degraded gateway (limits fail open,
    cached keys keep working) would become a total outage. Dependencies are reported
    for dashboards and humans."""
    if not services.started:
        return JSONResponse({"status": "starting"}, status_code=503)
    deps = services.dependencies()  # cached; refreshed in the background
    degraded = any(v != "ok" for v in deps.values())
    return {"status": "degraded" if degraded else "ready", "dependencies": deps}


@app.get("/v1/models")
async def list_models(key: Authenticated) -> dict[str, object]:
    """Models this key may call: aliases (with their chain), then direct provider/models."""
    reg, lim = config.registry, key_limits(key)
    data: list[dict[str, object]] = [
        {
            "id": name,
            "object": "model",
            "created": 0,
            "owned_by": "gateway",
            **({"chain": a.chain} if a.policy is None else {"policy": a.policy.model_dump()}),
        }
        for name, a in reg.aliases.items()
        if lim.allows(name)
    ]
    if reg.allow_direct_models:
        direct = sorted(reg.known_targets())  # what `resolve` accepts, like /v1/catalog
        data += [
            {"id": m, "object": "model", "created": 0, "owned_by": m.split("/", 1)[0]}
            for m in direct
            if lim.allows(m)
        ]
    return {"object": "list", "data": data}


# Blended price for sorting: a typical 3:1 mix of input to output tokens.
BLEND_INPUT, BLEND_OUTPUT = 3, 1
SortBy = Literal["name", "price", "quality", "ttft", "latency"]


@app.get("/v1/catalog", response_model=None)
async def model_catalog(
    key: Authenticated,
    capability: Annotated[list[config.Capability], Query()] = [],  # noqa: B006 — FastAPI copies it
    min_context: int = 0,
    sort: SortBy = "name",
) -> dict[str, Any] | JSONResponse:
    """What this key can use, with price, capabilities, quality and live performance, so
    apps can choose a model for their use case (ADR 0011). Live stats are this replica's
    view of the last 15 minutes."""
    lim = key_limits(key)
    verdict = await services.limiter.take(key.id, lim.requests_per_minute, lim.tokens_per_minute, 0)
    if not verdict.allowed:
        metrics.rejected.labels("rate_limit").inc()
        return error_response(
            429,
            "Rate limit reached for this key.",
            "rate_limit_error",
            "rate_limit_exceeded",
            headers=verdict.headers(),
        )
    # One snapshot of the config: a reload during the awaits below mustn't mix versions.
    reg, pricing, catalog = config.registry, config.pricing, config.catalog
    prices, cat = pricing.models, catalog.models
    aliases = {
        n: a.chain if a.policy is None else policy.candidates(n, a.policy)
        for n, a in reg.aliases.items()
        if lim.allows(n)
    }
    policies = {
        n: a.policy.model_dump() for n, a in reg.aliases.items() if a.policy and lim.allows(n)
    }
    targets = dict.fromkeys(t for chain in aliases.values() for t in chain)
    if reg.allow_direct_models:
        targets |= dict.fromkeys(t for t in sorted(reg.known_targets()) if lim.allows(t))

    rows: list[dict[str, Any]] = []
    for t in targets:
        facts = cat.get(t) or config.CatalogEntry()
        if any(c not in facts.capabilities for c in capability):
            continue
        if min_context and (facts.context_window or 0) < min_context:
            continue
        price = prices.get(t)
        row_price = None
        if price is not None and price.input is not None and price.output is not None:
            blended = (BLEND_INPUT * price.input + BLEND_OUTPUT * price.output) / (
                BLEND_INPUT + BLEND_OUTPUT
            )
            row_price = {
                "input": price.input,
                "output": price.output,
                "cached_input": price.cached_input,
                "cache_write": price.cache_write,
                "cache_write_1h": price.cache_write_1h,
                # Long prompts reprice the whole request; off-peak hours are cheaper.
                "tiers": [t.model_dump() for t in price.tiers],
                "off_peak": price.off_peak.model_dump() if price.off_peak else None,
                "blended": round(blended, 6),
                "unit": f"{pricing.currency} per 1M tokens",
            }
        rows.append(
            {
                "id": t,
                "provider": t.partition("/")[0],
                "callable_directly": reg.allow_direct_models and lim.allows(t),
                # False: the provider's API key isn't set, so calls fall through to the next target
                "configured": _configured(
                    t.partition("/")[0], reg.providers.get(t.partition("/")[0])
                ),
                "in_aliases": [n for n, chain in aliases.items() if t in chain],
                "pricing": row_price,
                **facts.model_dump_public(),
                # What the gateway can use, not just what the model can do (ADR 0012).
                "capabilities": policy.usable_capabilities(list(facts.capabilities), t),
                "circuit": "unknown",  # filled in below, all targets at once
                "live": live.snapshot(t),
            }
        )
    for row, circuit in zip(rows, await _circuits([r["id"] for r in rows]), strict=True):
        row["circuit"] = circuit
    rows.sort(key=lambda r: _catalog_sort_key(r, sort))
    return {
        "object": "catalog",
        "prices_checked": pricing.checked,
        "catalog_checked": catalog.checked,
        "blend": f"{BLEND_INPUT}:{BLEND_OUTPUT} input:output tokens",
        "aliases": [
            {
                "id": n,
                **({"policy": policies[n], "candidates": c} if n in policies else {"chain": c}),
            }
            for n, c in aliases.items()
        ],
        "data": rows,
    }


def _configured(name: str, cfg: dict[str, Any] | None) -> bool:
    """Ask the adapter: the router skips exactly what this reports as unconfigured."""
    if cfg is None:
        return False
    try:
        return providers.pool.get(name, cfg).configured
    except providers.UnsupportedProvider:
        return False


async def _circuits(targets: list[str]) -> list[str]:
    """Breaker state per target, read concurrently. "unknown" while the breaker store is
    failing open, because its "closed" would then be a guess."""
    store = router.store
    if getattr(store, "degraded", False):
        return ["unknown"] * len(targets)
    states = await asyncio.gather(*(store.state(t) for t in targets), return_exceptions=True)
    return ["unknown" if isinstance(s, BaseException) else str(s) for s in states]


def _catalog_sort_key(row: dict[str, Any], sort: str) -> tuple[Any, ...]:
    """Unknown values sort last; ties break by id."""
    big = float("inf")
    if sort == "price":
        value: float = row["pricing"]["blended"] if row["pricing"] else big
    elif sort == "quality":
        value = -row["quality"] if row["quality"] is not None else big
    elif sort in ("ttft", "latency"):
        stat = row["live"][f"{sort}_ms"]
        value = stat["p50"] if stat else big
    else:
        value = 0
    return (value, row["id"])


@app.post("/v1/chat/completions", response_model=None)
async def chat_completions(
    body: ChatCompletionRequest, request: Request, key: Authenticated
) -> JSONResponse | StreamingResponse | Response:
    raw = body.model_dump(exclude_unset=True)
    if (clean := extensions.without_thinking(raw)) is not raw:
        body = ChatCompletionRequest.model_validate(clean)
    return await _chat(body, request, key, anthropic_client=False)


@app.post("/v1/messages", response_model=None)
async def messages(
    request: Request, key: Authenticated
) -> JSONResponse | StreamingResponse | Response:
    """Anthropic Messages API: translated to the internal format, routed like any chat
    request (so it can fall back to a non-Anthropic model), translated back (ADR 0010)."""
    body = await _messages_body(request)
    if isinstance(body, JSONResponse):
        return body
    fmt = None
    if body.stream:
        cpt = config.limits.estimation.chars_per_token
        prompt = estimate_prompt_tokens(body.model_dump()["messages"], cpt)
        fmt = messages_api.MessagesStream(input_tokens=prompt, chars_per_token=cpt)
    resp = await _chat(body, request, key, fmt=fmt, anthropic_client=True)
    # Streams are already written as Anthropic events; JSON answers and errors convert here.
    return messages_api.convert_response(resp) if isinstance(resp, JSONResponse) else resp


@app.post("/v1/messages/count_tokens", response_model=None)
async def count_tokens(request: Request, key: Authenticated) -> dict[str, int] | JSONResponse:
    """An *estimate* (the same one rate limits use), not the provider's exact count: the
    gateway doesn't know which provider will serve. Clients use it for context budgeting."""
    raw = await _json_body(request)
    if isinstance(raw, JSONResponse):
        return raw
    try:
        oai = messages_api.to_openai({"max_tokens": 1, **raw})
    except messages_api.InboundError as exc:
        return messages_api.error_response(400, str(exc))
    lim = key_limits(key)
    if not lim.allows(str(oai["model"])):
        metrics.rejected.labels("model_not_allowed").inc()
        return messages_api.error_response(403, f"This key may not use model '{oai['model']}'")
    # Free upstream, but not free here: it counts as a request (no tokens) like any other.
    verdict = await services.limiter.take(key.id, lim.requests_per_minute, lim.tokens_per_minute, 0)
    if not verdict.allowed:
        metrics.rejected.labels("rate_limit").inc()
        return messages_api.error_response(
            429, "Rate limit reached for this key.", headers=verdict.headers()
        )
    cpt = config.limits.estimation.chars_per_token
    tools = len(json.dumps(oai.get("tools") or [])) / cpt
    return {"input_tokens": estimate_prompt_tokens(oai["messages"], cpt) + int(tools)}


async def _json_body(request: Request) -> dict[str, Any] | JSONResponse:
    try:
        raw = await request.json()
    except ValueError:
        return messages_api.error_response(400, "request body is not valid JSON")
    if not isinstance(raw, dict):
        return messages_api.error_response(400, "request body must be a JSON object")
    return raw


async def _messages_body(request: Request) -> ChatCompletionRequest | JSONResponse:
    raw = await _json_body(request)
    if isinstance(raw, JSONResponse):
        return raw
    try:
        return ChatCompletionRequest.model_validate(messages_api.to_openai(raw))
    except messages_api.InboundError as exc:
        # A fixed field name only: the message itself may quote client input.
        log.warning("messages request rejected: unsupported %s", exc.field)
        return messages_api.error_response(400, str(exc))
    except ValidationError as exc:
        err = exc.errors()[0]  # a ValidationError always has at least one
        where = ".".join(str(p) for p in err["loc"])
        return messages_api.error_response(400, f"Invalid request: {where}: {err['msg']}")


async def _chat(
    body: ChatCompletionRequest,
    request: Request,
    key: ApiKey,
    fmt: StreamFormat | None = None,
    anthropic_client: bool = False,
) -> JSONResponse | StreamingResponse | Response:
    """The chat pipeline. `fmt` writes a stream in another wire format (None = OpenAI).
    `anthropic_client`: the caller speaks the Messages API and gets the Anthropic-only
    extension fields (thinking blocks); OpenAI clients never do (ADR 0013)."""
    lim = key_limits(key)
    if not lim.allows(body.model):
        metrics.rejected.labels("model_not_allowed").inc()
        return error_response(
            403,
            f"This key may not use model '{body.model}'",
            "permission_error",
            "model_not_allowed",
            param="model",
        )

    # Budget: checked before the call against month-to-date spend (ADR 0007).
    team_budget = config.limits.teams.get(key.team) if key.team else None
    if (
        team_budget is not None
        and key.team_spend_id
        and await services.spend.spent(key.team_spend_id) >= team_budget.monthly_budget_usd
    ):
        metrics.rejected.labels("team_budget").inc()
        return error_response(
            429,
            "Monthly budget for this key's team is exhausted.",
            "insufficient_quota",
            "insufficient_quota",
        )
    if await services.spend.spent(key.id) >= lim.monthly_budget_usd:
        metrics.rejected.labels("budget").inc()
        return error_response(
            429,
            "Monthly budget for this key is exhausted.",
            "insufficient_quota",
            "insufficient_quota",
        )

    # Rate limits: one request + an estimate of its tokens, from both buckets at once.
    est = config.limits.estimation
    prompt_estimate = estimate_prompt_tokens(body.model_dump()["messages"], est.chars_per_token)
    completion_cap = body.max_completion_tokens or body.max_tokens or est.default_completion_tokens
    estimate = prompt_estimate + int(completion_cap)
    verdict = await services.limiter.take(
        key.id, lim.requests_per_minute, lim.tokens_per_minute, estimate
    )
    rl_headers = verdict.headers()
    if not verdict.allowed:
        metrics.rejected.labels("rate_limit").inc()
        return error_response(
            429,
            "Rate limit reached for this key. Retry after the `retry-after` header.",
            "rate_limit_error",
            "rate_limit_exceeded",
            headers=rl_headers,
        )

    # Anthropic streams always end with usage (message_delta), so that format needs it.
    client_wants_usage = fmt is not None or bool(
        body.stream_options and body.stream_options.include_usage
    )
    meter = Meter(
        key,
        lim,
        services.limiter,
        services.spend,
        estimate,
        prompt_estimate,
        client_wants_usage,
        sink=services.usage,
        alias=body.model,
        streamed=body.stream,
    )
    try:
        chain = config.registry.resolve(body.model)
    except KeyError:
        meter.status, meter.error_code = 404, "model_not_found"
        meter.alias_label = "_unknown"  # never a client-chosen string as a metric label
        await meter.settle()
        return _unknown(body)
    alias = config.registry.aliases.get(body.model)
    if alias is not None and alias.policy is not None:
        # Policy routing (ADR 0017): this request's chain, from the catalog.
        try:
            hints = _route_hints(body, request)
            plan = await policy.plan(
                body.model,
                policy.effective(alias.policy, hints),
                body.model_dump(exclude_unset=True),
                prompt_estimate,
            )
        except (ValueError, TypeError) as exc:
            meter.status, meter.error_code = 400, "invalid_route"
            await meter.settle()
            return error_response(
                400, f"Invalid route hints: {exc}", code="invalid_route", param="route"
            )
        except policy.NoRoute as exc:
            status = 503 if exc.capacity else 400
            meter.status, meter.error_code = status, "no_route"
            await meter.settle()
            return error_response(
                status,
                str(exc),
                "api_error" if exc.capacity else "invalid_request_error",
                "no_route",
            )
        chain = plan.chain
        rl_headers["x-gateway-route"] = plan.header()
    else:
        chain = []  # the router resolves the alias itself
    await meter.reserve(chain[0] if chain else config.registry.resolve(body.model)[0])
    if body.stream:
        # Always ask the provider for usage so streams can be metered (ADR 0007); the
        # meter drops the usage chunk again if the client didn't ask for it. Keep any
        # other stream options the client sent.
        body.stream_options = (body.stream_options or StreamOptions()).model_copy(
            update={"include_usage": True}
        )
    # Server-Timing: how long the gateway took to admit the request (auth, model check,
    # budget, rate limits) before routing it — the gateway's fixed cost per request.
    started = obs_log.request_started.get()
    if started:
        rl_headers["server-timing"] = f"admit;dur={(time.perf_counter() - started) * 1000:.2f}"
    if body.stream:
        return await _stream(body, request, meter, rl_headers, fmt, chain)
    return await _complete(body, request, meter, rl_headers, anthropic_client, chain)


def _route_hints(body: ChatCompletionRequest, request: Request) -> dict[str, Any] | None:
    """Policy hints from the body (`route`, e.g. OpenAI SDK extra_body) or the
    `x-gateway-route` header. The body wins if both are sent."""
    extra = body.model_extra or {}
    if isinstance(extra.get("route"), dict):
        return dict(extra["route"])
    if header := request.headers.get("x-gateway-route"):
        return policy.parse_header(header)
    return None


async def _settle(meter: Meter) -> None:
    """Accounting must never turn a delivered (and billed) answer into an error."""
    try:
        await meter.settle()
    except Exception:
        log.exception("usage accounting failed")


async def _settle_shielded(meter: Meter) -> None:
    with anyio.CancelScope(shield=True):
        await _settle(meter)


def _code_of(resp: JSONResponse) -> str | None:
    try:
        code = json.loads(bytes(resp.body))["error"]["code"]
        return str(code) if code is not None else None
    except ValueError, KeyError, TypeError:
        return None


def _unknown(body: ChatCompletionRequest) -> JSONResponse:
    return error_response(
        404, f"The model '{body.model}' does not exist", code="model_not_found", param="model"
    )


async def _complete(
    body: ChatCompletionRequest,
    request: Request,
    meter: Meter,
    rl_headers: dict[str, str],
    anthropic_client: bool = False,
    chain: list[str] | None = None,
) -> JSONResponse | Response:
    routed = Routed(chain=chain or None)
    try:
        try:
            result, routed = await cancel_on_disconnect(request, router.route_chat(body, routed))
        except UnknownModel:  # the model vanished in a config reload mid-request
            meter.status, meter.error_code = 404, "model_not_found"
            meter.alias_label = "_unknown"
            return _unknown(body)
        except AllTargetsFailed as exc:
            resp = routing_error_response(exc)
            resp.headers.update(rl_headers)
            meter.routed(exc.routed)
            meter.status, meter.error_code = resp.status_code, _code_of(resp)
            return resp
        except ClientDisconnected:
            # The provider already has the prompt (and may still be generating): bill
            # at least the prompt to whoever was working on it, so hanging up isn't free.
            meter.target = routed.current or None
            meter.routed(routed)
            meter.status, meter.error_code = 499, "client_disconnected"
            return Response(status_code=499)  # nobody left to answer
        except BaseException:
            meter.status, meter.error_code = 500, "gateway_error"  # bug or shutdown
            raise
        meter.target = routed.target
        meter.routed(routed)
        meter.observe_completion(result)
        if not anthropic_client:
            result = extensions.strip_response(result)  # OpenAI clients: standard fields only
        return JSONResponse(result, headers={**routed.headers(), **rl_headers})
    except BaseException:
        meter.status, meter.error_code = 500, "gateway_error"  # bug or shutdown
        raise
    finally:
        with anyio.CancelScope(shield=True):
            await _settle(meter)


async def _stream(
    body: ChatCompletionRequest,
    request: Request,
    meter: Meter,
    rl_headers: dict[str, str],
    fmt: StreamFormat | None = None,
    chain: list[str] | None = None,
) -> JSONResponse | Response:
    # Retries and fallback cover everything up to the first chunk, which is pulled
    # *before* the 200 goes out (ADR 0002, 0005). That wait can be long (prompt
    # processing), so it is watched for disconnects too.
    routed = Routed(chain=chain or None)
    try:
        (first, chunks), routed = await cancel_on_disconnect(
            request, router.route_stream(body, routed)
        )
    except BaseException as exc:
        # Nothing was streamed. Settle now (shielded: we may be cancelled) — after this
        # point the SSEResponse owns settling.
        meter.routed(getattr(exc, "routed", routed))
        meter.status, meter.error_code = 500, "gateway_error"
        if isinstance(exc, ClientDisconnected):
            meter.target = routed.current or None  # bill the prompt, as for non-streams
            meter.status, meter.error_code = 499, "client_disconnected"
        if isinstance(exc, UnknownModel):
            meter.status, meter.error_code = 404, "model_not_found"
            meter.alias_label = "_unknown"
        if not isinstance(exc, AllTargetsFailed):  # that path settles after its status
            await _settle_shielded(meter)
        if isinstance(exc, UnknownModel):
            return _unknown(body)
        if isinstance(exc, AllTargetsFailed):
            resp = routing_error_response(exc)
            resp.headers.update(rl_headers)
            meter.status, meter.error_code = resp.status_code, _code_of(resp)
            await _settle_shielded(meter)
            return resp
        if isinstance(exc, ClientDisconnected):
            return Response(status_code=499)
        raise
    meter.target = routed.target
    meter.routed(routed)
    return SSEResponse(
        relay_sse(first, chunks, meter, fmt),
        upstream=chunks,
        headers={
            **routed.headers(),
            **rl_headers,
            "cache-control": "no-cache",
            "x-accel-buffering": "no",
        },
        on_close=lambda: _settle(meter),  # after the stream ends, however it ends
    )


# --- admin -------------------------------------------------------------------


class NewKey(BaseModel):
    model_config = ConfigDict(extra="forbid")  # a typo'd field must not be silently ignored

    name: str = Field(min_length=1, max_length=100)
    tier: str
    team: str | None = Field(default=None, min_length=1, max_length=100)
    requests_per_minute: int | None = Field(default=None, gt=0)
    tokens_per_minute: int | None = Field(default=None, gt=0)
    monthly_budget_usd: float | None = Field(default=None, ge=0)
    allowed_aliases: list[str] | None = None


@app.post("/admin/keys", status_code=201, response_model=None)
async def create_key(new: NewKey, _: Admin) -> dict[str, Any] | JSONResponse:
    if new.tier not in config.limits.tiers:
        return error_response(
            400, f"unknown tier {new.tier!r}; one of {list(config.limits.tiers)}", param="tier"
        )
    if new.team is not None and new.team not in config.limits.teams:
        return error_response(
            400, f"unknown team {new.team!r}; one of {list(config.limits.teams)}", param="team"
        )
    overrides = new.model_dump(exclude={"name", "tier"}, exclude_none=True)
    key, plaintext = await services.keys.store.create(new.name, new.tier, overrides)
    # The only time the plaintext exists outside the client: store it now.
    return {**key.public(), "key": plaintext}


@app.get("/admin/keys")
async def list_keys(_: Admin) -> dict[str, Any]:
    keys = await services.keys.store.list()
    return {
        "data": [
            {**k.public(), "spent_this_month_usd": await services.spend.spent(k.id)} for k in keys
        ]
    }


@app.delete("/admin/keys/{key_id}", response_model=None)
async def revoke_key(key_id: str, _: Admin) -> dict[str, Any] | JSONResponse:
    if not await services.keys.store.revoke(key_id):
        return error_response(404, "no active key with that id", code="key_not_found")
    services.keys.invalidate()  # this instance stops accepting it now; others within 30s
    return {"revoked": True, "id": key_id}


class KeyUpdate(BaseModel):
    """Fields to change; a field sent as null clears it (back to the tier's value)."""

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=100)
    tier: str | None = None
    team: str | None = Field(default=None, min_length=1, max_length=100)
    requests_per_minute: int | None = Field(default=None, gt=0)
    tokens_per_minute: int | None = Field(default=None, gt=0)
    monthly_budget_usd: float | None = Field(default=None, ge=0)
    allowed_aliases: list[str] | None = None


@app.patch("/admin/keys/{key_id}", response_model=None)
async def update_key(key_id: str, changes: KeyUpdate, _: Admin) -> dict[str, Any] | JSONResponse:
    """Edit a key in place: name, tier, team, limits, allowed aliases. The plaintext key
    doesn't change. Takes effect here now and on other replicas within 30 s."""
    fields = changes.model_dump(include=changes.model_fields_set)
    if fields.get("name", "") is None or fields.get("tier", "") is None:
        return error_response(400, "name and tier can be changed but not cleared")
    if (tier := fields.get("tier")) is not None and tier not in config.limits.tiers:
        return error_response(
            400, f"unknown tier {tier!r}; one of {list(config.limits.tiers)}", param="tier"
        )
    if (team := fields.get("team")) is not None and team not in config.limits.teams:
        return error_response(
            400, f"unknown team {team!r}; one of {list(config.limits.teams)}", param="team"
        )
    key = await services.keys.store.update(key_id, fields)
    if key is None:
        return error_response(404, "no active key with that id", code="key_not_found")
    services.keys.invalidate()
    return {**key.public(), "spent_this_month_usd": await services.spend.spent(key.id)}


@app.get("/admin/teams")
async def list_teams(_: Admin) -> dict[str, Any]:
    """Teams from limits.yaml with their budget, month-to-date spend and active keys."""
    keys = [k for k in await services.keys.store.list() if not k.revoked]
    data = []
    for name, team in config.limits.teams.items():
        data.append(
            {
                "team": name,
                "monthly_budget_usd": team.monthly_budget_usd,
                "spent_this_month_usd": await services.spend.spent(f"team:{name}"),
                "keys": sum(1 for k in keys if k.team == name),
            }
        )
    return {"data": data}


@app.post("/admin/reload", response_model=None)
async def reload(_: Admin) -> dict[str, object] | JSONResponse:
    try:
        reg = config.reload_registry()
    except Exception as exc:  # bad YAML / validation error: the old config stays live
        msg = f"reload failed, previous config kept: {type(exc).__name__}: {str(exc)[:300]}"
        return error_response(400, msg, code="invalid_config")
    return {"reloaded": True, "aliases": list(reg.aliases)}


@app.get("/admin/providers", response_model=None)
async def provider_status(_: Admin) -> dict[str, object]:
    """Circuit-breaker state of every target used by an alias."""
    targets = dict.fromkeys(t for a in config.registry.aliases.values() for t in a.chain)
    return {"targets": {t: str(await router.store.state(t)) for t in targets}}


def resolve_target(model: str) -> tuple[ProviderAdapter, str, str]:
    """alias or provider/model → (adapter, upstream model id, "provider/model") of the
    chain's first entry. Routing uses the whole chain (app.routing.router); this is for
    tools and tests that need one specific adapter."""
    reg = config.registry
    try:
        target = reg.resolve(model)[0]
    except KeyError as exc:
        raise LookupError(f"The model '{model}' does not exist") from exc
    provider, _, upstream_model = target.partition("/")
    cfg = reg.providers.get(provider)
    if cfg is None:
        raise LookupError(f"The model '{model}' does not exist")
    return providers.pool.get(provider, cfg), upstream_model, target
