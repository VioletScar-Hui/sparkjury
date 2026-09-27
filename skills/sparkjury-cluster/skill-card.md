# Skill card: sparkjury-cluster

| Field | Value |
|---|---|
| Owner | SparkJury team (DGX Spark Hackathon, 3rd edition) |
| Version | 0.1.0 |
| Product | SparkJury agent evaluation harness |
| Underlying command | `sparkjury cluster` |
| Risk level | write (local store) |
| Data handling | Reads the local SQLite store. Badcase text goes to the embedding endpoint (Qwen3-Embedding on local vLLM by default, hashing embedder as the offline fallback) and cluster representatives go to the Jev cloud API for labelling when `TYPESAFE_API_KEY` is set; otherwise labels fall back to heuristic rules. |
| Network | Optional: local embedding endpoint, Jev (TypeSafe). |
| Side effects | Writes to the given SQLite store and `runs/<run_id>/` |
| Evaluation dataset | `../../data/samples/` (tau2 + OTel samples), `../../tests/` |
| Signature | `skill.oms.sig` to be produced with `model_signing` before publishing (see `../../scripts/sign_skills.sh`) |
