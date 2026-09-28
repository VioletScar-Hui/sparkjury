# SparkJury Agent Skills

Six skills, one per evaluation stage, following the [Agent Skills specification](https://agentskills.io/specification)
and the layout used by the [NVIDIA/skills](https://github.com/NVIDIA/skills) registry.

| Skill | Wraps | Purpose |
|---|---|---|
| `sparkjury-clean` | `sparkjury ingest` + `precheck` | load traces, label environment failures |
| `sparkjury-evalset` | `sparkjury run --stages EVALSET` | fix the exact traces to judge |
| `sparkjury-score` | `sparkjury score` + `arbitrate` | three-judge panel, Jev / local arbitration |
| `sparkjury-cluster` | `sparkjury cluster` | badcase clusters, labels, priority |
| `sparkjury-report` | `sparkjury report` | evidence card (json / md / html) |
| `sparkjury-regress` | `sparkjury regress` | before / after comparison |

Each directory contains:

- `SKILL.md` — frontmatter (`name`, `description`, `license`, `compatibility`, `metadata`) + instructions
- `skill-card.md` — governance metadata required by the NVIDIA registry (owner, risk, data handling, side effects)
- `scripts/run.py` — thin wrapper around the CLI; extra arguments are passed through

## 每个技能真正碰哪些模型

上面那张表说的是「技能包了哪几条命令」，不等于「技能会调模型」。六个里只有两个会碰到模型，
其余四个是确定性代码（读库、算数、出文件），把它们说成同一个样子会让 skill card 里的数据流描述失真。

| Skill | 会碰到的模型 | 剩下的部分 |
|---|---|---|
| `sparkjury-clean` | 无 | 导入解析与 precheck 规则都是确定性代码 |
| `sparkjury-evalset` | 无 | 只按 `task_id` / `limit` 挑出要评的 trace |
| `sparkjury-score` | Judge A（8001）、Judge B（8002）、Judge C（StepFun 云，没配 key 退化成 mock）、仲裁 Jev（TypeSafe 云，没配 key 退化成 Judge A 本地仲裁并标降级） | 四个维度的打分规则、仲裁与审计逻辑 |
| `sparkjury-cluster` | Embedding（8003，连不上退化成哈希向量）、簇标签也用 Jev（没配 key 退化成启发式规则） | 聚类算法本身是 sklearn，确定性 |
| `sparkjury-report` | 无 | 卡片内容全部由库里已有的分数算出来 |
| `sparkjury-regress` | 无 | pass^k 从库里算 |

## 本地端点路由表（8001–8004）

上一张表是「技能 → 模型」，这一张是反方向：**节点上四个本地端点各自被谁消费、断了退到哪**。
端口与模型的真源是 `deploy/dgx/common.sh`（`JUDGE_A_PORT` 等五个变量），`scripts/certificate.py`
按它逐行核对本表，改一处不改另一处会红。

| 端点 | 模型（common.sh 默认） | 谁在用 | 端点不可达时 |
|---|---|---|---|
| `127.0.0.1:8001` | `Qwen/Qwen3-30B-A3B-Instruct-2507-FP8` | `sparkjury-score`（Judge A）；Jev 缺 key 时兼本地仲裁人 | harness 健康检查后摘掉或换 mock，记降级 |
| `127.0.0.1:8002` | `nvidia/Nemotron-3.5-Lightning-30B-A3B-NVFP4` | `sparkjury-score`（Judge B） | 同上 |
| `127.0.0.1:8003` | `Qwen/Qwen3-Embedding-0.6B` | `sparkjury-cluster`（badcase 向量化） | 退回离线哈希向量，输出里标 `hashing-512` |
| `127.0.0.1:8004` | `Qwen/Qwen3-8B` | 被测 Agent 本体；`sparkjury agent`（M13）默认的调技能模型 | 不影响评测已有 trace；agent 模式无法运行 |
| 云端（无本地端口） | StepFun `step-3.7-flash` / TypeSafe Jev | `sparkjury-score`（Judge C / 仲裁）、`sparkjury-cluster`（簇标签） | 缺 key 分别退 mock / 本地仲裁 / 启发式标签，全部记降级 |

8004 上的模型是**被测对象**：它不得坐上裁判席——`sparkjury score` 会拒绝裁判模型与库中
`agent_model` 相同的面板（`--allow-self-judge` 是显式逃生口，用了会在 stderr 大声标注）。

这是「技能 → 模型」的方向。反过来还有一层：`src/sparkjury/agent/`（M13）里模型是**调用方**，
它自己决定调哪个技能——工具表里有 `load_skill`（读说明书）和 `run_skill`（执行技能），
跑的时候用哪个模型由 `--model` 决定（节点上默认 `subject`，也就是 8004 上的 Qwen3-8B）。
换句话说，技能调用哪家模型是固定的，而「谁来调技能」是可换的。

每个技能的 `skill-card.md` 里 "Data handling" / "Network" 两行按这张表写，`scripts/certificate.py`
会逐份核对，改了一处不改另一处会红。

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
