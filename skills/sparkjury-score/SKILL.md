---
name: sparkjury-score
description: Score agent traces with a three-judge panel (three different model families) on four dimensions - outcome, tool_use, efficiency, safety - then resolve disagreements: cloud Jev decision model when configured, local judge otherwise (marked degraded), with a 5% audit sample. Use when you need per-trace quality scores, judge rationales, or want to know which traces the judges disagreed on.
license: MIT
compatibility: Requires Python 3.12+ and the sparkjury package (uv sync in the SparkJury repo); model endpoints optional, offline mock mode available
metadata:
  author: sparkjury-team
  version: "0.1.0"
  product: sparkjury
  command: "sparkjury score + arbitrate"
---

# sparkjury-score

## When to use
- After `sparkjury-clean`, to get a 0-4 score per dimension per trace with evidence steps and rationales.
- To find contested traces (`needs arbitration`) and how they were resolved.

## Steps
1. Choose judges: `mock` (offline) or a panel TOML (see `deploy/judges.example.toml`; keys come from env vars such as `STEPFUN_API_KEY`).
2. Score: `sparkjury score --db <store.db> --judges mock|<panel.toml> [--evalset runs/<id>/evalset.json] [--dims outcome,tool_use,efficiency,safety] [--limit N]`
   `--evalset` judges exactly the trace ids `sparkjury-evalset` fixed and nothing else; an evalset matching no scorable trace exits non-zero instead of silently scoring nothing.
3. Arbitrate: `sparkjury arbitrate --db <store.db> --judges mock|<panel.toml> [--jev auto|off] [--audit-rate 0.05]`
   (`TYPESAFE_API_KEY` enables Jev; without it every disagreement is resolved locally and flagged degraded)
4. Inspect one trace: `sparkjury verdicts <trace_id> --db <store.db>`

One call: `python scripts/run.py --db <store.db> --judges mock`

## Agreement rule
- outcome: three pass/fail labels must match; score dimensions: max - min <= 1. Any errored judge counts as disagreement.

## Output
- `verdicts`, `panel`, `arbitration` tables; `score --json` and `arbitrate --json` return summaries and per-trace results.

## Edge cases
- Judges that fail their health check are swapped for mock judges by the harness (`sparkjury run`), never silently.
- The agent under test's own model must not sit on the panel — enforced: `score` refuses such a panel (a model grading itself is not a judge) unless `--allow-self-judge` is passed explicitly.

## References
- Architecture and data contracts: `../../docs/ARCHITECTURE.md`
- Module acceptance log: `../../docs/MODULES.md`
