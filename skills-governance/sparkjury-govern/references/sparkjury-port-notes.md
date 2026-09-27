# sparkjury 移植说明（port notes）

> 2026-09-27 由 `eval-agent/skills/govern/` 迁入 sparkjury，改名 `sparkjury-govern`。
> 本文件记录**路径适配**与**两边语义差异**。差异只记录，不私自统一。

## 1. 路径适配（硬编码改了哪些）

| 位置 | eval-agent 原态 | sparkjury 现状 | 为什么 |
|---|---|---|---|
| `scripts/govern.py:50-52` | `sys.path.insert(0, PROJECT_ROOT / "scripts")` | `... / "tools"` | 唯一 YAML/hash 实现迁到 `tools/` |
| `scripts/_golden_pool.py:22-24` | 同上 | 同上 | 同上 |
| `scripts/_lifecycle.py:PACK_DIR` | `parents[3] / "scenario-pack"` | `parents[3] / "standards" / "scenario-pack"` | pack 位置变了；与 `tools/pack_freeze.py` 的「根 `scenario-pack/`（不存在）→ `standards/scenario-pack/`」解析一致 |
| `scripts/_lifecycle.py:PACK_FREEZE` | `parents[3] / "scripts" / "pack_freeze.py"` | `parents[3] / "tools" / "pack_freeze.py"` | 唯一 `frozen_hash` 实现迁到 `tools/`，仍是**只调用不复制** |
| `scripts/_lifecycle.py:require_repo_pack()` | 拒绝文案写「`scripts/pack_freeze.py` 管辖的 pack … 仓库内 `scenario-pack/`」 | 文案改为 `tools/pack_freeze.py` … `standards/scenario-pack/` | 报错必须指向真实路径。**守卫语义一字未改**：仍要求 `Path(args.pack_dir).resolve() == PACK_DIR.resolve()`，副本一律退出码 2 且零写盘 |
| `scripts/_golden_pool.py` 新增 `PACK_PREFIX` | 无此常量，`target_file` / `candidate_fields` 硬编码 `scenario-pack/...`（10 处） | `PACK_PREFIX = "standards/scenario-pack"`，10 处字面量改为 `f"{PACK_PREFIX}/..."` | 提案指向的路径必须在本仓库里真实存在 |
| `scripts/_lifecycle.py:cmd_freeze` 的 checkpoint `artifact`、`cmd_history` 的过滤条件 | `f"scenario-pack/{pack_id}"`、`"scenario-pack" in artifact` | `f"{PACK_PREFIX}/{pack_id}"`、`PACK_PREFIX in artifact` | `history` 重建靠这个串识别 freeze 事件 |
| `scripts/govern.py` selftest 断言 | `p["target_file"].startswith("scenario-pack/")` | `startswith(PACK_PREFIX + "/")` | 断言要跟上真实路径 |
| `SKILL.md` / `schemas/io.schema.json` / `references/*` / `BENCHMARK.md` | `scripts/pack_freeze.py`、`scenario-pack/`、`skills/govern/`、`scripts/lint_skills.py` | `tools/pack_freeze.py`、`standards/scenario-pack/`、`skills-governance/sparkjury-govern/`、`tools/lint_skills.py` | 同上 |
| `evals/evals.json` | `expected_skill = "govern"` | `"sparkjury-govern"` | 路由门指向新目录名 |

**未改**：`DECISION_KINDS` / `EVENT_TYPES` / `RATIONALE_REQUIRED` 枚举、`MIN_SUPPORT=2`、
三类投影逻辑、`action_key` 幂等语义、`frozen_at` 由 `pack_freeze.py` 写常量日期，一律未动。

### 关于 `governance/` 默认目录

`DEFAULT_LEDGER = <repo>/governance/events.jsonl`、`DEFAULT_OUT_DIR = <repo>/governance`
保持原样：sparkjury 尚未建这个目录，`write_json` / `append_event` 会按需 `mkdir`。
移植**没有**把它改默认到 `runs/` —— 那会让「治理产物在哪」出现两个答案，
且 `runs/` 在 `.gitignore` 里，账本会被一起忽略掉。

## 2. 新增的 sparkjury 规范件

| 文件 | 说明 |
|---|---|
| `scripts/run.py` | 薄包装：`runpy.run_path("govern.py", run_name="__main__")`。`--selftest` / 各 subcommand / `--help` 均可用 |
| `skill-card.md` 的 `## Data handling` / `## Risk level` | `tools/lint_skills.py` 与 `tests/test_m9_skills_nat.py` 断言的两个小节 |

## 3. 语义差异（只记录，不统一）

### 3.1 `frozen_hash` 与仓库位置无关（好消息，实测）

`tools/pack_freeze.py:compute_hash()` 只覆盖 pack 内**相对路径 + 字节**，
`pack.manifest.json` 被排除。因此同一份 pack 从 eval-agent 的根 `scenario-pack/`
复制到 sparkjury 的 `standards/scenario-pack/`，hash 不变（`3d736734…`）。
回归铁律「两轮同 `pack_hash` 才可比」跨仓库成立。

### 3.2 运行时没有 ledger / 状态机对应物

`DECISION_KINDS`（`accept_card` / `override_priority` / `rename_cluster` /
`reject_proposal` / `approve_pack_diff` / `thaw_pack`）与 `contracts/event-ledger.schema.json`
在 sparkjury 里**只有契约文件、没有生产者**：`src/sparkjury/` 不写 decision 事件，
`sparkjury-report` 的证据卡也不带 approve/reject 交互。
因此本 skill 的黄金池投影在 sparkjury 里目前只能吃**手工 `govern.py ledger append`**
灌进来的事件。移植未给运行时加写账本的代码（超出移植范围，且属于跨模块架构决策）。

### 3.3 分类体系：`F01-F99` + severity/fixability（pack）vs `FailureLabel`（运行时）

见 `../sparkjury-prioritize/references/sparkjury-port-notes.md` §3.1/§3.2。
本 skill 的 `taxonomy_gap` 投影会从 `facts["free_category_ids"]` 里找
**13 起未被占用的最小 F 号**（pack v0.1 用到 F12，故下一个是 F13）；
运行时的 `FailureLabel` 没有编号，也没有 `default_severity` / `fixability_boost` 可校准。
要打通需要一次显式映射决策。

### 3.4 量表 0-3（pack）vs 0-4（运行时）

本 skill 不读分数，只读 pack 的 `taxonomy.default_severity`（1-3）与
`rubrics.dimensions[].weight`（和为 1）。运行时的 `Verdict.score` 是 0-4、
`Cluster.severity` 是加权浮点。两者不交换数据，故无冲突；但若将来要让
「运行时回归结果」驱动黄金池，先把尺度换算规则写进 pack，不要再在 skill 里换算。

## 4. 复现方式

```bash
python3 skills-governance/sparkjury-govern/scripts/run.py --selftest   # 必须 PASS（含路径守卫三条断言）
python3 tools/lint_skills.py --skills-dir skills           # 本 skill 必须 PASS
python3 tools/pack_freeze.py --verify                      # 必须 OK FROZEN（pack 未被碰）
```
