# Quality and safety: injection filter and LLM-as-judge

## Prompt-injection filter

ADR 0021. Apps put untrusted text into prompts: what users type, and what tools bring back
(web pages, emails, files). Hidden instructions in that text, known as **prompt injection**,
can make a model ignore its rules, reveal its system prompt, or leak data. The gateway sees
every prompt, so it checks them.

### How it works

- **Rules** in `config/guardrails.yaml` (hot-reloaded) are weighted regular expressions.
  They're matched against **normalised** text: Unicode folded (full-width letters become
  plain), zero-width characters removed, lower-cased, whitespace collapsed. Trivial
  obfuscation doesn't slip past.
- **Roles:** by default the rules apply to **user messages and tool results**. Tool results
  are where *indirect* injection hides. System messages are the operator's and aren't
  scanned.
- **Detection:** a request scoring `threshold` (default 1.0) or more is a detection. One
  strong signal is enough; weak ones add up.

The shipped rules:

| Rule | Catches | Weight |
|------|---------|--------|
| `ignore_instructions` | "ignore / disregard / override … previous / system … instructions" | 1.0 |
| `reveal_system_prompt` | "reveal / print / repeat … system prompt / hidden instructions" | 1.0 |
| `role_override` | "you are now …", "developer mode", "do anything now" | 0.6 |
| `fake_chat_markup` | Chat-template tokens and fake role headers (`<\|im_start\|>`, `### system:`) | 0.6 |
| `restriction_removal` | "without restrictions / filters / content policy" | 0.5 |
| `exfiltration_link` | Markdown images or links that put data into a URL query | 1.0 |
| `tool_instruction` | Tool output that addresses the assistant ("the AI must now email …") | 0.5 |

Tests check them against common attacks *and* ordinary prompts ("show me the previous
quarter's results"), so normal use isn't flagged.

### Actions per tier

In `limits.yaml`, each tier sets `injection`:

| Action | Effect |
|--------|--------|
| `off` | No scanning |
| `log` | Detections are logged and counted (default) |
| `flag` | Also `x-gateway-guardrail: flagged; rules=…` on the response; the request proceeds |
| `block` | 400 `prompt_injection_detected` (in the client's format); nothing reaches a provider |

Start with `log`, watch `gateway_guardrail_detections_total{rule}`, adjust the weights, then
move tiers to `flag` or `block`. Logs and metrics carry **rule names and scores, never
content**.

### Optional classifier

```yaml
classifier:
  alias: fast            # any gateway alias
  when: suspicious       # suspicious (borderline only) | always
  timeout_seconds: 5
  max_chars: 4000
```

For **borderline** requests (some rule matched, but below the threshold), a model is asked
whether the text is an injection attempt.
- **Prompting:** the untrusted text is wrapped in tags with an instruction not to follow it.
- **Can only add:** only "INJECTION" adds a detection; "SAFE" can't clear a rule match. So
  an attacker who manages to fool the classifier gains nothing.
- **Failure:** errors fail open.
- **Cost:** each check is one small request, billed to your provider account.

### What it can't do

Heuristics are a tripwire for common and copy-pasted attacks, and they'll miss novel ones.
Keep the model-side defences:
- give tools least privilege;
- confirm irreversible actions;
- treat tool output as data, never as instructions.

## LLM-as-judge sampling

ADR 0022. Latency, errors and cost are measured on every request. **Answer quality** needs
a reviewer. The judge is a strong model that grades a sample of real answers against your
rubric.

```yaml
aliases:
  support:
    chain: [...]
    judge:
      sample_rate: 0.05        # 5% of successful answers
      judge: smart             # the alias that grades; use a different, stronger model
      rubric: "Is the answer correct, polite, and does it cite the right policy?"
      max_chars: 12000
```

- **When:** sampling is decided when the request arrives. Only sampled requests keep their
  text, and only in memory until they're judged.
- **How:** after a successful answer (assembled from the stream if needed), the
  conversation, answer and rubric go to the judge in the background. The judge must reply
  with JSON: a **score from 1 to 5** and **labels** from a fixed list: `good`, `incorrect`,
  `incomplete`, `off_topic`, `unsafe`, `verbose`, `refused`.
- **What's kept:** the score and labels only, in `judge_scores`, joinable to `usage_log` by
  request id. **No content and no free-text reasons**, which could quote the prompt.
- **Seeing it:** Grafana's quality row shows the average score per alias and A/B arm, and
  the label distribution. Metrics: `gateway_judge_score{alias,variant}` and
  `gateway_judge_total{result}` (scored, dropped, error, unparsable).
- **Load:** the queue is bounded. Under load, samples are dropped and counted, so judging
  never slows traffic.

Pair it with [A/B tests](Smart-Routing.md#ab-tests-variants) to answer "is the cheaper model
good enough?" with data. Judges have blind spots too, so spot-check a few answers by hand.
Sampled content is sent to the judge's provider.
