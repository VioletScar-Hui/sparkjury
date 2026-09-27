# Skill card: sparkjury-evalset

| Field | Value |
|---|---|
| Owner | SparkJury team (DGX Spark Hackathon, 3rd edition) |
| Version | 0.1.0 |
| Product | SparkJury agent evaluation harness |
| Underlying command | `sparkjury run --stages EVALSET` |
| Risk level | write (local store) |
| Data handling | Reads the local SQLite store only, to pick which traces get judged. No model is called. |
| Network | None. |
| Side effects | Writes to the given SQLite store and `runs/<run_id>/` |
| Evaluation dataset | `../../data/samples/` (tau2 + OTel samples), `../../tests/` |
| Signature | `skill.oms.sig` to be produced with `model_signing` before publishing (see `../../scripts/sign_skills.sh`) |
