---
name: sparkjury-calibrate
description: SparkJury judge calibration (sparkjury-calibrate)：给 LLM 裁判做上岗体检——用校准集在正式打分前测量每个裁判×维度 vs 金标的可靠性，产出裁判画像与 PASS/DEGRADE/FAIL 门禁，并生成 judges.yaml override 提案交 govern。当用户说「裁判准不准」「校准裁判」「judge 可靠性」「裁判画像」「上岗门禁」「agreement 对齐率」「裁判漏判」「over/under 打分偏差」「顺序交换敏感度」「lead judge 选谁」「Jev 不可达谁来兜底」「裁判提示词要不要改」「先校准再打分」「小模型当裁判行不行」时使用；同样用于换裁判模型后重跑体检、某维度裁判争议过大、以及回归前确认尺子有没有变。
license: MIT
compatibility: Python 3.9+ 标准库即可运行（YAML 由 sparkjury 根 tools/_yaml_lite.py 唯一实现解析，PyYAML 优先、子集兜底）；三裁判跑在本地 vLLM 端点，本 skill 本身不调用模型，只消费已产出的投票 JSON 与金标。输入只读。换域/换场景 = 换 standards/scenario-pack，本 skill 引擎不变。
metadata:
  author: "万凌"
  version: "0.1.0"
  product: sparkjury
  tags: "evaluation, judge-calibration, llm-judge, reliability"
allowed-tools: Read Bash Write
---

# sparkjury-calibrate — 先让裁判上场考试，再让裁判上场打分

流水线位置 `ingest → clean → evalset → **calibrate** → score → arbitrate → cluster → prioritize → report → regress`。

本 skill 是全库的差异化核心：**它是唯一一件"在打分之前先怀疑尺子"的事。**

评测 agent 的一切结论最终都来自 LLM 裁判的票。但裁判本身是 LLM，有已知的系统性毛病：
同族自评掉点（JudgeBench，arXiv [2410.12784](https://arxiv.org/abs/2410.12784)）、
位置偏误、倾向于给中间分、对罕见失败模式视而不见。
不先测裁判，等于把整条管线的可信度押在一个没验证过的假设上。

而这个假设失效的方式特别阴险：**一个小模型系统性地漏判某类失败，你不会收到任何报错。**
漏判的 badcase 不进 cluster、不进优先级、不进证据卡，下游全部静默失效，
报告看起来干净漂亮。calibrate 就是唯一能在打分前发现这件事的环节
—— 因为它拿有外部真值的校准集去考裁判，而不是拿裁判去考裁判。

**sparkjury 移植说明（2026-09-27）**：本 skill 由 eval-agent 的 `calibrate` 迁入 sparkjury，
改名 `sparkjury-calibrate` 并在运行时无对应 CLI（净新增，属治理层桥接 skill）。
路径适配：YAML/hash 等根工具从 `<repo>/scripts/` 改为 `<repo>/tools/`，
scenario pack 从根 `scenario-pack/` 改为 `standards/scenario-pack/`。
入口：`python3 scripts/run.py --selftest`（薄包装，内部 exec `scripts/compute_profile.py`）。
与 sparkjury 运行时的语义差异（量表 0-3 vs 运行时 0-4、维度命名、taxonomy id vs FailureLabel）
见 `references/sparkjury-port-notes.md` —— **只记录差异，不做统一**。

**核心立场：calibrate 只诊断，不动手。** 画像说明谁不可靠、不可靠在哪个维度、往哪个方向偏；
改变评测标准（rubric / prompt / judges.yaml）全部以**提案**形式交 govern，人批准才生效。
一个会因为"裁判不行"就自己改评分标准的校准层，已经和被评对象同流合污了。

## 何时用我

- 正式打分前的第一道门：evalset 的 calibration 集就位后必须跑，没有例外。
- 换了任何裁判模型 / 量化 / prompt → 旧画像作废，必须重跑。
- 某个维度三裁判争议特别大（arbitrate 频繁升级 Jev）→ 回来查是不是该维度有裁判在 DEGRADE。
- Jev 不可达，需要确定兜底裁判是谁。
- 有人问"这个小模型当裁判行不行" —— 这个问题只能由画像回答，不能由印象回答。
- pack 从 DRAFT 进 FROZEN 后第一轮运行。

## 输入

> **v0.1 生产者缺位声明**：下表的 calibration 集（`sets.calibration`）、金标票（votes/golds）
> 在 sparkjury 当前运行时**没有生产者**——`sparkjury` 的 evalset 产扁平 trace_id 数组，
> 不产三态集与金标配对。本 skill 现阶段吃按 `schemas/io.schema.json` 制备的 fixture
> （见 BENCHMARK §行为基准），CLI 侧接口需求已提给滨辉（docs/PROPOSAL-skill-governance.md D4）。

| 输入 | 来源 | 契约 |
|---|---|---|
| calibration 集 | evalset 的 `sets.calibration` | 全条目 `has_golden_outcome == true` |
| 金标 | tau2 自带 DB 终态判定（经 ingest 进 `Trace.outcome`，按组件拆开）；`golden_steps_ref` 提供时 process 维也有金标 | `references/reliability-metrics.md` §0 |
| 三裁判试打分 | score 侧在 calibration 集上的预跑投票 | `schemas/io.schema.json#votes` |
| 成对场景的交换顺序重跑 | pack `thresholds.scoring.order_swap: true` + `rubrics.process.pairwise: true` | `schemas/io.schema.json#swap_runs` |
| 裁判配置与阈值 | pack `judges.yaml` / `thresholds.yaml`（FROZEN 只读） | pack |
| 被评 agent 身份 | traces 的 `agent.name` / `agent.model` / 族 | 用于同族红线校验 |

被评 agent 的 model family 与 `judges.yaml#judged_agent_forbidden_families` 有交集 →
**不出画像，直接 fail**（`preflight_failed`）。同族自评不可信，画像建在不合法的样本上没有意义。

## 输出

`judge_profile.json`，包在统一 skill envelope 内：

```
envelope.manifest        五元组（trace_version / pack{pack_hash} / skill_versions / model_versions{三裁判} / outputs+hash）
envelope.degraded_flags  本次体检所有降级标记
envelope.payload
├── judges
│   └── judge_a
│       └── by_dimension
│           └── outcome { agreement, agreement_within_1, n, over_rate, under_rate,
│                          mean_signed_bias, score_variance, order_swap_conflict_rate,
│                          repeat_agreement, latency_p50_ms, latency_p95_ms,
│                          min_n_met, no_gold_for_dimension, note }
├── gates[]           [{ judge, dimension, verdict ∈ PASS/DEGRADE/FAIL/UNKNOWN, reason, threshold_source }]
├── lead_judges       { outcome: { judge, agreement, basis: "ranking_not_gate" }, ... }
├── proposals[]       [{ target_field, current, proposed, evidence, requires: "govern_approval" }]
├── coverage          { cells_total, cells_with_gold, votes_total, unmatched_votes, judges_evaluated }
├── notes             人看的说明
└── provenance        { pack_hash, judge_versions, calibration_task_count, gate_threshold_source }
```

`gates` 由 score / arbitrate 直接消费；`lead_judges` 在 Jev 不可达时决定兜底裁判；
`proposals` 只进 govern。

## Steps

0. **校验输入**：calibration 集非空且全条目有金标（否则回 evalset 修，不在此降级）；
   votes 每条含 judge/dimension/task_id/score；golds 每条含 task_id/dimension/gold_score。
   schema 不符 → `failed` 事件，不猜不补。
1. **同族红线校验**：被评 agent family × `judged_agent_forbidden_families` 有交集 → fail，不出画像。
2. **配准票与金标**：按 `(task_id, dimension)` 配对，只取 `repeat=1` 主票进 agreement 分母。
   配不上的票（无对应金标 / 不在校准集）进 `coverage.unmatched_votes`，**不静默丢弃**。
3. **算 six-cell 指标**：`agreement_with_gold()` + `order_swap_conflict_rate()` + `repeat_agreement()`，
   公式见 `references/reliability-metrics.md`。发 `progress` 事件。
4. **门禁判定**：`render_profile()` 按 `references/gate-policy.md` §2 的顺序规则逐 cell 判。
   阈值一律从 pack `thresholds.calibrate.*` 取；**v0.1 该段不存在** →
   `UNKNOWN` + `calibrate_thresholds_undefined` 告警 + 补阈值的 pack diff 提案。
5. **选 lead judge**：按 `agreement_exact` 降序排（阈值无关），标 `basis: "ranking_not_gate"`。
6. **生成提案**：只提案，不生效。每条必须带 agreement / n / 维度 / 裁判。
7. **落盘**：`judge_profile.json` + `checkpoint` 事件 + `completed` 事件 + run manifest 五元组。
   画像同时作为**黄金决策池的输入之一**交给 govern 观察趋势。


## 可靠性曲线与 ECE（scripts/reliability.py，v0.1 新增）

回答「裁判/仲裁说 80% 把握时到底多少次是对的」。背景：Jev 官方从未公布 ECE，且其
文档自认置信度只是相对偏好；我们有别人没有的真值——黄金池 PM 拍板与确定性 checker。
`python3 scripts/reliability.py --pairs pairs.json` 输出 10-bin 可靠性表 + ECE +
ASCII 曲线 + overconfident/underconfident 判定。红线：n<30 强制标 low_sample
（噪声不当校准结论）、空 bin 不伪造、非法置信度显式拒绝。selftest 含
「Jev 财报实测形态」负例（高置信押错 → 必须判 overconfident）。

## Edge cases

所有降级进 `degraded_flags`，不静默。

| 条件 | 行为 | flag |
|---|---|---|
| `thresholds.calibrate.*` 缺失（v0.1 的常态） | 门禁只给 UNKNOWN（零方差规则除外）；提补阈值提案 | `calibrate_thresholds_undefined` |
| cell 主票数 < `min_n_per_cell`（pack 定义了才有此判；v0.1 未定义时该 cell 归 `thresholds_undefined`） | 该 cell UNKNOWN，**不出 FAIL** | `calibrate_insufficient_n:<judge>:<dim>`（逐 cell 一条） |
| 该维度无金标（process 维最常见） | 该 cell 标 `no_gold_for_dimension`，agreement 类指标留空，顺序交换敏感度仍可用 | `calibrate_dim_without_gold:<dim>` |
| 某裁判在某维度零方差且金标有方差 | DEGRADE（退化裁判，阈值无关规则；门禁顺序上排在 `n < min_n` 之后、阈值缺失之前，见 gate-policy.md §2） | `judge_no_discrimination_<judge>_<dim>` |
| 裁判在某维度系统性偏差 | DEGRADE + 指定 lead judge | `judge_degraded_<judge>_<dim>` |
| 某维度可用裁判 < 2 / panel 剩 2 | **v0.1 未实现**（`dimension_insufficient_panel` / `panel_reduced_to_2` 两个 flag 不发出）；该级联在 gate-policy.md §4 定义，由 score/arbitrate 侧落地 | — |
| 裁判模型版本缺失 | **v0.1 未实现"拒绝出画像"**：`model_versions` / `provenance.judge_versions` 如实记（缺 model 的裁判记字符串 `"None"`，肉眼可查），不静默省略 | — |
| Jev 不可达 | 兜底裁判取该维度 PASS 裁判中 agreement 最高者；PASS 为空则 blocked 请求人。`jev_unreachable` 由 pack `thresholds.arbitration.jev_fallback` 写死、arbitrate 侧记，不在本 skill 产出 | — |

## 红线（违者即 fail）

- **不得因裁判表现差而自行改 rubric / prompt / judges.yaml。** 那是改评测标准，必须人批准。
  提案是 observations + 建议，不是 actions。
- **不得跳过校准直接打分。** 放弃校准 = 放弃唯一的外部基准，后续分数无法区分
  "被评 agent 的问题"和"裁判的问题"。
- **不得把裁判互投当金标。** 用 judge 的票去考 judge 是循环论证，画像会系统性偏乐观，
  且要等回归才暴露。
- **不得用被评 agent 补裁判位。** 不做 agent 自己打分自己改。
- **不得自造阈值。** pack 没定义就是 UNKNOWN + 告警 + 提案，不许在代码里埋一个"看起来合理"的数。
- **不得删 panel 成员。** 移除裁判是改 judges.yaml，同红线第一条。
- 不写任何密钥/令牌。

## 停止条件

- 被评 agent 与任一裁判同族。
- calibration 集空，或含无金标条目。
- 某维度可用裁判 < 2，或 panel < 2。
- 用户要求"跳过校准，直接跑分"——说明代价后由人决定，不由本 skill 决定。
- 用户要求"裁判不行就把 rubric 改松一点"。

## TODO（待 pack v0.2）

- `thresholds.calibrate.min_n_per_cell`、`agreement_min`、`over_under_max`：门禁三条阈值，
  v0.1 缺失导致门禁只能给 UNKNOWN。这是本 skill 最大的待补项。
- `thresholds.calibrate.judge_repeats_handling`：重复票进不进分母（当前按 SKILL.md §0 只取主票，
  需 pack 显式背书）。
- `thresholds.calibrate.multiple_comparison_correction`：12 个 cell 的多重比较校正策略。

## 相关文件

- `references/sparkjury-port-notes.md` — 2026-09-27 移植适配清单 + 与 sparkjury 运行时的语义差异（量表 0-3 vs 0-4、process/risk vs tool_use/safety），只记录不统一。
- `references/reliability-metrics.md` — 六个指标的定义/公式/最小样本量推导/方差注意事项。
- `references/gate-policy.md` — 四态语义、降级路径、lead judge 机制、与 arbitrate 的联动、提案边界。
- `scripts/run.py` — 薄包装（sparkjury 规范）：exec `scripts/compute_profile.py`，参数原样透传。
- `scripts/compute_profile.py` — `agreement_with_gold()` + `render_profile()` 骨架，`--selftest` 内嵌
  6 票对 4 金标的 fixture。模块拆分（单文件 600 行上限，`.claude/rules/coding.md`）：

  | 文件 | 职责 |
  |---|---|
  | `scripts/compute_profile.py` | CLI 入口 + pack 元信息读取 + envelope 组装；re-export 其余模块的公开符号 |
  | `scripts/_metrics.py` | 指标层：六个可靠性指标 + 主票筛选（重复票不进 agreement 分母）+ 四维量表定义 |
  | `scripts/_gates.py` | 门禁层：四态判定（顺序规则）+ 画像渲染 + lead judge 排序 + 提案 |
  | `scripts/_profile.py` | 画像构造层：输入校验 + 同族红线 + `build_profile()` 纯函数入口 |
  | `scripts/_contracts.py` | 输入契约层：异常分级、阈值读取、event ledger 事件生成 |
  | `scripts/_selftest.py` | `--selftest` 的 6 票对 4 金标 fixture 与断言 |

  注：本 skill **不复制** evalset 的 YAML 子集解析器 —— 跨 skill 复制解析器会让两份实现各自漂移，
  而 pack 的读法必须全库唯一。技能自包含性靠"输入约定"（阈值传 JSON 视图）保证，不靠复制代码。
