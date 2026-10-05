# Changing models

There are two separate kinds of "model" in this project. Both are changed by
editing config, never code.

## 1. Models the gateway routes to (your product)

Everything lives in `config/models.yaml` and `config/pricing.yaml`.

### Swap the model behind an alias
```yaml
aliases:
  smart:
    chain:
      - anthropic/claude-sonnet-5-5     # was claude-opus-5-5
      - openai/<model-id>
```
Then `make reload`. Clients keep sending `model: "smart"` — nothing changes for them.

### Change the fallback order
Reorder the `chain` list. First entry is tried first.

### Add a new alias
```yaml
aliases:
  cheap:
    chain: [ollama/llama3.2:3b, anthropic/claude-haiku-4-5-20251001]
```
Add it to `allowed_aliases` in `config/limits.yaml` for the tiers that may use it.

### Add a new provider
- OpenAI-compatible APIs (Ollama, vLLM, Groq, Together, Mistral, OpenRouter…):
  add a provider block with `type: openai` and its `base_url`. No code.
- Others: run `/add-provider <name>` in Claude Code.

### Model capabilities (Anthropic)
Claude models differ in which OpenAI parameters they accept. If a new model behaves
differently from the provider's `defaults`, add a line under `providers.anthropic.models`:
```yaml
    models:
      claude-new-model: { sampling: false, forced_tool_choice: false, effort: true, refusal_fallback: true }
```
`sampling` (temperature/top_p), `forced_tool_choice` (`tool_choice: "required"`/named),
`effort` (`reasoning_effort`), `refusal_fallback` (server-side refusal fallback, beta).
Check the model's docs; see ADR 0003.

### Reload
`make reload` (or `POST /admin/reload`, or `kill -HUP` when running without `--reload`).
A file that fails to parse or validate is rejected with a 400 and the previous config
stays live.

### Always also
1. Check the model ID in the provider's current docs.
2. Add the model's price to `config/pricing.yaml` (or `null` until you know it).
3. `make test` — config tests catch typos and unknown providers.
4. `make reload`, then send one test request.
5. CHANGELOG entry under **Changed**.

Or let Claude Code do all of it: `/swap-model smart anthropic/claude-sonnet-5-5`.

## 2. Models Claude Code uses to build the project (your tooling)

| What | Where | Values |
|------|-------|--------|
| Default model for your sessions | `.claude/settings.json` → `"model"` | `opus`, `sonnet`, `haiku`, or a full model ID |
| Per-agent model | `model:` line in `.claude/agents/<agent>.md` | same, or `inherit` to follow the session |
| One-off for the current session | `/model` command in Claude Code | — |

Current defaults: `opus` for planning and review (architect, code-reviewer),
`sonnet` for building, `haiku` for docs. Trade quality for cost by moving agents
down a tier; move `provider-adapter` up to `opus` if format translation gets hairy.
