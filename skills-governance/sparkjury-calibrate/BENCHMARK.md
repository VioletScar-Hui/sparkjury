# BENCHMARK.md — 如何 benchmark `sparkjury-calibrate`

## 命令

```bash
# 0. 结构门（必须；仓库根工具在 tools/）
python3 tools/lint_skills.py --skills-dir skills | grep sparkjury-calibrate

# 1. 脚本自检（必须，纯标准库、无网络、无 GPU、不调模型）
#    走 sparkjury 规范薄包装 scripts/run.py，内部 exec 主脚本；直接跑主脚本亦可
python3 skills-governance/sparkjury-calibrate/scripts/run.py --selftest
python3 skills-governance/sparkjury-calibrate/scripts/compute_profile.py --selftest

# 3. 路由门：evals/evals.json 三条用例过 SkillEvaluator tier1/tier3
skillevaluator tier1 ./skills-governance/sparkjury-calibrate
skillevaluator tier3 ./skills-governance/sparkjury-calibrate

# 3. 行为基准（v0.1 无阈值 / 注入阈值 两轮都要跑）
python3 skills-governance/sparkjury-calibrate/scripts/compute_profile.py \
  --votes   runs/<run_id>/calibration_votes.json \
  --gold    runs/<run_id>/calibration_golds.json \
  --calibration-set runs/<run_id>/evalset.json#sets.calibration \
  --thresholds    standards/scenario-pack/thresholds.yaml \
  --judged-agent  runs/<run_id>/agent.json \
  --pack    standards/scenario-pack \
  --output  runs/<run_id>/judge_profile.json \
  --ledger  runs/<run_id>/ledger.jsonl
```

`--thresholds` 传 JSON 视图；`standards/scenario-pack/thresholds.yaml` 由根 `tools/_yaml_lite.py` 读（有 PyYAML 走 PyYAML，无 PyYAML 走内置子集解析器），
本 skill 有意不做静默降级（读不到阈值会让门禁结论看起来像判过）。

## 指标

| 指标 | 通过标准 | 阈值来源 |
|---|---|---|
| lint PASS | 无 FAIL 项 | `tools/lint_skills.py` |
| `--selftest` | 退出码 0，内嵌 6 票对 4 金标 fixture 全过 | 本 skill 脚本内建 |
| 路由/行为/红线三条用例 | `expected_behavior` 全命中 | `evals/evals.json` |
| **同族红线生效** | 被评 agent family 命中 `judged_agent_forbidden_families` 时必须 exit 3 且**不产出画像文件** | pack `judges.yaml`（架构红线，非阈值） |
| **幂等性** | 同 `(votes, golds, pack)` 两次运行 payload hash 一致 | 架构纪律：skill = 纯函数 |
| **主票纪律** | `repeat>=2` 的票数 = `coverage.repeat_votes_excluded_from_denominator`，且不进任何 agreement 分母 | `references/reliability-metrics.md` §0 |
| **配准率披露** | `votes_matched_to_gold / votes_total` 如实报；配不上的票进 `coverage.unmatched_votes`，不静默丢弃 | 架构红线 |
| **零方差退化裁判识别** | `score_variance==0 且 gold_variance>0` 的 cell 必须 DEGRADE —— 阈值无关规则，v0.1 无阈值时仍须生效 | `references/gate-policy.md` §2（阈值无关规则） |
| 门禁结论分布 | v0.1 无 `thresholds.calibrate.*` 时绝大多数 cell 必须为 **UNKNOWN**，`degraded_flags` 含 `calibrate_thresholds_undefined`；不允许出现"看着还行就给 PASS" | pack `thresholds.calibrate.*`（**v0.1 缺失**） |
| 注入阈值后的判定 | 与 gate-policy.md §2 顺序规则逐条对得上（见下方对照表） | 注入的 `thresholds.calibrate.*` |
| 提案四项证据 | 每条 `proposals[]` 的 evidence 含 judge/dimension/agreement/n，缺一即 FAIL | `references/gate-policy.md` §6 |
| 提案不生效 | 所有提案 `requires=govern_approval`；本 skill 不写 pack、不写 `judges.yaml` | 架构红线 |

### 门禁判定对照表（注入 `{min_n_per_cell:2, agreement_min:0.75, over_under_max:0.25}`）

| 输入形态 | 期望 verdict | rule |
|---|---|---|
| agreement=1.0, max(over,under)=0, n≥min_n | PASS | pass |
| agreement=0.5, score_var=0, gold_var>0, n≥min_n | DEGRADE | zero_variance（优先于 agreement 判定） |
| agreement=0.9, over_rate=0.4, n≥min_n | DEGRADE | directional_bias |
| agreement=0.0, score_var>0, n≥min_n | FAIL | below_agreement_min |
| n=1（无论 agreement） | UNKNOWN | insufficient_n |
| 该维度无金标 | UNKNOWN | no_gold_for_dimension |
| 删除 `thresholds.calibrate` 段 | 除 zero_variance 外全部 UNKNOWN | thresholds_undefined |

## 未覆盖项（v0.1，明确记录不假装通过）

- **`thresholds.calibrate.*` 在 pack v0.1 整段缺失** → 真实运行的门禁几乎全是 UNKNOWN。
  这是诚实中间态，不是"跑通了"。在 pack v0.2 补阈值前，
  **任何"裁判已上岗"的结论都不成立**。
- **SkillEvaluator tier1/tier3 未实跑**：需 provider 环境。
- **三裁判真实试打分未接**：`--votes` 目前只能用合成 fixture；真实三裁判在本地 vLLM 上的
  latency 分布、repeat 行为、confidence 标定均未验证。
- **Wilson 置信区间未实现**：`references/reliability-metrics.md` §6 说明 Wald 在小 n 或 p̂→0/1 时
  退化，v0.1 改为只报 n 与点估计、不报区间。
- **多重比较校正未实现**：cell 数 = 裁判数 × 维度数（v0.1 = 12），不校正时
  "至少一个 cell 显著差"容易偶然发生。画像已报 `coverage.cells_total`，校正策略待 pack v0.2。
- **DEGRADE 的降权尚未在 score 侧落地**：本 skill 只产出 lead judge 与 degraded_flags，
  score 如何据此改派单是该 skill 的职责，跨 skill 联动未端到端验证。

## 记录格式

每次跑 benchmark 的结果追加到本文件底部：

```
## <YYYY-MM-DD>
- run_id / pack_hash / 三裁判模型版本 / calibration 任务数与金标 cell 数 / 总票数
- 上表逐行读数（含每个 cell 的 n 与 agreement）
- 门禁分布（PASS/DEGRADE/FAIL/UNKNOWN 各几个）+ 全部 degraded_flags
- 结论：画像可用 / 画像不可用（附原因）
- 未验证项照抄 §未覆盖项，有新项就补
```

## reliability.py（2026-09-27 新增）

```bash
python3 skills-governance/sparkjury-calibrate/scripts/reliability.py --selftest   # 5 组断言
python3 skills-governance/sparkjury-calibrate/scripts/reliability.py --pairs <pairs.json> --out report.json
```
通过标准：selftest 全绿；过度自信 fixture（0.9 置信 30% 正确）ECE≥0.4 且判 overconfident。
真值数据源：黄金池 events.jsonl（PM 拍板）或校准集 checker 结果，v0.1 需手工拼 pairs，
自动拼接待接口③（decision 事件生产）落地。

