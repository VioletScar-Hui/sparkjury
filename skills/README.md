# SparkJury Agent Skills

Eleven skills: six for the evaluation stages, five for scoring governance
(arbitration protocol, judge calibration, prioritization, clarification and pack lifecycle). following the [Agent Skills specification](https://agentskills.io/specification)
and the layout used by the [NVIDIA/skills](https://github.com/NVIDIA/skills) registry.

| Skill | Wraps | Purpose |
|---|---|---|
| `sparkjury-clean` | `sparkjury ingest` + `precheck` | load traces, label environment failures |
| `sparkjury-evalset` | `sparkjury run --stages EVALSET` | fix the exact traces to judge |
| `sparkjury-score` | `sparkjury score` + `arbitrate` | three-judge panel, Jev / local arbitration |
| `sparkjury-cluster` | `sparkjury cluster` | badcase clusters, labels, priority |
| `sparkjury-report` | `sparkjury report` | evidence card (json / md / html) |
| `sparkjury-regress` | `sparkjury regress` | before / after comparison |
| `sparkjury-arbitrate` | `sparkjury arbitrate` | disagreement arbitration protocol (Jev / local / degraded) |
| `sparkjury-calibrate` | `tools/` 桥接脚本（v0.1） | judge reliability profile before scoring starts |
| `sparkjury-prioritize` | `tools/` 桥接脚本（v0.1） | which failure class to fix first + golden-pool override hook |
| `sparkjury-clarify` | `tools/` 桥接脚本（v0.1） | the only pack writer during the clarification stage |
| `sparkjury-govern` | `tools/` 桥接脚本（v0.1） | pack freeze/thaw + decision ledger + golden-pool projections |

Each directory contains:

- `SKILL.md` — frontmatter (`name`, `description`, `license`, `compatibility`, `metadata`) + instructions
- `skill-card.md` — governance metadata: NVIDIA registry template (owner, risk, references, output) + `Data handling` and `Risk level` sections
- `schemas/io.schema.json` — I/O contract for the stage's artifacts (JSON Schema)
- `evals/evals.json` — 3 registry routing/behaviour/guard cases
- `references/` — deep protocol notes loaded on demand
- `BENCHMARK.md` — how to benchmark this skill and what counts as pass

Structure gate: `python3 tools/lint_skills.py --skills-dir skills` (superset of `scripts/validate_skills.py`: adds schemas/evals/pyc checks).
- `scripts/run.py` — thin wrapper around the CLI; extra arguments are passed through

## Install into an agent

Copy or symlink the directories into the agent's skills folder, e.g. for Claude Code:

```
cp -r skills/sparkjury-* ~/.claude/skills/
```

The skills need the `sparkjury` package on the PATH (`uv sync` in this repo, then `uv run`, or `pip install -e .`).

## Validate

```
uv run python scripts/validate_skills.py
```

Checks the frontmatter against the specification (name charset and length, name == directory, description length,
compatibility length, metadata string map, SKILL.md under 500 lines, wrapper script present).

## Sign and publish (NVIDIA registry)

The registry requires a detached OMS signature per skill (`skill.oms.sig`) verifiable against `nv-agent-root-cert.pem`.
`scripts/sign_skills.sh` holds the `model_signing` command lines; signing needs the team's certificate and is done by the
skill-library owner before submission. Product registration goes through a `components.d/` YAML in the registry repo;
`sparkjury.component.yaml` here is the draft.
