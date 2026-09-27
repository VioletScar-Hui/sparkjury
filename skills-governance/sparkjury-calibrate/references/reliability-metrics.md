# reliability-metrics.md — 裁判可靠性指标：定义、公式、最小样本量、方差注意事项

calibrate 的度量依据。SKILL.md 只留主干，这里放公式与统计口径。
**本文件随 pack 一起被 `frozen_hash` 覆盖**，修改它 = 改评测标准 = pack 新版本。

所有阈值的唯一权威来源是 `thresholds.yaml`。`thresholds.calibrate.*` 在 pack v0.1 **整段不存在**，
因此 v0.1 无法给出任何门禁判定，只能给 UNKNOWN + 告警 + pack diff 提案（见 `gate-policy.md`）。
本文给出的数字只有两类：**(a) 从公式直接推导的样本量**，**(b) 明确标注为工程判断的参考线**。
两者都不是 pack 阈值。

---

## 0. 输入口径（先定这个，否则所有数字都没意义）

| 概念 | 定义 |
|---|---|
| 金标 cell | `(task_id, dimension)` 二元组。只有 `expected_outcome_ref` 已注册（有 checker）的任务在其 outcome 维有金标；process 维仅在提供 `golden_steps_ref` 时有金标 |
| 主票 | 同一 `(judge, dimension, task_id)` 的多次重复跑（`judge_repeats=2`）中 `repeat=1` 的那一票。**所有 agreement 类指标只用主票** |
| 重复票 | `repeat>=2` 的票。只用于估裁判自身稳定性（§5），**不并入 agreement 分母** |

为什么重复票不并入分母：同一 prompt、同一输入、同一温度的重复投票高度相关，把它当独立样本会让
n 虚高、置信区间看起来比实际窄一倍。**n 必须是"独立任务数"而不是"调用次数"**。
派 more trials 换不来更多证据，只能换来更稳的同一个证据。

## 1. agreement_with_gold（与金标一致率）

设裁判在某 cell 上的主票为 `s_1..s_n`，对应金标为 `g_1..g_n`。

```
                1[ s_i == g_i ]            (i = 1..n)
agreement = ───────────────────────── ,   1[·] 为指示函数
                         n
```

**双口径**（0-3 有序量表上"差 1 分"的含义不均匀，单一口径会误导）：

```
agreement_exact  = mean( 1[s_i == g_i] )                    # 严格一致，默认主口径
agreement_±1     = mean( 1[|s_i - g_i| <= 1] )              # 宽松一致，仅作参考
```

**方向分解**（这是核心。一个裁判可以 agreement 很高但系统性偏向一侧）：

```
over_rate  = mean( 1[s_i >  g_i] )     # 高估，比金标宽松
under_rate = mean( 1[s_i <  g_i] )     # 低估 / 漏判
mean_signed_bias = mean( s_i - g_i )   # 幅度版本，-0.5 表示系统性低半分
```

注：`over_rate + under_rate + agreement_exact = 1`。

为什么必须分开报：**漏判（under）和错杀（over）对产品的伤害不对称。**
漏判意味着"被评 agent 的问题裁判看不见"，badcase 进不了 cluster、进不了证据卡，
整条管线的下游全部静默失效 —— 这正是"小模型系统性漏判永远看不见"的具体机制。
错杀至少会表现成一条可见的 badcase，人会去查。所以 `under_rate` 是必须单独盯的指标。

## 2. score_variance（score 方差）

```
              1           n
Var(s) = ──────────  ·  Σ ( s_i - mean(s) )²        # 样本方差，ddof = 1
             n - 1
```

用途不是"越小越好"，而是**退化裁判检测**：

- Var ≈ 0 且金标有方差 → 该裁判几乎总给同一个分，agreement 可能是"全打中间分"刷高的。
  这是**阈值无关**的判定条件（`Var(s)==0 且 Var(g)>0`），不需要任何 pack 阈值即可识别。
- Var ≈ 0 且金标方差也 ≈ 0 → 说明不了任何事，通常是校准集难度没分层（见 evalset
  `three-set-spec.md` §2），此时 agreement=1.0 是空值。

**永远不要把 score 方差和 agreement 分开读。** 高 agreement + 零方差 = 无判别力；
低 agreement + 高方差 = 至少它在分辨，只是判错了，后者比前者更容易修。

## 3. order_swap_conflict_rate（顺序交换敏感度）

成对比较场景（`rubrics.process.pairwise: true`，`thresholds.scoring.order_swap: true`）下，
同一对被评轨迹以 A/B 与 B/A 两种顺序各跑一次，得两个结论 `c1, c2 ∈ {a_gt_b, b_gt_a, tie}`：

```
order_swap_conflict_rate = (#{ pair : c1 ≠ c2 }) / (#{ pair })
```

这是 LLM judge 已知的位置偏误（position bias）的直接观测，不依赖任何金标 ——
所以在没有金标的维度上，它是**唯一可用的可靠性信号**。

注意：冲突率 0 在 n 很小时不说明问题（2 对全一致也可能纯属偶然）。必须与 n 一起读。

## 4. latency（时延）

```
latency_p50 / latency_p95    # 单次裁判调用的耗时分位数
```

**这是成本信号，不是可靠性指标。** v0.1 无 latency 阈值 → 只记录，不进门禁判定。
它的实际用途：`run_manifest.route.estimated_judge_calls` 的预算核对，
以及 lead judge 同分时的排序依据（取更快的那个）。

## 5. repeat_agreement（裁判自身稳定性）

同一 `(judge, dimension, task_id)` 的 `judge_repeats` 次重复投票一致的比例：

```
repeat_agreement = (#{ task : 该 task 的所有重复票相同 }) / (#{ task : 重复票数 >= 2 })
```

用途：区分" judge 判错了"和"judge 自己都没想好"。低 repeat_agreement + 高 agreement
说明 judge 对难题结论不稳但平均而言对；两个都低说明提示词或量表有问题（→ pack diff 提案，
不自行改 prompt）。

## 6. 最小样本量

比例估计 `p̂` 的置信区间半宽（Wald 近似，95%）：

```
half_width ≈ z · sqrt( p̂(1 - p̂) / n ),   z = 1.96
```

令半宽 ≤ 0.10，解出所需 n：

| p̂ | 半宽 ≤ 0.10 所需 n | 半宽 ≤ 0.05 所需 n |
|---|---|---|
| 0.90 | **≈ 35** | ≈ 139 |
| 0.80 | **≈ 62** | ≈ 247 |
| 0.70 | ≈ 81 | ≈ 325 |

推导示例（p̂=0.9, 半宽=0.1）：`1.96·sqrt(0.9·0.1/n) ≤ 0.1` ⟹ `n ≥ 0.3457/0.01 ≈ 35`。

**结论：50 条校准集（evalset 的默认目标规模）只够分辨"裁判明显坏"和"裁判大概能用"，
不足以支撑对单个 (judge × dimension) cell 的精细结论。** 规模与门禁是两个约束，
必须同时满足；只把校准集做大而不设 min_n，画像会给出大量过自信的 PASS。

**工程参考线（非 pack 阈值）**：每个 cell 至少 **20 张主票** 才进入门禁判定。
这个数取自上表 p̂=0.8 / 半宽≈0.13 的量级，是工程判断而不是推导结果。
pack v0.2 应固化为 `thresholds.calibrate.min_n_per_cell`。

**Wald 的局限**：p̂→0/1 或 n 小时 Wald 退化明显，上表在这些区间偏乐观。
v0.1 不实现 Wilson 区间，改为**只报 n 和点估计、不报置信区间** ——
宁可少给一个数字，也不给一个假精确的数字。

## 7. 方差注意事项（会让指标说谎的六种情形）

1. **小 n 的点估计本身方差大。** n=2 的 agreement 只能是 0 / 0.5 / 1.0 三个值之一。
   所以 n < min_n 时门禁只能给 UNKNOWN，**绝不给 FAIL**（FAIL 是强断言）。
2. **票不独立。** 三裁判共享同一套 prompt 模板、同一份校准集、同一台机器。
   跨裁判的"一致"不等于独立验证 —— 这正是 pack 要求三裁判**跨模型族**的原因
   （Nine Judges 的观察：同族裁判的有效独立票约 2 票）。
3. **重复票高度相关。** 见 §0。重复跑提高稳定性，不提高证据量。
4. **选择偏差。** 只有被实际运行的 trace 才产生票。未覆盖的任务静默缺席，
   画像的分母不等于任务集。→ `coverage.unmatched_votes` 必须和画像一起报。
5. **多重比较。** cell 数 = 裁判数 × 维度数（v0.1 = 3 × 4 = 12）。
   不校正时"至少一个 cell 显著差"很容易偶然发生。v0.1 不做校正，
   但画像必须报 `coverage.cells_total`，让读的人知道做了多少次比较。
6. **金标只在有 checker 处存在。** 没有金标的维度，agreement 类指标不可计算 ——
   标 `no_gold_for_dimension`，**不要用裁判互投当代理**（那是循环论证，
   见 evalset 的 `evalset-guard-no-fake-gold` 用例）。

## 8. 与外部出处的关系

可引用的已有工作（不确定的一律标"未验证"，不编造数字）：

- **JudgeBench**（arXiv [2410.12784](https://arxiv.org/abs/2410.12784)）：judge 可靠性基准。
  本仓引用其**同族自评掉点**的观察作为"裁判不得与被评 agent 同族"这一红线的依据。
  具体掉点幅度以论文原文为准，本文件不复述未经核对的数字。
- **Skywork-Reward-V2**（arXiv [2507.01352](https://arxiv.org/abs/2507.01352)）：
  奖励模型校准方向的工作，用于说明"为 judge 建立可靠性画像"不是本仓独创的需求。
- **τ²-bench**（arXiv [2506.07982](https://arxiv.org/abs/2506.07982)）：本仓金标与 pass^k 的来源。
