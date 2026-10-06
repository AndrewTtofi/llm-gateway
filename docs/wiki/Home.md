# LLM Gateway wiki

LLM Gateway is a self-hosted service that sits between your applications and LLM
providers. Apps send requests to one endpoint. The gateway then:

- picks a model from a configured chain;
- retries and falls back when a provider fails;
- enforces per-key rate limits and monthly budgets;
- records what every request cost;
- exports metrics and dashboards.

Clients can call it with the **OpenAI chat-completions** format (`/v1/chat/completions`) or
the **Anthropic Messages** format (`/v1/messages`). Behind it, requests go to **Anthropic
Claude**, **OpenAI**, **Ollama** (local models) or any **OpenAI-compatible** API.

```
apps ──► gateway ──► Claude · OpenAI · Ollama · vLLM · …
         auth → limits → router (retries, fallback, breakers) → adapter → provider
```

The README is the short version. This wiki explains how each part works and why it was
built that way, including the AI-specific parts (tokens, streaming, provider formats) for
readers who are new to them.

## Where to start

| If you want to… | Read |
|-----------------|------|
| Run it locally in five minutes | [Getting started](Getting-Started.md) |
| See it serve many apps with separate keys and budgets | [Use case: one gateway for many apps](Use-Case-Multi-App.md) |
| Understand the vocabulary (aliases, targets, tokens, TTFT, SSE) | [Core concepts](Core-Concepts.md) and [Glossary](Glossary.md) |
| See how a request flows through the system | [Architecture](Architecture.md) |
| Know which providers work and what gets translated | [Providers and translation](Providers-and-Translation.md) |
| Understand failover and circuit breakers | [Routing and reliability](Routing-and-Reliability.md) |
| Issue keys and control spend | [Keys, limits and budgets](Keys-Limits-and-Budgets.md) |
| Monitor it | [Observability](Observability.md) |
| Let apps pick models by price, capability, quality and live speed; keep prices current | [Choosing models](Choosing-Models.md) |
| Route by policy (`model: auto`) or run A/B tests | [Smart routing](Smart-Routing.md) |
| Cache answers (exact or semantic) | [Response cache](Response-Cache.md) |
| Filter prompt injection; score answer quality | [Quality and safety](Quality-and-Safety.md) |
| Recover providers automatically; get alerts | [Self-healing](Self-Healing.md) |
| Deploy with TLS, replicas and monitoring | [Production deployment](Production-Deployment.md) |
| Change models, prices or limits | [Configuration reference](Configuration-Reference.md) |
| Integrate a client | [API reference](API-Reference.md) |
| Run it in production | [Operations and deployment](Operations-and-Deployment.md) |
| Check the performance claims | [Testing and benchmarks](Testing-and-Benchmarks.md) |
| Review the security model | [Security](Security.md) |
| Ask whether a subscription (Claude Pro/Max, ChatGPT/Codex) works with it | [Subscriptions and provider terms](Subscriptions-and-Terms.md) |
| Find a quick answer | [FAQ](FAQ.md) |

## Design records

Every non-obvious decision has an ADR in
[`docs/decisions/`](https://github.com/AndrewTtofi/llm-gateway/tree/main/docs/decisions).
Each wiki page links to the ADRs behind it.

## Keeping the wiki in sync

The wiki's source lives in the main repo under
[`docs/wiki/`](https://github.com/AndrewTtofi/llm-gateway/tree/main/docs/wiki), so it is
reviewed in the same pull requests as the code it describes. After every merge to `main`
that changes it, the `wiki` workflow publishes it to the GitHub Wiki tab
(`scripts/publish_wiki.sh`). Edit the files in `docs/wiki/`, not the Wiki tab: edits made
there are overwritten by the next sync.
