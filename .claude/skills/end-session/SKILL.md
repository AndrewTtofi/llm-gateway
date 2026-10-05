---
name: end-session
description: End a work session. Runs tests and lint, updates SESSION.md, CHANGELOG.md and PLAN.md checkboxes, and proposes a commit message.
---
1. Run `make test` and `make lint`. If red, report and stop — don't update docs to claim done.
2. Use the docs-keeper agent to update SESSION.md, CHANGELOG.md [Unreleased] and PLAN.md checkboxes from the actual diff.
3. Ask the user for one line on what they learned this session; add it under "Learned".
4. Propose a conventional commit message. Don't commit unless the user says so.
