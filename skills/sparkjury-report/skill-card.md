# Skill card: sparkjury-report

| Field | Value |
|---|---|
| Owner | SparkJury team (DGX Spark Hackathon, 3rd edition) |
| Version | 0.1.0 |
| Product | SparkJury agent evaluation harness |
| Underlying command | `sparkjury report` |
| Risk level | read/write (local files) |
| Data handling | Reads the local SQLite store and writes `card.json` / `card.md` / `card.html` under `runs/<run_id>/card/`. No model is called. |
| Network | None. |
| Side effects | Writes to the given SQLite store and `runs/<run_id>/` |
| Evaluation dataset | `../../data/samples/` (tau2 + OTel samples), `../../tests/` |
| Signature | `skill.oms.sig` to be produced with `model_signing` before publishing (see `../../scripts/sign_skills.sh`) |
