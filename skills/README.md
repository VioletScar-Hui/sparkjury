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

Two tiers, two gates（门禁分层）:

**六个阶段 skill**（`sparkjury-clean/evalset/score/cluster/report/regress`，由 `scripts/gen_skills.py` 生成）——三件套：

- `SKILL.md` — frontmatter (`name`, `description`, `license`, `compatibility`, `metadata`) + instructions
- `skill-card.md` — governance metadata required by the NVIDIA registry
- `scripts/run.py` — thin wrapper around the CLI; extra arguments are passed through

结构门：`python3 scripts/validate_skills.py skills`（11/11 valid，全量适用）。

**五个治理 skill**（`sparkjury-arbitrate/calibrate/clarify/govern/prioritize`，手工维护，勿加入生成器）——在三件套之上再加：

- `schemas/io.schema.json` — I/O contract for the stage's artifacts (JSON Schema)
- `evals/evals.json` — 3 registry routing/behaviour/guard cases
- `references/` — deep protocol notes loaded on demand
- `BENCHMARK.md` — how to benchmark this skill and what counts as pass

结构门：`python3 tools/lint_skills.py --skills-dir skills --only sparkjury-arbitrate,sparkjury-calibrate,sparkjury-clarify,sparkjury-govern,sparkjury-prioritize`
（六件套检查只对治理五个生效；给六个阶段 skill 补六件套是后续 PR 的事，此前对它们跑该 lint 会如实报缺件）。
仓库级检查（pyc / 禁入路径，任何状态可跑）：`python3 tools/lint_skills.py --repo-only`。


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
