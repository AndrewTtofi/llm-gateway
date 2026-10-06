# API reference

The interactive OpenAPI docs are at `http://localhost:8000/docs`.

## Authentication

| Endpoint group | Credential |
|----------------|------------|
| `/v1/*` | A gateway key: `Authorization: Bearer gw_…` or `x-api-key: gw_…` |
| `/admin/*` | `Authorization: Bearer $GATEWAY_ADMIN_KEY` |
| `/healthz`, `/readyz` | None |

## Endpoints

| Method | Path | Purpose |
|--------|------|---------|
| POST | `/v1/chat/completions` | OpenAI chat completions, streaming or not |
| POST | `/v1/messages` | Anthropic Messages API, streaming or not |
| POST | `/v1/messages/count_tokens` | The gateway's input-token estimate (counts as one request against the key's limit) |
| GET | `/v1/models` | Aliases (with their chains) and direct models this key may use |
| GET | `/v1/catalog` | Price, capabilities, quality, breaker state and live stats per model this key may use; `?capability=`, `?min_context=`, `?sort=price\|quality\|ttft\|latency` ([Choosing models](Choosing-Models.md)); counts as one request |
| GET | `/healthz` | Liveness: the process answers |
| GET | `/readyz` | Readiness: 503 until startup completes; then 200 `ready` / `degraded`, with dependency status |
| POST | `/admin/keys` | Create a key: `{name, tier, team?, requests_per_minute?, tokens_per_minute?, monthly_budget_usd?, allowed_aliases?}` |
| GET | `/admin/keys` | List keys with month-to-date spend |
| PATCH | `/admin/keys/{id}` | Edit a key in place (same fields; `null` clears an override) |
| DELETE | `/admin/keys/{id}` | Revoke a key |
| GET | `/admin/teams` | Teams with budget, month-to-date spend and active keys |
| POST | `/admin/reload` | Reload `config/*.yaml` |
| GET | `/admin/providers` | Circuit-breaker state per target |
| GET | `:9100/metrics` | Prometheus (internal port) |

### `/v1/chat/completions`

This is the standard OpenAI request. The gateway reads `model`, `messages`, `stream`,
`stream_options`, `max_tokens` and `max_completion_tokens`, and forwards everything else to
the provider: tools, `response_format`, `temperature` and so on. For Anthropic targets the
adapter translates; see [Providers and translation](Providers-and-Translation.md).

### `/v1/messages`

This is the standard Anthropic request (`model`, `max_tokens`, `messages`, `system`, `tools`,
`tool_choice`, `stream`, …). What's supported and what's dropped is in
[Providers and translation → Inbound](Providers-and-Translation.md#inbound-anthropic-messages-api-v1messages).

## Response headers

| Header | When | Meaning |
|--------|------|---------|
| `x-request-id` | always | The gateway's id for the request, in its logs and usage row |
| `x-client-request-id` | the caller sent an id | The caller's own id, echoed; stored beside the gateway's |
| `x-gateway-provider` | routed | The `provider/model` that served |
| `x-gateway-fallback` | routed | `true` if it wasn't the chain's first choice |
| `x-gateway-attempts` | routed | Upstream calls made, including retries |
| `x-ratelimit-{limit,remaining,reset}-{requests,tokens}` | admitted or rate-limited | OpenAI-style limit state |
| `retry-after` | 429 | Seconds until both buckets have room |
| `server-timing: admit;dur=…` | admitted | Milliseconds the gateway spent on auth, budget and limits |
| `x-gateway-route` | policy aliases | `optimize=…; considered=…; chain=…` ([Smart routing](Smart-Routing.md)) |
| `x-gateway-variant` | A/B aliases | The arm this request was assigned to |
| `x-gateway-cache` | cached aliases | `hit`, `miss`, `bypass`, `refresh` or `uncacheable` ([Response cache](Response-Cache.md)) |
| `x-gateway-guardrail` | tiers with `flag`/`block` | `flagged; rules=…` (plus `; unscanned` if some text was over the scan budget), `unscanned`, or `blocked` without rule names ([Quality and safety](Quality-and-Safety.md)) |

Request headers the gateway reads:

| Header | Effect |
|--------|--------|
| `x-gateway-route` | Policy hints: `optimize=…; needs=…; min_quality=…; max_price=…` (they can only tighten) |
| `x-gateway-variant` | Pin an A/B arm by name |
| `x-gateway-cache` | `bypass` (no read, no write) or `refresh` (no read, write; key-scoped caches only) |
| `x-client-request-id` (or `x-request-id`) | Your own id for tracing, if it matches `[A-Za-z0-9._:-]{1,64}`. Echoed and logged; the gateway still assigns its own |

## Errors

On `/v1/chat/completions` and the admin API, errors use OpenAI's shape, so OpenAI SDKs raise
the right exception class:

```json
{"error": {"message": "…", "type": "rate_limit_error", "param": null, "code": "rate_limit_exceeded"}}
```

On `/v1/messages*`, errors use Anthropic's shape:
`{"type": "error", "error": {"type": "rate_limit_error", "message": "…"}}`.

| Status | `code` | When | Retry? |
|--------|--------|------|--------|
| 400 | (none), `unsupported_parameter`, `upstream_rejected` | Invalid body (including over 10 000 messages, 1 000 parts per message, `max_tokens` over 1 000 000, `n` outside 1–128), untranslatable request, provider rejected the input | No; fix the request |
| 400 | `invalid_route` | Bad routing hints (unknown hint, wrong type) | No |
| 400 | `no_route` | No model satisfies the policy and the request's needs | No; relax the request |
| 400 | `prompt_injection_detected` | Blocked by the prompt-injection filter (tier action `block`) | No |
| 401 | `invalid_api_key` | Missing, malformed or revoked key | No |
| 403 | `model_not_allowed` | The key's tier doesn't allow this alias | No |
| 403 | `tier_unknown` | The key's tier was removed from `limits.yaml` | No; operator fix |
| 403 | `team_unknown` | The key's team was removed from `limits.yaml` (fails closed) | No; operator fix |
| 404 | `model_not_found` | Unknown alias or model | No |
| 429 | `rate_limit_exceeded` | Over requests/min or tokens/min | Yes, after `retry-after` |
| 429 | `admin_login_limited` | (admin API) too many failed admin logins from this source in a minute | After a minute |
| 429 | `concurrency_limit_exceeded` | The key already has its tier's `concurrent_requests` in flight on this replica | Yes, when one finishes |
| 413 | `request_too_large` | Body over `MAX_BODY_BYTES` | No |
| 429 | `insufficient_quota` | The key's or its team's monthly budget is used up | No; wait for next month or raise the budget |
| 429 | `upstream_rate_limited` | Every target was rate-limited upstream | Yes |
| 499 | `client_disconnected` | (usage log only) the client hung up | — |
| 501 | `provider_not_supported` | The chain only has unimplemented provider types | No |
| 502 | `upstream_error` | The provider failed with no better target | Yes |
| 503 | `all_providers_unavailable` | Every target's breaker is open, or all failed | Yes |
| 503 | `upstream_quota_exhausted` | The provider account is out of credit | No; operator fix |
| 503 | `gateway_busy` | Every connection to the provider is in use on this replica (never counted against the provider) | Yes, after `retry-after` |
| 503 | `auth_unavailable` | Key store down and the key isn't cached | Yes |
| 503 | `no_route` | Models fit the policy but none is available right now (keys missing, breakers open) | Yes |
| 504 | `upstream_timeout` | The provider timed out | Yes |

**Mid-stream errors.** After a stream has started, failures arrive in-band and the stream
ends without its end marker:

- **OpenAI format:** `data: {"error": {...}}` and no `data: [DONE]`.
- **Anthropic format:** `event: error` and no `message_stop`.

The usage row and metrics record these as `200:<code>`.

## Examples

```bash
# non-streaming, OpenAI format
curl localhost:8000/v1/chat/completions -H "Authorization: Bearer $GW_KEY" \
  -H 'content-type: application/json' \
  -d '{"model":"fast","messages":[{"role":"user","content":"hi"}],"max_tokens":50}'

# streaming with usage at the end
curl -N localhost:8000/v1/chat/completions -H "Authorization: Bearer $GW_KEY" \
  -H 'content-type: application/json' \
  -d '{"model":"fast","stream":true,"stream_options":{"include_usage":true},"messages":[{"role":"user","content":"hi"}]}'

# Anthropic format
curl localhost:8000/v1/messages -H "x-api-key: $GW_KEY" -H 'content-type: application/json' \
  -d '{"model":"fast","max_tokens":50,"messages":[{"role":"user","content":"hi"}]}'

# what can this key use?
curl localhost:8000/v1/models -H "Authorization: Bearer $GW_KEY"
```
