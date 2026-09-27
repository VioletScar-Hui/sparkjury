# Skill card: sparkjury-score

| Field | Value |
|---|---|
| Owner | SparkJury team (DGX Spark Hackathon, 3rd edition) |
| Version | 0.1.0 |
| Product | SparkJury agent evaluation harness |
| Underlying command | `sparkjury score + arbitrate` |
| Risk level | write (local store) |
| Data handling | Sends trace excerpts to the three judge endpoints: Judge A and Judge B on local vLLM by default, Judge C on the StepFun cloud API (a mock judge stands in when `STEPFUN_API_KEY` is unset). Disagreements go to the Jev cloud API (TypeSafe) when `TYPESAFE_API_KEY` is set, and fall back to local Judge A arbitration marked degraded otherwise. Nothing else leaves the machine. |
| Network | Required for scoring: Judge A / Judge B (local vLLM), Judge C (StepFun). Optional for arbitration: Jev (TypeSafe). |
| Side effects | Writes to the given SQLite store and `runs/<run_id>/` |
| Evaluation dataset | `../../data/samples/` (tau2 + OTel samples), `../../tests/` |
| Signature | `skill.oms.sig` to be produced with `model_signing` before publishing (see `../../scripts/sign_skills.sh`) |
