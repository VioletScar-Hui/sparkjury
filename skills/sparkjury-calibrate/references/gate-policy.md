# gate-policy.md — 裁判上岗门禁：三态语义、降级路径、lead judge 选择、与 arbitrate 的联动

calibrate 的判决策略。`references/reliability-metrics.md` 定义"怎么量"，
本文件定义"怎么判"与"判完之后谁动"。
**本文件随 pack 一起被 `frozen_hash` 覆盖**，修改它 = 改评测标准 = pack 新版本。

---

## 1. 门禁只针对 (judge × dimension) cell，不针对裁判整体

一个裁判在 outcome 维可靠、在 risk 维漏判，是常见且可用的形态。
按裁判整体 PASS/FAIL 会同时丢掉好维度和放过坏维度。
所以画像和门禁的最小单元都是 cell：`(judge_a, outcome)`。

## 2. 四个状态

| verdict | 语义 | score 侧的动作 |
|---|---|---|
| **PASS** | 该组合可作正式裁判 | 正常派单 |
| **DEGRADE** | 可用，但该维度须降权 | 该维度改由 lead judge 主导；`degraded_flags` 记 `judge_degraded_<judge>_<dim>`，report 必须点名 |
| **FAIL** | 不得使用该裁判 × 该维度 | score 不得把该裁判派到该维度 |
| **UNKNOWN** | 证据不足，无法判定 | 按保守原则处理：不视为 PASS。**UNKNOWN 不等于 PASS，缺失阈值时尤其不是** |

### 判定规则（按序取第一个命中的）

```
前置：该 cell 无金标              → UNKNOWN  (no_gold_for_dimension)
前置：n < min_n_per_cell         → UNKNOWN  (insufficient_n)
Var(s) == 0 且 Var(g) > 0        → DEGRADE  (退化裁判：零判别力，agreement 是刷出来的)
                                       ↑ 阈值无关规则，必须排在「门禁阈值缺失」之前，
                                         否则 v0.1（阈值整段缺失）下它永远不触发。
前置：门禁阈值缺失               → UNKNOWN  (thresholds_undefined)
──────────────────────────────────────────────────────────────
agreement_exact < agreement_min  → FAIL     (低于门禁线，不得作正式裁判)
max(over_rate, under_rate) > over_under_max
                                 → DEGRADE  (系统性单向偏差)
其余                             → PASS
```

三条设计说明：

1. **UNKNOWN 排在所有阈值判定之前。** 缺阈值时给 UNKNOWN 而不是"看起来没问题就放行" ——
   后者会让一整个 v0.1 的运行都建立在没判过的裁判上。
2. **零方差 DEGRADE 是阈值无关规则。** `Var(s)==0 且 Var(g)>0` 不需要任何 pack 阈值即可识别，
   所以即使 `thresholds.calibrate.*` 整段缺失，这条仍然生效。这是 v0.1 唯一能给出的实判。
3. **DEGRADE 与 FAIL 判据不同源。** DEGRADE 可以是"大体对但方向偏"，FAIL 是"低于门禁线"。
   把两者合成一档会逼使用人在坏裁判和不坏裁判之间二选一。

### v0.1 的实际状态

`thresholds.calibrate.min_n_per_cell` / `agreement_min` / `over_under_max`
**在 pack v0.1 全部不存在**。因此 v0.1 的绝大多数 cell 只能得 UNKNOWN，
外加 `calibrate_thresholds_undefined` 告警，外加一条补阈值的 pack diff 提案。
这不是缺陷，是**诚实的中间态**：宁可说"没判"，不说"过了"。

## 3. DEGRADE 的降权机制：lead_judge_selection

`judges.yaml#lead_judge_selection` 写明"由 calibrate 产出的裁判可靠性画像决定，不写死"。
具体机制：

```
某维度若存在 DEGRADE 裁判：
  → 该维度指定 lead judge = 画像中该维度 agreement_exact 最高的 PASS/UNKNOWN 裁判
  → 三裁判分歧且升级仲裁时，lead judge 的票在 arbitrate 的第 3 条裁决依据
    （"与 rubrics checklist 字面更相符"仍无法分辨时）中优先
  → 进入 run manifest 的 degraded_flags，并写进 report
```

**关键限制：lead judge 是排序结果，不是门禁结论。**
`agreement_exact` 的排序不需要任何阈值，所以即使门禁全 UNKNOWN，lead judge 仍可给出。
但报告里必须写明"这是排序不是门禁"，否则读者会把一个没人判过的裁判当成冠军用。

## 4. 降级路径（progressive degradation）

门禁结果会级联，必须逐级往下走而不是一步跳到"整个流程停":

```
Step 1  cell FAIL
        → 该维度可用裁判集合收缩。剩余 PASS 裁判 >= 2：多数表决仍成立，继续。
        → 剩余可用裁判 == 1：该维度无法做多数表决（thresholds.arbitration 的
          majority_with_golden / without_golden / split 全部无从触发）。
          → 升级人工评审，停止条件触发，不得用单裁判独裁。

Step 2  裁判在所有维度 FAIL
        → 从 panel 移除。panel 剩 2 个裁判：可继续，但票数 2:0/0:2/1:1，
          1:1 即 split → 升级 Jev。记 degraded flag `panel_reduced_to_2`。
        → panel 剩 1：停止。**不得用被评 agent 自评补位**（红线：不做 agent 自己打分自己改；
          同族自评掉点见 reliability-metrics.md §8）。

Step 3  全 panel FAIL / 全 UNKNOWN
        → 不出画像结论，只出"画像不可用"报告 + 补阈值的 pack diff 提案。停止并请求人。
```

任何一步都不得"因为赶时间跳过校准直接打分"—— 跳过意味着放弃唯一的外部基准，
后面所有分数都无法判断是不是裁判造成的。

## 5. 与 arbitrate 的联动

`thresholds.arbitration` 规定 `jev_fallback: "Jev 不可达 → calibrate 画像中的 lead judge 裁决"`。
这条把 calibrate 变成了 Jev 不可达时的兜底路径，因此：

1. **兜底裁判 = 画像中该维度 PASS 裁判里 agreement 最高的。** 若该维度 PASS 裁判为空，
   兜底裁判缺失 → arbitrate 进 blocked 并请求人，**不得"随便挑一个还活着的裁判"**。
2. `degraded_flags` 追加 `jev_unreachable`（pack 写死）与该维度 lead judge 的 id，
   供 report 说明"这条结论是在仲裁不可用的情况下由谁裁的"。
3. `thresholds.arbitration.jev_choice_limit = 255`、taxonomy 当前 13 类 → 安全，无需动作。
4. **画像绑版本。** 画像绑定 `pack_hash` + 各裁判模型版本。换裁判模型 = 旧画像作废，
   必须重跑校准。把旧画像套新裁判，等于用上一季度的体检报告给今天的运动员开健康证明。

## 6. proposals：只提案，不生效

calibrate 产出 `judges.yaml` 的 override **提案**，去向 govern（`decision_kind=approve_pack_diff`）。
每条提案结构：`{target_field, current, proposed, evidence}`，其中 evidence 必须含
`agreement / n / 维度 / 裁判` 三项，缺一不可 —— 没有 n 的 agreement 不能作为改变评测标准的理由。

允许的提案类型：

| target_field | 形态 | 需要的证据强度 |
|---|---|---|
| `judges.<id>.dimensions` | 从全维度收窄到若干维 | 该 cell 的 FAIL 及 n |
| `judges.<id>.lead_dimensions` | 提升为某维度 lead | 该维度 agreement 排序 |
| `thresholds.calibrate.min_n_per_cell` 等 | 补缺失阈值 | 当前 UNKNOWN 的 cell 数与样本分布 |
| `prompts/<dim>.md` | 修裁判提示词 | 系统性 over/under 率 + 具体误判样本 |

**红线：calibrate 不得因裁判表现差而自行改 rubric 或 prompt。** 那是改评测标准，
必须人批准。提案是 observations + 建议，不是 actions。
同理，calibrate 不得自行把某个裁判从 panel 里删掉 —— 那也是在改 judges.yaml。

## 7. 画像数据是黄金决策池的输入之一

每次画像连同 `pack_hash` / 裁判模型版本 / 各 cell 的 n 一起进黄金池（golden decision pool），
供 govern 观察趋势：同一个裁判在同一 pack 上多轮 DEGRADE，比单轮 FAIL 更有信息量。
单轮 FAIL 可能来自一批难样本；连续三轮 FAIL 才说明这个裁判×维度组合真的不能用。

池的写入由 govern 负责，calibrate 只保证画像本身带齐溯源字段。

## 8. 停止条件速查

- 被评 agent 的 model family 与 `judges.yaml#judged_agent_forbidden_families` 有交集 → 不出画像，直接 fail。
- 某维度可用裁判（PASS + 可 DEGRADE 使用）< 2 → 该维度升级人工评审。
- panel < 2 → 停止。
- 校准集条目缺少 `has_golden_outcome=true` → 输入不合法，回到 evalset 修，不在 calibrate 侧降级。
