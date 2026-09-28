---
name: sparkjury-evalset
description: Build the evaluation set for a SparkJury run: pick the scorable traces (precheck-clean), optionally restrict to given task ids or cap the count, and write runs/<run_id>/evalset.json. Use when you want a reproducible, smaller subset of traces to judge, or need the exact list of traces a run evaluated.
license: MIT
compatibility: Requires Python 3.12+ and the sparkjury package (uv sync in the SparkJury repo); model endpoints optional, offline mock mode available
metadata:
  author: sparkjury-team
  version: "0.1.0"
  product: sparkjury
  command: "sparkjury run --stages EVALSET"
---

# sparkjury-evalset

## When to use
- Before scoring, to fix exactly which traces will be judged (reproducibility, demo speed).
- To answer "which traces did run X evaluate?" (read `runs/<run_id>/evalset.json`).

## Steps
1. Make sure the store has been prechecked (`sparkjury-clean`).
2. Run only the EVALSET stage of the harness:
   `sparkjury run --config <run.toml> --stages EVALSET` (set `[evalset] limit` / `task_ids` in the TOML), or
   `python scripts/run.py --db <store.db> --run-id <id> [--limit N] [--task-ids a,b]`
3. The stage writes `runs/<run_id>/evalset.json` (list of trace ids) and records `n_traces`, `n_tasks` in the manifest.
4. Hand the file to scoring: `sparkjury score --evalset runs/<run_id>/evalset.json ...` judges exactly these ids (standalone `score` without it judges every scorable trace).

## Output
- `runs/<run_id>/evalset.json`
- Manifest entry `stages.EVALSET`

## Edge cases
- If precheck was never run, every trace counts as scorable.
- `limit` applies after `task_ids`.

## References
- Architecture and data contracts: `../../docs/ARCHITECTURE.md`
- Module acceptance log: `../../docs/MODULES.md`
