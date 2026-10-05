---
name: phase-check
description: Verify the current phase's Definition of Done from PLAN.md and list exactly what is missing before the phase can be closed.
---
1. Find the current phase in SESSION.md, then its tasks and DoD in PLAN.md.
2. For each task, check the code/tests/config actually exist and work (run commands where possible).
3. Output a table: task | status (done / partial / missing) | evidence.
4. If everything passes: suggest the version bump per CHANGELOG's version plan, and run code-reviewer before merging.
