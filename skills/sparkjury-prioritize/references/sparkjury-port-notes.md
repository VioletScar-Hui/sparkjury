# sparkjury 移植说明（port notes）

> 2026-09-27 由 `eval-agent/skills/prioritize/` 迁入 sparkjury，改名 `sparkjury-prioritize`。
> 本文件记录**路径适配**与**两边语义差异**。差异只记录，不私自统一。

## 1. 路径适配（硬编码改了哪些）

| 位置 | eval-agent 原态 | sparkjury 现状 | 为什么 |
|---|---|---|---|
| `scripts/ranking.py:23-25` | `sys.path.insert(0, PROJECT_ROOT / "scripts")` | `... / "tools"` | 根工具 `_yaml_lite.py` 在 sparkjury 的 `tools/` |
| `scripts/prioritize.py:55-57` | 同上 | 同上 | 同上 |
| `scripts/pack_binding.py:load_pack_freeze()` | 向上逐级找 `base / "scripts" / "pack_freeze.py"` | `base / "tools" / "pack_freeze.py"` | `frozen_hash` 唯一实现迁到 `tools/`；仍是**只 import 不自造** |
| `scripts/_selftest.py` | 「取不到 scripts/pack_freeze.py 的 compute_hash」 | 「取不到 tools/pack_freeze.py 的 compute_hash」 | 报错信息指向真实文件 |
| `SKILL.md` | `scenario-pack/thresholds.yaml` / 根 `scripts/pack_freeze.py` | `standards/scenario-pack/thresholds.yaml` / 根 `tools/pack_freeze.py` | 位置变了 |
| `schemas/io.schema.json` | `$id = https://eval-agent.local/skills/prioritize/...`；阈值路径写作 `scenario-pack/thresholds.yaml` | `https://sparkjury.local/skills/sparkjury-prioritize/schemas/...`；`standards/scenario-pack/thresholds.yaml` | 归属 + 真实路径 |
| `evals/evals.json` | `expected_skill = "prioritize"` | `"sparkjury-prioritize"` | 路由门指向新目录名 |
| `BENCHMARK.md` | `scripts/lint_skills.py` / `skills/prioritize/...` | `tools/lint_skills.py` / `skills/sparkjury-prioritize/...` | 同上 |

**未改**：`--pack` 仍无默认值；公式、排序键 `(-priority, category_id)`、override 匹配键
（`taxonomy_id` 权威 / `target` 兼容路径）、拒排语义一律未动。`--thresholds` 是显式参数，
不提供 pack 默认路径 —— 「缺系数就拒排」的纪律依赖调用方显式给阈值文件。

## 2. 新增的 sparkjury 规范件

| 文件 | 说明 |
|---|---|
| `scripts/run.py` | 薄包装：`runpy.run_path("prioritize.py", run_name="__main__")`，`--selftest` / `--help` 均可 |

## 3. 语义差异（只记录，不统一）

### 3.1 失败分类体系：taxonomy `F01-F99`（pack）vs `FailureLabel` 枚举（运行时）

- pack：`standards/scenario-pack/taxonomy.yaml` 的 `categories[]`，每条含
  `id`(F\d\d) / `name` / `definition` / `typical_dimension` / `default_severity` /
  `fixability_boost`（在 `thresholds.yaml:prioritize` 里）。
- sparkjury 运行时：`src/sparkjury/cluster/taxonomy.py` 的 `FailureLabel` 枚举，
  9 个值：`loop` / `wrong_tool` / `hallucinated_info` / `missing_lookup` /
  `unauthenticated_action` / `missing_confirmation` / `wrong_args` / `premature_stop` /
  `policy_violation`（+ `OTHER`），由正则启发式从 badcase 文本判定。
- **后果**：`compute_priority()` 的第一个动作就是 `cid = str(cluster.get("label"))`，
  然后拿它去 `fixability_boost[cid]` 查系数。运行时的 `label` 是 `wrong_tool` 这种字符串，
  在 pack 表里查不到 → 该类进 `rejected_candidates`，**不会猜一个系数顶上**。
  这是安全失效（显式拒排），但意味着「直接把运行时 `clusters.json` 喂进来」只会得到
  一片拒排。要打通需要一次显式的 `FailureLabel → F\d\d` 映射决策（属 pack 层，
  不是本 skill 能自己加的）。

### 3.2 severity：`severity_max`（pack 序数 1-3）vs `severity`（运行时加权浮点）

| | pack 侧（本 skill 期望） | sparkjury 运行时（`Cluster` 模型） |
|---|---|---|
| 字段 | `severity_max` | `severity` |
| 含义 | 类内最大 `default_severity`（taxonomy 的 1/2/3） | `sum of weights of failed dimensions` 的成员均值 |
| 尺度 | 离散 1-3 | 连续 0-1（按 `rubrics.yaml` 权重和折算） |
| 用途 | `severity_weights[str(severity_max)]` = {3:1.0, 2:0.6, 1:0.3} | 运行时自己算 `priority = size × severity` |

`severity_weights` 只认字符串键 `"1"/"2"/"3"`，运行时给 0.42 会拒排
（「未在 pack 定义」）。**移植未做任何尺度换算** —— 换算等于偷偷改 pack 的权重语义。

### 3.3 优先级公式不同（同一个词，两个定义）

- 本 skill：`priority = frequency_norm × severity_weight × fixability_boost`
  （三系数全部来自 pack，缺一即拒排）。
- 运行时：`Cluster.priority = size × severity`（启发式，无 fixability 项）。
- 同名不同义。`report` 证据卡若直接打印 `Cluster.priority`，读的人也读不出
  「可修复性先验」这一项。移植未统一，仅在 `references/priority-formula.md` 说明口径。

### 3.4 分母

- 本 skill 优先级：显式 `--total-badcases` > 同目录 `cluster_report.json` 的
  `total_badcases` > 累加 `clusters.size`；分母必须含 F99（未分类）。
- 运行时 `share = size / n_badcases`（`ClusterRun.n_badcases` 是全部 badcase）。
- 两者语义一致（都含未分类），但字段名不同：`total_badcases` vs `n_badcases`。

## 4. 复现方式

```bash
python3 skills/sparkjury-prioritize/scripts/run.py --selftest     # 必须 PASS
python3 tools/lint_skills.py --skills-dir skills                 # 本 skill 必须 PASS
```
