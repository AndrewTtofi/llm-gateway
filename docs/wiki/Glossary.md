# Glossary

| Term | Meaning |
|------|---------|
| **Alias** | A model name clients use (`fast`, `smart`), mapped to an ordered chain of targets in `models.yaml` |
| **Backoff, full jitter** | Wait a random time between 0 and an exponentially growing cap before retrying, so many clients don't retry at the same moment |
| **Budget** | A monthly USD cap per key, enforced before each request |
| **Burn rate** | How fast an SLO's error budget is being spent; 1 = exactly on budget for the period |
| **Cached input tokens** | A prompt prefix the provider recognises from a recent request, billed far cheaper (prompt caching) |
| **Chain** | The ordered list of targets behind an alias; fallback walks it |
| **Circuit breaker** | Per-target switch that stops calling a failing target for a while, then probes it |
| **Completion tokens** | Tokens the model generated (output); usually the expensive kind |
| **Content block** | Anthropic's unit of output: a text, `tool_use` or thinking block, streamed as start → deltas → stop |
| **Context window** | The maximum tokens (prompt + output) a model can handle in one request |
| **Coordinated omission** | A benchmarking error where a slow system causes fewer measurements and so looks faster |
| **Effort / reasoning effort** | How much a reasoning model "thinks" before answering; trades latency and cost for quality |
| **Estimate, then reconcile** | Charge limits with a guess before the call; correct with real usage after |
| **Fail open** | When a dependency (Redis) is down, allow traffic rather than reject it |
| **Fallback** | Serving a request from a later target in the chain after an earlier one failed |
| **First-token boundary** | The moment the first chunk reaches the client; fallback is possible before it, not after |
| **Half-open** | Breaker state that lets exactly one probe request through to test recovery |
| **In-band error** | An error sent inside an already-started stream, since the HTTP status (200) has already gone out |
| **`max_tokens`** | The cap on output tokens for a request; also the basis of the gateway's token estimate |
| **Prompt tokens** | Tokens you send: system prompt, conversation history, tools, images |
| **Provider** | An upstream API (Anthropic, OpenAI, Ollama…) with its credentials and timeouts |
| **Refusal** | The model declining to answer (`stop_reason: refusal` / `finish_reason: content_filter`) |
| **SLO** | Service level objective, such as "99.9% of requests succeed over 30 days" |
| **SSE** | Server-Sent Events: a streaming HTTP response of `data:` lines, how LLM APIs stream tokens |
| **Stop reason / finish reason** | Why generation ended: natural end, length cap, tool call, stop sequence, refusal |
| **Target** | `provider/model`: the unit that's called, metered, priced and circuit-broken |
| **Tier** | A named set of limits, budget and allowed aliases in `limits.yaml`; each key has one |
| **Token** | The unit LLMs read, write and bill by; about 4 characters of English |
| **Token bucket** | A rate limiter that refills continuously and allows bursts up to its capacity |
| **Tool call** | The model asking the caller to run a function with JSON arguments; the result goes back in the next request |
| **TTFT** | Time to first token: how long until the first output arrives; what users perceive as speed |
| **Usage** | The provider's token counts for a request (prompt, completion, cached) |
