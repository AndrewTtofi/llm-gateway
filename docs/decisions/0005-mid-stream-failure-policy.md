# 0005 — Mid-stream failure policy

- **Status:** accepted
- **Date:** 2026-10-05
- **Phase:** 3

## Context
A streamed answer fails after the client has already received some tokens. The
gateway knows the partial text. Could it fall back transparently?

## Options considered
1. **Continue on the next provider** — send the conversation plus the partial answer as
   an assistant prefill and ask the next model to continue. Rejected: current Claude
   models reject assistant prefill (400); models phrase things differently, so the seam
   shows mid-sentence; tool-call arguments can't be safely spliced; and the partial
   answer is billed twice.
2. **Restart on the next provider and re-send from the beginning** — the client already
   rendered the first tokens; sending them again duplicates output in every UI that
   appends deltas.
3. **Fail in-band, let the client decide** — one `data: {"error": …}` event and no
   `[DONE]` (ADR 0002). The breaker counts the failure, so the *next* request goes
   elsewhere if the target keeps failing.

## Decision
Option 3. Everything up to the first chunk is retried and falls back (ADR 0004); after
it, the gateway is committed to the target. A stream that exceeds the provider's
`timeouts.stream_total` is ended the same way, so a provider trickling keep-alives can't
hold a connection forever.

## Consequences
- Most failures happen before the first token (auth, rate limits, overload, cold
  starts), so most stream failures are still invisible to clients.
- Clients must handle an in-band error; the `openai` SDK raises `APIError` for it.
- If Phase 6 shows mid-stream failures are common for some provider, revisit option 1
  for text-only answers on models that accept prefill.
