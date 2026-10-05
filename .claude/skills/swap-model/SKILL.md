---
name: swap-model
description: Change which model an alias uses or add a model to a fallback chain. Usage: /swap-model <alias> <provider/model> [position]. Edits config only, then reloads and smoke-tests.
---
1. Confirm the provider exists in config/models.yaml; if not, run add-provider first.
2. Verify the model ID against the provider's current docs (WebFetch) — model IDs change.
3. Edit config/models.yaml: replace or insert the model in the alias chain (position defaults to first).
4. Ensure config/pricing.yaml has an entry; if the price is unknown, add null and tell the user.
5. Run `make test` (config tests catch typos), then `make reload`.
6. Smoke test: one non-streaming and one streaming request to the alias; show which provider served it (x-gateway-provider header).
7. Add a CHANGELOG entry under [Unreleased] → Changed: "alias <x> now uses <model>".
No code in app/ should change. If it seems to need to, stop and explain why.
