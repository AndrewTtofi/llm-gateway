# Getting started

## Requirements

- Docker with Compose.
- Optional: [Ollama](https://ollama.com) for a free local model (`ollama pull llama3.2:3b`).
- Optional: provider API keys (Anthropic, OpenAI). Without them you can still use Ollama and
  the built-in `fake` provider.

## 1. Start the stack

```bash
git clone https://github.com/AndrewTtofi/llm-gateway.git && cd llm-gateway
cp .env.example .env
```

Edit `.env`:

| Variable | What to put |
|----------|-------------|
| `ANTHROPIC_API_KEY` | From console.anthropic.com (optional) |
| `OPENAI_API_KEY` | From platform.openai.com (optional) |
| `OLLAMA_BASE_URL` | Usually `http://host.docker.internal:11434/v1` |
| `GATEWAY_ADMIN_KEY` | A long random string; protects `/admin/*` |

```bash
make up            # gateway, Redis, Postgres, Prometheus, Grafana
curl localhost:8000/healthz      # {"status":"ok"}
curl localhost:8000/readyz       # {"status":"ready","dependencies":{"redis":"ok","postgres":"ok"}}
```

| Service | URL |
|---------|-----|
| Gateway (interactive API docs at `/docs`) | http://localhost:8000 |
| Grafana (admin / admin) | http://localhost:3000 |
| Prometheus | http://localhost:9090 |

The dev stack binds every port to `127.0.0.1`, so nothing is exposed on your network.

## 2. Create a key

```bash
make key name=me tier=dev
```

The command prints a `gw_…` key once. The gateway stores only its SHA-256 hash, so save it
now. The `dev` tier allows the `fast` and `local` aliases; see
[Keys, limits and budgets](Keys-Limits-and-Budgets.md) for the other tiers.

## 3. Send a request

With curl:

```bash
export GW_KEY=gw_...
curl localhost:8000/v1/chat/completions -H "Authorization: Bearer $GW_KEY" \
  -H 'content-type: application/json' \
  -d '{"model":"local","messages":[{"role":"user","content":"Say hi in five words"}]}'
```

With the OpenAI SDK:

```python
from openai import OpenAI

client = OpenAI(base_url="http://localhost:8000/v1", api_key="gw_...")
for chunk in client.chat.completions.create(
    model="fast", messages=[{"role": "user", "content": "Hello"}], stream=True
):
    print(chunk.choices[0].delta.content or "", end="")
```

With the Anthropic SDK:

```python
import anthropic

client = anthropic.Anthropic(base_url="http://localhost:8000", api_key="gw_...")
print(client.messages.create(
    model="fast", max_tokens=200, messages=[{"role": "user", "content": "Hello"}]
).content[0].text)
```

The response headers say what happened:

- `x-gateway-provider`: who answered;
- `x-gateway-fallback`: whether a fallback was used;
- `x-gateway-attempts`: how many upstream calls were made.

## 4. Watch it fail over (free)

The dev stack loads a `fake` provider with failure profiles. Create a key on the `chaos` tier:

```bash
make key name=demo tier=chaos
export GW_KEY=gw_...
for i in $(seq 1 10); do
  curl -s -o /dev/null -D - localhost:8000/v1/chat/completions -H "Authorization: Bearer $GW_KEY" \
    -H 'content-type: application/json' \
    -d '{"model":"chaos-down","messages":[{"role":"user","content":"hi"}]}' | grep -i x-gateway-attempts
done
```

The primary of `chaos-down` always fails:

- **First requests:** `x-gateway-attempts: 3`. The gateway retries the primary, then falls back.
- **After about five failures:** the circuit breaker opens. Attempts drop to `1`, because the
  dead primary is skipped.

Open Grafana → **LLM Gateway** to watch it happen. `chaos-blip` is down for 20 s of every
minute, so you can also watch its breaker recover.

## 5. Next steps

- Point real apps at it: [API reference](API-Reference.md).
- Change which models an alias uses: [Configuration reference](Configuration-Reference.md).
- Route Claude Code through it: [Providers and translation → Anthropic clients](Providers-and-Translation.md#inbound-anthropic-messages-api-v1messages).
- Stop the stack: `make down`. Data stays in Docker volumes.
