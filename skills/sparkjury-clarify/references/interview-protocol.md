# references/interview-protocol.md — 五问的设计原理

clarify 的访谈不是「随便问几个问题」，而是一次有预算的**标准定义收敛**。
本文件解释：每问为什么存在、它对应 pack 的哪个字段、为什么这五个维度够了、
以及过度追问的代价。SKILL.md 只放结论，这里放推理。

---

## 1. pack 里到底有多少「可变」的东西

先把 scenario-pack 的可变维度列全（依据 `standards/scenario-pack/README.md` 的结构），
再问「哪些必须由人在澄清期定」：

| pack 内容 | 谁来定 | 为什么 |
|---|---|---|
| `pack.manifest.json` 的 `pack_id` | 人（Q1） | 被评对象决定域，域决定 checker 与 taxonomy 归属 |
| `rubrics.yaml` 四维权重 | 人（Q2） | 权重是价值判断，没有任何数据能推出「risk 该占多少」 |
| `rubrics.yaml` outcome 判据路径 + `checkers/` | 人（Q3） | 有无可判定终态是任务域的事实，agent 无从推测 |
| `rubrics.yaml` risk `hard_rules` / `taxonomy.yaml` F08 | 人（Q4） | 一票否决是产品责任边界，只能由人划线 |
| `thresholds.yaml` `regress.pass_k` / `gates.primary_metric` | 人（Q5） | 回归口径决定「改进」的定义，改了口径历史结论作废 |
| `judges.yaml` 裁判面板 | calibrate 产出可靠性画像 | **不在访谈范围** —— 由数据决定，不由人拍 |
| `thresholds.yaml` `cluster.min_cluster_size` 等聚类参数 | 默认值 + 黄金池校准 | **不在访谈范围** —— 属于调参，不是标准定义 |
| `prompts/*` 措辞 | 由 rubric 派生 | **不在访谈范围** —— 追问措辞是追问实现细节 |
| `taxonomy.yaml` F01-F99 词表 | 默认 v0.1 草案 + 黄金池补类 | **不在访谈范围** —— 访谈期问不出真实失败模式分布，那是 cluster 的活 |

于是剩下必须问的恰好是五件事：**对象、权重、判据、红线、回归口径**。这就是五问。

## 2. 每问对应的 pack 字段

| # | 问题 | 命中字段 | 派生影响 |
|---|---|---|---|
| Q1 | 被评对象是什么 agent、什么域？ | `pack.manifest.json:pack_id`；`checkers/README.md` 的域归属 | 决定 checker 契约提案的命名空间、taxonomy 的域偏置 |
| Q2 | 哪几个维度重要？ | `rubrics.yaml:dimensions[id=outcome\|process\|efficiency\|risk].weight` | 直接影响 score 的加权总分；四者和必须为 1.0，否则提案作废记 pending |
| Q3 | 有没有可判定终态/金标？ | `rubrics.yaml:dimensions[id=outcome].method`（`deterministic_first` / `llm_rubric`）；`checkers/` 注册；`judges.yaml` | 有金标 → outcome 走确定性路径、`no_golden_outcome` 标记不亮；无金标 → pack v0.1 已内建「最多 2 分 + 置信降档」，通常零 diff |
| Q4 | 什么动作一票否决？ | `rubrics.yaml:dimensions[id=risk].hard_rules`；`taxonomy.yaml:F08`；`thresholds.yaml:regress.gates.new_severe_cluster_blocks` | 新增否决动作需要 checker 契约提案（不是实现）；`new_severe_cluster_blocks` 决定新类能否进门 |
| Q5 | 回归口径？ | `thresholds.yaml:regress.pass_k`；`regress.gates.primary_metric`；`regress.gates.delta_min` | 直接决定 regress skill 的判据；改 k = 历史结论不可比 |

**追问的合法范围只有「标准定义」。** 什么是标准定义？「risk 该占多少」「什么算一票否决」
「改进多少算改进」。什么是实现细节？「用哪个 embedding」「裁判 prompt 怎么写」
「HDBSCAN 参数多少」。后者不属于访谈，出现即违规。

## 3. 为什么这五个维度足够（论据）

1. **覆盖性**：§1 的推导显示 pack 中其余字段都有非人的决定者或默认值，五问之后
   pack 的每个可变字段都有了明确的来源（人定 / calibrate 定 / 默认 / 待校准）。
2. **可判定性**：五问的答案都能落成具体字段的取值或方向。答不出来的问是坏问题。
3. **可回溯性**：五问的 `maps_to` 让每条 diff 都能回答「你凭什么改这个字段」。
4. **正交性**：五问之间基本不互相推导——知道域推不出权重，知道 k 推不出红线。
   不正交的问题会滚雪球（问了一个就顺手问出五个），是预算失控的主因。

## 4. 过度追问的代价（为什么预算硬上限是 5）

- **澄清疲劳**：问得越多，后面的回答越草率。第 4、5 个问题的答案质量通常已明显低于前两个。
- **随口定义**：PM 在被连续追问时会给出「听起来合理」的数字（「那就 0.2 吧」），
  这个数字此后被当成标准执行数月，无人记得它是被问出来的。**宁可留 pending_item，
  不要一个没有依据的数字。**
- **pack 版本抖动**：访谈产出越多 diff，v(N+1) 越大，冻结越难，回归基准越不稳定。
- **准备过度、执行滞后**：把标准打磨到完美再开跑，是这套产品要对抗的失效模式。
  澄清期的成功标准是「够用且可回溯」，不是「完备」。
- **追问会滑向实现细节**：问得深就容易问到你无权决定的字段（裁判选型、prompt 措辞），
  而那些字段的正确答案在 calibrate 的画像里，不在 PM 嘴里。

**预算规则**：问题数 > 5 = 过度设计。处理方式固定：停止提问，用默认值收敛，
把所有未决项写进 `pending_items`，让 v(N+1) 带着已声明的未决项冻结。
v(N+1) 的 pending_item 会在下一轮跑完后由黄金池信号或新的访谈自然消解。

## 5. 收敛的三种合法姿态

| 姿态 | 何时用 | 产出 |
|---|---|---|
| 零改动收敛 | 用户说默认 / 诉求已被 pack 覆盖 | `status=zero_change`，空 diff，记 `no_change_reason` |
| 显式收敛 | 用户给了数字 | `proposed=<数字>` + 校验（权重和为 1、k 为正整数） |
| 方向收敛 | 用户只给了方向 | `proposed=null` + `direction` + pending_item |

非法的第四种姿态是「agent 猜一个数补全」——那是把人的责任转移给系统，
本 skill 一律禁止。

## 6. 复盘清单（reject 之后看这里）

提案被 `reject_proposal` 时，把理由回流到这张表，判断是「问题设计错了」还是「预算不够」：

- 被拒的是 diff 内容（字段选错）→ 修 `maps_to` 映射表。
- 被拒的是问题本身（PM 答不上来）→ 该问应改为默认值 + pending_item。
- 被拒的是数量（问太多）→ 压缩问题合并，不是增加预算。
- 被拒的是「你凭什么替我问 Q4」→ 检查是否追问了实现细节。
