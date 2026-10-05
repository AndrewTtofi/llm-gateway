---
name: start-session
description: Start a work session on the LLM gateway. Reads SESSION.md and PLAN.md and reports the current phase, the next task and any blockers.
---
1. Read SESSION.md (Current state + latest session entry) and PLAN.md (current phase section).
2. Run `git status` and `git log --oneline -5`.
3. Report in under 10 lines: phase, branch, what was done last, the next unchecked task, blockers.
4. Propose the first concrete step for this session and which agent should do it. Wait for confirmation.
