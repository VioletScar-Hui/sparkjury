# references/priority-formula.md — 公式语义与边界

本文件解释公式**怎么读**、**边界怎么走**，以及**为什么某些情况选择拒排而不是给个数**。
系数值本身在 `standards/scenario-pack/thresholds.yaml`，此处不复制。

## 1. 三项相乘的语义

```
priority = frequency_norm × severity_weight × fixability_boost
```

乘法结构本身就是一个声明：**三个因子是串联的，任何一项趋零，整体趋零。**

| 因子 | 问的问题 | 数据来源 |
|---|---|---|
| `frequency_norm` | 这错影响多少人（条） | 输入计算，确定性 |
| `severity_weight` | 这错有多严重 | cluster 的 `severity_max` → pack 查表 |
| `fixability_boost` | 改起来有多顺手 | pack 人工先验 |

相乘而非相加的理由：加法会让一个很大的因子掩盖另外两个。
比如"频率极高但 severity=1 且难改"的类，在加法模型里可能排得很高，
在乘法模型里会被正确地压下来 —— 它确实不值得先修。

**注意 severity 与 fixability 的地位是不对称的**：severity 来自观测（trace 评分），
fixability 来自先验（人的判断）。两者相乘意味着**人的先验和对数据的观测同权重**。
这是 v0.1 的有意选择（否则没有 fixability 这一项），但它要求输出必须显式标注先验未经校准
—— 见 `fixability_source` 与 `meta.disclaimer`。读者把先验当结论用，是这个设计最大的风险。

## 2. 归一化方式

### frequency_norm

```
frequency_norm = 类内 badcase 数 / 总 badcase 数      ∈ [0, 1]
```

分母取 `cluster_report.total_badcases`（含 F99）。

**为什么不做 min-max 或 z-score 归一化**：那种归一化会让 priority 依赖本轮所有类的分布。
同一个类在两轮之间分布变了，它的 priority 就变了 —— 而这会让"两轮之间 F02 的优先级变了"
分不清是数据变了还是别的类变了。用绝对占比，priority 只依赖自己的分子分母，可跨轮比较。

代价：占比天然偏袒大类。一个占 20% 的 severity=3 类会赢过占 40% 的 severity=1 类。
这正是设计意图（`0.2 × 1.0 × x` vs `0.4 × 0.3 × y`），不是缺陷。

### severity_weight

pack 表 `{3: 1.0, 2: 0.6, 1: 0.3}`。三档而非连续值，因为 rubric 的 scale 本身就是
0-3 的四档离散刻度，连续化等于伪造精度。

**不夹逼**：`severity_max` 若不在 {1,2,3}（例如 score 侧给了 5，或字段缺失成 `null`），
该类进 `rejected_candidates`。绝不夹逼到 3 —— 夹逼会让它静默拿到 `1.0` 的最高权重，
看起来完全正常。

### fixability_boost

pack 逐类人工先验，**没有归一化**（F01=1.0、F02=1.2、F08=0.5 …
取值不经任何缩放）。原因同上：缩放到 [0,1] 会让它依赖本轮出现的类集合。
原样使用意味着"1.2 就是这个类的绝对可修复性倍数"。

v0.1 的 12 个值全部是**未经黄金池校准的人工设定**，输出必须注明。

## 3. 边界情况处理

### severity=3 的"一票否决类"

v0.1 **没有**单独的一票否决逻辑。severity=3 通过 `severity_weight=1.0`
（表中的最高档）体现。

为什么不加硬否决：硬否决会让一个只出现 1 条的 F04 排到几十条 F02 前面。
这在某些场景对（危险动作确实该优先），在另一些场景错（一次危险 vs 系统性失效）。
**这是一个产品判断，不是技术判断**，不该由 skill 悄悄做掉。

如果产品最终要求"severity=3 无条件最优先"，正确做法是在 pack 里加显式规则字段
（例如 `prioritize.severity3_veto: true`），而不是在 skill 里 if 一个魔法数。
TODO(v0.2)。

### fixability=0 的类

pack v0.1 没有任何类取 0，所以这条是前瞻约定，**未经真实数据检验**：

- 不进 `rejected_candidates`。0 是一个合法系数，意思是"完全不可修"，
  与"缺系数"是两回事 —— 前者是结论，后者是缺失
- `priority = 0`，排在末位
- `rationale` 照常写出乘法，让"为什么是 0"可复算
- **不进 `top_recommendation`**（它本来也排不到第一）

呈现上需要注意：一个 priority=0 的类在卡片上会显示"建议先修：无"。
更稳妥的呈现是在 `report` 侧处理（提示"本轮无高优先级修复项"），
但那超出本 skill 范围，此处留给 report 决定。

### 分母不可用

`total_badcases ≤ 0` 或非整数 → 该类进 `rejected_candidates`，reason 写明"frequency_norm 无分母"。
不猜分母。**一个算错分母的优先级排序比没有排序更危险**，因为它看起来是可执行的。

### F99 混入 clusters

pack 的 `fixability_boost` 没有 F99 条目 → 自然进 `rejected_candidates`。
这是防线，也是正确行为：F99 是待人裁决的队列，把未分类的东西排进"先修"顺序
等于把人的分类工作悄悄排了个期。

## 4. 排序键与幂等

```
排序键 = (-priority, category_id)
```

`category_id` 升序兜底，保证 priority 相同时顺序确定。
（真实数据里三个 equal-priority 的类很常见 —— 频率相同时尤其。）

**幂等性**是本 skill 的硬要求：
- 输入相同 → 输出 byte 级相同（sha256 一致）
- override 事件重复应用不改变结果：提升后 rank ≤ `to_rank`，第二次判定不触发

`_selftest.py` 第 6 组用 sha256 对比验证。这不是洁癖 ——
一个排序器如果不能被独立复算，它的结论就无法在评审时被辩护。

## 5. 与黄金池的关系

公式是**默认判断**，人是**校准源**。

```
本轮排序 = apply_overrides(sort_by_formula(clusters), ledger.override_events)
```

hook 语义刻意保守：

- **只提升、不压低**。本轮 rank 已 ≤ 人工 `to_rank` 时不动（说明人与公式一致）
- 提升后其余顺延
- `rationale` 必须追加人工修正来源（`event_id`、`from_rank→to_rank`、`actor`）
- `override_applied` 逐条记录用了哪个人工决策

**为什么不直接按人的偏好重算权重**：一个 PM 的一次 override 是 N=1 的样本。
按 N=1 调权重会让下一次结论完全由这一个人定，且没有任何可解释性。
正确路径是积累足够多的 override 决策，经 `govern` 提案改 pack，
让系数变更留下可审计的痕迹。v0.1 的 hook 只是这条路的雏形。

**反向信号同样重要**：如果 `top_recommendation` 与 PM 上次 override 连续冲突，
说明公式缺了一个 PM 在意的维度。这时该考虑扩公式，而不是反复覆盖人工决策
（SKILL.md「停止条件」已列为必须停下的情形）。

## 6. 未验证清单

- `fixability_boost` 12 个值的准确性：**未验证**（人工先验）
- `severity_weights` 三档间距是否合理：**未验证**（1.0/0.6/0.3 无校准依据）
- `severity3_veto` 是否该存在：**未验证**（产品决策）
- `fixability=0` 的呈现方式：**未验证**（pack 无 0 值类）
- 排序结果与"实际修复后 pass^k 提升"的相关性：**未验证**
  （需要 `regress` 多轮数据，目前无数据）
