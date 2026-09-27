# sparkjury 移植说明（port notes）

> 2026-09-27 由 `eval-agent/skills/clarify/` 迁入 sparkjury，改名 `sparkjury-clarify`。
> 本文件记录**路径适配**与**两边语义差异**。差异只记录，不私自统一。

## 1. 路径适配（硬编码改了哪些）

| 位置 | eval-agent 原态 | sparkjury 现状 | 为什么 |
|---|---|---|---|
| `scripts/pack_scan.py:21-23` | `sys.path.insert(0, PROJECT_ROOT / "scripts")` | `... / "tools"` | 唯一 YAML 实现 `_yaml_lite.py` 在 sparkjury 的 `tools/` |
| `scripts/clarify.py:139` | `pack_dir = project_root() / "scenario-pack"` | `project_root() / "standards" / "scenario-pack"` | pack 位置变了 |
| `scripts/clarify.py` docstring / `--pack-dir` / `--out` 帮助、结尾打印 | 写作 `scripts/_yaml_lite.py`、`scenario-pack/` | `tools/_yaml_lite.py`、`scenario pack/` | 指向真实文件与目录 |
| `scripts/_contracts.py` 新增 `PACK_PREFIX` | 无此常量（各处硬编码 `scenario-pack/...` 字面量） | `PACK_PREFIX = "standards/scenario-pack"` | `proposal.py` 的 14 处 `maps_to` / `target_file` 字面量全部改为 `f"{PACK_PREFIX}/..."`，保证提案指向的路径在本仓库里真实存在（一份指向不存在文件的 pack diff 比没有 diff 更危险） |
| `scripts/proposal.py` | `"scenario-pack/rubrics.yaml"` 等 14 处字面量 | `f"{PACK_PREFIX}/rubrics.yaml"` 等 | 同上 |
| `scripts/_selftest.py` 注释 | 「提案不得落在 scenario-pack 内」 | 「不得写进 pack」+ 点名真实 pack 路径（fixture 仍是 tmp 里的同名副本，未动） | 注释准确性 |
| `SKILL.md` | 输入表 / 红线 / 示例 JSON 写 `scenario-pack/...` | `standards/scenario-pack/...`（或 pack 相对写法） | 同上 |
| `schemas/io.schema.json` | `$id = eval-agent.local/.../clarify/...`；三处路径描述 | `sparkjury.local/.../sparkjury-clarify/schemas/...`；`standards/scenario-pack/...` | 归属 + 真实路径 |
| `evals/evals.json` | `expected_skill = "clarify"` | `"sparkjury-clarify"` | 路由门指向新目录名 |
| `references/*`、`BENCHMARK.md` | `scenario-pack/`、`scripts/lint_skills.py`、`skills/clarify/` | `standards/scenario-pack/`、`tools/lint_skills.py`、`skills/sparkjury-clarify/` | 同上 |

**未改**：`QUESTION_BUDGET=5` / `MIN_SUPPORT=3` / `IMPACT` 影响表 / 五问文案 /
`compile_proposal` 的拒产规则（权重和 ≠1 无数值 diff、方向型只出 pending）一律未动。

## 2. 新增的 sparkjury 规范件

| 文件 | 说明 |
|---|---|
| `scripts/run.py` | 薄包装：`runpy.run_path("clarify.py", run_name="__main__")`。`--questions` / `--answers` / `--selftest` / `--help` 均可用，退出码语义与主脚本一致 |

## 3. 语义差异（只记录，不统一）

### 3.1 pack 的 `IMPACT` 影响表按 pack 目录布局写死

`scripts/_contracts.py:IMPACT` 的键是 pack 内的相对文件（`rubrics.yaml` / `taxonomy.yaml` /
`thresholds.yaml` / `judges.yaml` / `prompts` / `checkers`）。这套键在 sparkjury 的
`standards/scenario-pack/` 下**结构相同**（已实测：同名的 4 个 YAML + `checkers/` + `prompts/`
都在），因此影响面推导无需改动。但注意键是**pack 内相对名**、而 `target_file` 是**仓库相对名**，
两层前缀不同是有意的：`IMPACT` 查表用短键，落进提案的路径用完整路径。

### 3.2 量表与维度（与运行时）

- pack `rubrics.yaml:meta.scale = "0-3"`，四维 `outcome / process / efficiency / risk`。
- sparkjury 运行时 `src/sparkjury/models/verdict.py`：`SCORE_MIN, SCORE_MAX = 0, 4`，
  四维 `outcome / tool_use / efficiency / safety`。
- 本 skill 只改 pack 字段（权重 / method / checklist / hard_rules / pass_k），
  **不产出任何分数**，因此量表差异不影响它的行为；但 Q2/Q4 的提问文案里的
  「四维」指的是 pack 四维，不是运行时四维。README/证据卡若把两者混写会误导。
- `fixability_boost` / `severity_weights` 在 pack 的 `prioritize` 段，运行时没有对应物
  （运行时 `Cluster.priority = size × severity`）。详见 `../sparkjury-prioritize/references/sparkjury-port-notes.md`。

### 3.3 `governance/proposals.json` 默认路径

`--golden-proposals` 是显式参数，无默认值；`sparkjury-govern` 的默认输出目录是
`<repo>/governance/`（该目录在 sparkjury 尚未建，`write_json` 会按需创建）。
**移植未把它默认到 `runs/`** —— 改了会让「govern 的产物在哪」这个问题有两个答案。

## 4. 复现方式

```bash
python3 skills/sparkjury-clarify/scripts/run.py --selftest       # 必须 PASS
python3 skills/sparkjury-clarify/scripts/run.py --questions      # 读真实 pack，打印五问
python3 tools/lint_skills.py --skills-dir skills                 # 本 skill 必须 PASS
```
