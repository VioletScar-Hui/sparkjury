# Skill card: sparkjury-arbitrate

## Description

Resolves three-family judge disagreements into one final per-dimension decision per trace.
Agreed dimensions take the panel median (majority label for `outcome`); disagreements escalate to
the cloud Jev decision model, and when Jev is unreachable a local judge arbitrates with the decision
explicitly marked degraded, before a deterministic 5% sample is re-scored by an independent audit
judge. Exposes `sparkjury arbitrate` as an Agent Skill so the arbitration protocol — including its
degradation semantics — is inspectable rather than buried in the harness.

## Owner

SparkJury team (DGX Spark Hackathon, 3rd edition) — skill authored by 万凌.

## License

MIT (see `../../LICENSE` or the repository root).

## Use Case

- After `sparkjury-score`, to collapse three verdicts per dimension into one decision per dimension.
- When a reviewer asks how a contested score was resolved and whether that resolution was degraded.
- Before `sparkjury-cluster` and `sparkjury-report`, both of which read decisions rather than verdicts.

## Requirements

- Python 3.12+ and the `sparkjury` package (`uv sync` in the SparkJury repo, or `pip install -e <repo>`).
- A store containing panel results, i.e. `sparkjury score` has already run. Without them the CLI exits 1.
- `TYPESAFE_API_KEY` only if cloud arbitration is wanted. Without it every disagreement is resolved
  locally and flagged degraded — the run still completes.
- Panel judges reachable (local vLLM by default); `--judges mock` works fully offline.

## Known Risks

- **Silent quality degradation when Jev is absent.** With `--jev auto` and no key, every disagreement
  becomes `source=local, degraded=true`. The run succeeds and the numbers look normal; only
  `summary.n_degraded` and the `degraded` column reveal the discount. Read both before trusting the card.
- **`n_audited == 0` is not "audit passed".** The 5% sample is hash-derived, so small runs frequently
  audit nothing. Absence of audit is absence of evidence, not evidence of correctness.
- **Label/score reconciliation on `outcome`.** The Jev path forces score and pass/fail label to agree,
  which means the reported score is a function of the label, not an independent reading.
- **Jev is unexplainable.** It returns a choice, not a reason; `rationale` records the position and
  confidence, not a justification. See `references/jev-integration.md` §5.
- **Panel tolerance hides near-ties.** Agreement uses spread <= 1, so a 4/4/3 panel is recorded as
  agreement with median 4 — a real disagreement of one level is absorbed without arbitration.

## Reference(s)

- `references/arbitration-rules.md` — vote-pattern routing, degradation semantics, and the four
  documented divergences from the v0.1 architecture document.
- `references/jev-integration.md` — Jev endpoint shape, cost basis, and the unexplainability trade-off.
- `../../standards/scenario-pack/thresholds.yaml` — FROZEN `arbitration` thresholds (authoritative).
- `../../contracts/run-manifest.schema.json` — where `degraded_flags` is defined.
- `../../docs/ARCHITECTURE.md` — harness architecture and data contracts.

## Skill Output

- `arbitration` table in the SQLite store: `trace_id, dimension, final_score, final_label, source,
  degraded, audit_*` plus the panel scores, Jev confidence/raw score, rationale, latency and error.
- `sparkjury arbitrate --json` -> `{summary: {n_traces, n_dimensions, by_source, n_degraded,
  n_audited_dimensions, n_audit_disagreements, n_outcome_fail, mean_final_score}, decisions: [...]}`.
- Run manifest `degradations[]` entry for the ARBITRATE stage whenever any decision was degraded.
- Machine-readable shape: `schemas/io.schema.json`.

## Evaluation Agents Used

- Mock panel judges (`PanelConfig.mock()`): `judge_a=judge_a/mock-qwen`, `judge_b=mock-gemma`,
  `judge_c=mock-step` — offline, deterministic enough for shape assertions.
- Audit judge: the panel's last judge, re-scoring the hash-selected 5% sample.
- Test coverage: `../../tests/test_m4_arbiter.py` (arbitration paths and degradation),
  `../../tests/test_m9_skills_nat.py` (skill structure, frontmatter, wrapper `--help`).

## Data handling

Reads the local SQLite store only — no trace files or external data sources. On a disagreement it
sends a **constructed state text** to the Jev cloud API (`https://api.typesafe.ai/v1/systemone`):
task id, trial, domain, the gold outcome when known, the dimension under arbitration, each judge's
score and rationale, and a transcript truncated to `transcript_width` (default 400 chars). No store
credentials, no file paths, no other runtime state leave the machine. When `TYPESAFE_API_KEY` is
unset nothing is sent anywhere. Panel judging stays on the configured endpoints (local vLLM by
default). The audit judge runs on the same local panel.

## Risk level

write (local store) — mutates the `arbitration` table in the given SQLite store and adds a
`degradations[]` entry to `runs/<run_id>/manifest.json` when any decision is degraded. It does not
delete or rewrite existing traces, verdicts or panel results, and it does not touch the pack.
