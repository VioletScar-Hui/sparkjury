# Skill card: sparkjury-clean

| Field | Value |
|---|---|
| Owner | SparkJury team (DGX Spark Hackathon, 3rd edition) |
| Version | 0.1.0 |
| Product | SparkJury agent evaluation harness |
| Underlying command | `sparkjury ingest + precheck` |
| Risk level | write (local store) |
| Data handling | Reads local trace files and writes them into a local SQLite store. No model is called: ingestion and the precheck rules are deterministic code, so no trace content leaves the machine. |
| Network | None. |
| Side effects | Writes to the given SQLite store and `runs/<run_id>/` |
| Evaluation dataset | `../../data/samples/` (tau2 + OTel samples), `../../tests/` |
| Signature | `skill.oms.sig` to be produced with `model_signing` before publishing (see `../../scripts/sign_skills.sh`) |
