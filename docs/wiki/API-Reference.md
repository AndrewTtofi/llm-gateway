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
| GET | `/healthz` | Liveness: the process answers |
| GET | `/readyz` | Readiness: 503 until startup completes; then 200 `ready` / `degraded`, with dependency status |
| POST | `/admin/keys` | Create a key: `{name, tier, requests_per_minute?, tokens_per_minute?, monthly_budget_usd?, allowed_aliases?}` |
| GET | `/admin/keys` | List keys with month-to-date spend |
| DELETE | `/admin/keys/{id}` | Revoke a key |
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
| `x-request-id` | always | Correlates with logs and the usage row. A valid incoming id is reused |
| `x-gateway-provider` | routed | The `provider/model` that served |
| `x-gateway-fallback` | routed | `true` if it wasn't the chain's first choice |
| `x-gateway-attempts` | routed | Upstream calls made, including retries |
| `x-ratelimit-{limit,remaining,reset}-{requests,tokens}` | admitted or rate-limited | OpenAI-style limit state |
| `retry-after` | 429 | Seconds until both buckets have room |
| `server-timing: admit;dur=…` | admitted | Milliseconds the gateway spent on auth, budget and limits |

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
| 400 | (none), `unsupported_parameter`, `upstream_rejected` | Invalid body, untranslatable request, provider rejected the input | No; fix the request |
| 401 | `invalid_api_key` | Missing, malformed or revoked key | No |
| 403 | `model_not_allowed` | The key's tier doesn't allow this alias | No |
| 403 | `tier_unknown` | The key's tier was removed from `limits.yaml` | No; operator fix |
| 404 | `model_not_found` | Unknown alias or model | No |
| 429 | `rate_limit_exceeded` | Over requests/min or tokens/min | Yes, after `retry-after` |
| 429 | `insufficient_quota` | Monthly budget used up | No; wait for next month or raise the budget |
| 429 | `upstream_rate_limited` | Every target was rate-limited upstream | Yes |
| 499 | `client_disconnected` | (usage log only) the client hung up | — |
| 501 | `provider_not_supported` | The chain only has unimplemented provider types | No |
| 502 | `upstream_error` | The provider failed with no better target | Yes |
| 503 | `all_providers_unavailable` | Every target's breaker is open, or all failed | Yes |
| 503 | `upstream_quota_exhausted` | The provider account is out of credit | No; operator fix |
| 503 | `auth_unavailable` | Key store down and the key isn't cached | Yes |
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
