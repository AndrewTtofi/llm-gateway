# 0021 — Prompt-injection filter

- **Status:** accepted
- **Date:** 2026-10-06
- **Phase:** 10

## Context
Apps put untrusted text in prompts: user input, and tool results such as web pages,
emails and files. Instructions hidden in that text can make a model ignore its rules, leak
its system prompt or exfiltrate data through links. The gateway sees every prompt, so it is
a natural checkpoint. It can't solve injection (the model is the real target), but it can
catch the common forms.

## Options considered
1. **A classifier model on every request.** Better recall, but adds latency and cost to
   everything, and the classifier can itself be injected.
2. **Heuristics only.** Instant and free, but they miss novel attacks.
3. **Heuristics, with a classifier only for borderline cases.** The classifier may add
   detections, never remove them.

## Decision
Option 3 (`app/guardrails.py`, rules in `config/guardrails.yaml`, hot-reloaded):

- **Rules:** weighted regular expressions, matched against **normalised** text:
  - Unicode NFKD with combining marks dropped;
  - common Cyrillic and Greek confusables mapped to Latin;
  - zero-width characters removed;
  - lower-cased, with whitespace collapsed.

  The scan is bounded per message and per request.
  - **ReDoS:** the patterns have bounded gaps (`.{0,40}`), so matching is linear. The review
    measured this with crafted input.
  - They apply per role: user and tool by default. Tool results are the main carrier of
    indirect injection. System messages are the operator's.
  - The default set covers instruction overrides, system-prompt extraction, role/persona
    overrides, fake chat-template markup, restriction removal, markdown exfiltration links
    and tool results that address the assistant.
- **Detection:** a request scoring `threshold` or more.
- **Classifier (optional):**
  - It's asked about borderline requests (a score above 0 but below the threshold), or about
    every request with `when: always`.
  - The untrusted text is wrapped in tags with an instruction not to follow it.
  - Only "INJECTION" adds a detection: a "SAFE" answer can't clear a rule match, so
    injecting the classifier gains nothing.
  - Errors and timeouts fail open.
- **Action per tier:** `injection: off | log | flag | block`.
  - `flag` adds `x-gateway-guardrail: flagged; rules=…`.
  - `block` returns 400 `prompt_injection_detected` in the client's format, without saying
    which rule fired.
- **Privacy:** logs and the `gateway_guardrail_detections_total` metric carry rule names,
  scores and the action, never content.

## Consequences
- **A cheap tripwire** for common and copy-pasted attacks, tuneable per deployment
  (start with `log`, measure, then flag or block).
- **False positives are possible:** for example, a security course quoting attacks. The
  shipped rules are checked against benign prompts in the tests, but your traffic is the
  real test.
- **It doesn't replace model-side defences:** least-privilege tools, confirming
  irreversible actions, and treating tool output as data.
