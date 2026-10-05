# 0022 — LLM-as-judge sampling

- **Status:** accepted
- **Date:** 2026-10-06
- **Phase:** 10

## Context
Latency, errors and cost are measured. Answer quality isn't. Sampling answers and having
a strong model grade them against a rubric gives a quality signal per alias, per model and
per A/B arm, without labelling by hand.

## Options considered
1. **Offline evaluation sets only.** Necessary, but not about real traffic.
2. **Online judging of a sample, in the background.**
3. **Judging every answer inline.** Doubles cost and latency.

## Decision
Option 2 (`app/judge.py`). An alias may set
`judge: {sample_rate, judge, rubric, max_chars}`:

- **Sampling:** the decision is made when the request arrives. Only sampled requests keep
  their conversation text, in memory, until judged.
- **Judging:**
  - After a successful answer (assembled from the stream if streamed), a job goes onto a
    bounded queue. A single background worker sends conversation, answer and rubric to the
    judge alias, with instructions not to follow anything in them.
  - The judge must reply with JSON: a 1–5 `score` and labels from a fixed list (`good`,
    `incorrect`, `incomplete`, `off_topic`, `unsafe`, `verbose`, `refused`). Anything
    else is counted as unparsable and dropped.
- **Stored:** in `judge_scores` (migration 0006): request id, alias, target, variant,
  judging model, score and labels. **No content and no free-text reasons**, which could
  quote the prompt. Joinable to `usage_log` by request id.
- **Metrics and dashboard:** `gateway_judge_total{result}` and `gateway_judge_score{alias,
  variant}`, plus Grafana panels for average score and the label distribution.
- **Load:** the queue drops samples (and counts them) instead of ever slowing traffic.

## Consequences
- **A quality signal next to cost and latency,** per A/B arm too. That makes "is the
  cheaper model good enough?" measurable.
- **Judge bias:** judges share blind spots with the models they grade. Keep the judge
  different from (and stronger than) the judged model, and spot-check by hand.
- **Privacy:** sampled content goes to the judge's provider. It's opt-in per alias, and
  the cost is billed to the provider account, not the caller's key.
