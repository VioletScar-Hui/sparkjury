# sparkjury 移植说明（port notes）

> 2026-09-27 由 `eval-agent/skills/calibrate/` 迁入 sparkjury，改名 `sparkjury-calibrate`。
> 本文件记录**路径适配**与**两边语义差异**。差异只记录，不私自统一 —— 统一属于架构决策，
> 要走 `DECISION_LOG.md`，不由一个 skill 的移植顺手做掉。

## 1. 路径适配（硬编码改了哪些）

| 位置 | eval-agent 原态 | sparkjury 现状 | 为什么 |
|---|---|---|---|
| `scripts/_contracts.py:20` | `sys.path.insert(0, PROJECT_ROOT / "scripts")` | `... / "tools"` | sparkjury 的根工具（`_yaml_lite.py`）在 `tools/`，不在 `scripts/` |
| `scripts/compute_profile.py` docstring / `_pack_forbidden_families` docstring | 权威实现写作 `scripts/_yaml_lite.py` | `tools/_yaml_lite.py` | 同上（注释与报错信息必须指向真实文件） |
| `--pack` 帮助串 | 「scenario-pack 目录」 | 「scenario pack 目录；sparkjury 仓库内即 `standards/scenario-pack/`」 | pack 位置变了 |
| `SKILL.md` compatibility | 「换域/换场景 = 换 scenario-pack」 | 「换域/换场景 = 换 `standards/scenario-pack`」 | 同上 |
| `schemas/io.schema.json` `$id` | `https://eval-agent.local/skills/calibrate/io.schema.json` | `https://sparkjury.local/skills/sparkjury-calibrate/schemas/io.schema.json` | 归属与目录名 |
| `evals/evals.json` `expected_skill` | `calibrate` | `sparkjury-calibrate` | 路由门必须指向新目录名 |
| `BENCHMARK.md` | `scripts/lint_skills.py` / `skills/calibrate/...` / `--pack scenario-pack` | `tools/lint_skills.py` / `skills/sparkjury-calibrate/...` / `--pack standards/scenario-pack` | 同上 |

**未改**：`--pack` 仍是可选参数且无默认值（画像绑不绑 pack 由调用方决定，
`calibrate_pack_unbound` flag 是这个决定的诚实记录）。字段名、指标公式、门禁顺序规则一律未动。

## 2. 新增的 sparkjury 规范件

| 文件 | 说明 |
|---|---|
| `scripts/run.py` | 薄包装：`runpy.run_path("compute_profile.py", run_name="__main__")`，参数原样透传。`--help` 退出码 0（validator 与参数化测试要求） |
| `skill-card.md` 的 `## Data handling` / `## Risk level` | `tools/lint_skills.py` 与 `tests/test_m9_skills_nat.py` 断言的兩個小节 |

## 3. 语义差异（只记录，不统一）

### 3.1 评分量表：0-3（pack）vs 0-4（运行时）

- pack 侧：`standards/scenario-pack/rubrics.yaml:meta.scale = "0-3"`，
  本 skill 的 `schemas/io.schema.json#/$defs/vote.score` 也写着「0-3 有序量表」。
- sparkjury 运行时：`src/sparkjury/models/verdict.py` → `SCORE_MIN, SCORE_MAX = 0, 4`；
  `src/sparkjury/integrations/nat_eval.py` 用 `mean(final_score / 4)` 归一到 0..1。
- **后果**：`agreement_within_1`（±1 口径）在两把尺子上不是同一个宽容度 ——
  0-3 尺上 ±1 覆盖 2/3 的量程，0-4 尺上 ±1 覆盖 2/5。`references/reliability-metrics.md`
  已说明该口径对尺子敏感；在 0-4 尺上跑本 skill 前必须重述 ±1 的语义。
- **移植未做任何换算**：本 skill 只比对「裁判票 vs 金标」两列数是否相等，
  不假设量程，因此 0-4 的输入也能算；但 `provenance.agreement_tolerance = 0` 的主口径
  与 ±1 辅口径的**解释**要按实际量程重写。

### 3.2 维度命名：process/risk（pack）vs tool_use/safety（运行时）

| pack（`rubrics.yaml`） | sparkjury 运行时（`Dimension` 枚举） |
|---|---|
| `outcome` | `outcome`（同名） |
| `process` | `tool_use`（近似，语义更窄：只覆盖工具选择/参数/顺序） |
| `efficiency` | `efficiency`（同名，但运行时无「只扣不加分」规则） |
| `risk` | `safety`（近似，运行时不含「不可逆操作前无确认」这条 checklist） |

本 skill 按 `(judge, dimension)` 字符串配对，**不认维度白名单**（未知维度会被
`_validate` 拒绝并报错），所以维度换名会直接报错而不是静默算错 —— 这是有意的。
要接运行时的 `tool_use` / `safety`，需要一次显式的维度映射决策，不是移植时顺手改名。

### 3.3 金标来源

pack 的金标来自 tau2 DB 终态判定与 `golden_steps_ref`；sparkjury 运行时把
`expected_output_obj` 做精确匹配后填 `Trace.outcome.gold["expected"]`，
`success` 只在完全相等时为 `True`。两处都不接受 `llm_judge` 作 `gold_source`
（本 skill `_validate` 会把 `llm_judge:*` 判为 PreflightError），这条红线一致。

## 4. 复现方式

```bash
python3 skills/sparkjury-calibrate/scripts/run.py --selftest     # 必须 PASS
python3 tools/lint_skills.py --skills-dir skills                 # 本 skill 必须 PASS
```
