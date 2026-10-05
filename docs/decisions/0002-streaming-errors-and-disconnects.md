# 0002 — Streaming error semantics and client disconnects

- **Status:** accepted
- **Date:** 2026-10-05
- **Phase:** 1

## Context
With `stream: true` the gateway answers with Server-Sent Events. Once the HTTP status
line and headers are sent, the status can't change, so a provider failing *after* that
point can't become a 4xx/5xx. Providers fail at three moments: before the first byte
(bad key, 429, model down), mid-stream (overload, connection drop), or never. Separately,
when a client hangs up the provider keeps generating — and billing — tokens unless the
gateway closes the upstream connection.

## Options considered
1. **Send 200 immediately, report every failure in-band** — simplest, but SDK users get
   a "successful" response that errors on the first read, and Phase 3 fallback can't
   retry a request whose 200 has already gone out.
2. **Buffer the first chunk before sending headers** — upfront failures keep proper
   statuses (and can fall back in Phase 3); costs nothing, because the client can't show
   anything before the first token anyway.
3. **Buffer the whole response** — defeats streaming.

## Decision
Option 2. Upfront failures → normal OpenAI error JSON with a real status. Mid-stream
failures → one `data: {"error": {...}}` event and **no** `data: [DONE]`, so the stream
can't be mistaken for a complete answer (the `openai` SDK raises `APIError` on it).

Disconnects: a watcher task on ASGI `receive()` cancels the upstream call for
non-streaming requests and for the wait before the first chunk (the longest wait —
prompt processing). After that, Starlette cancels the response on `http.disconnect`
(ASGI spec 2.3), but never closes the body iterator: if the cancel lands while the
gateway is blocked writing to a slow client, the upstream would stay open until GC.
`SSEResponse` therefore closes the relay and upstream generators itself, shielded from
the cancellation.

Error text: provider messages are shown to clients only for client-caused 4xx
(400/413/422), where they describe the client's own input. For everything else
(401/403/404/429/5xx, mid-stream errors, malformed bodies) clients get a generic
message; the raw text stays in `ProviderError.detail`, because it can contain API-key
fragments, org/project IDs and internal hostnames.

Phase 1 routes to the first entry of an alias's chain only; fallback is Phase 3.

## Consequences
- Phase 3 can retry/fall back any failure that happens before the first chunk.
  A mid-stream failure can't be retried transparently (the client has partial
  output) — that policy is a Phase 3 ADR.
- Tests drive the app at ASGI level to prove disconnects close upstream
  (`tests/test_disconnect.py`). If uvicorn moves to ASGI spec ≥ 2.4, Starlette
  detects disconnects only on the next write instead — re-check that test.
- Debugging provider failures needs server-side logs of `detail` (hashed or redacted) —
  Phase 5 logging.
- Deferred to Phase 3 (timeouts): per-provider connection-pool limits with a short pool
  timeout (today: httpx default 100, pool wait = read timeout → misleading 504), an
  overall stream deadline (keep-alives reset the read timeout), and closing adapters
  retired by config reload after a grace period.
