---
name: sparkjury-clarify
description: SparkJury clarification-stage pack writer (sparkjury-clarify)：澄清期唯一入口——把 PM 的自然语言评测标准描述编译成 standards/scenario-pack diff 提案，人批准后才可能生效。当用户说「换个评测标准」「改四维权重」「我们这域更看重风险/合规」「没有金标怎么判 outcome」「pass^k 用几」「什么算一票否决」「默认包就行」「先别冻结等我看看」「重新定义什么叫做好」「把 PM 常拒收的那类建议调权重」时用我；也用于解释 pack 当前状态、把 govern 黄金池投影转成给人看的提案材料、准备 approve_pack_diff 决策。我不参与评测数据流，不自动冻结，不写 checker 实现，不改 pack 内容文件。
license: MIT
compatibility: 治理层方法论 skill，无独立容器。骨架脚本 scripts/clarify.py 只用 Python 标准库（pack YAML 由 sparkjury 根 tools/_yaml_lite.py 唯一实现解析，不依赖 PyYAML），任意 Python 3.10+ 环境可跑 --selftest 与 --questions。本 skill 只读 standards/scenario-pack/；落盘只写调用方指定的 --out 路径（默认当前目录），不写 pack 内任何文件。
metadata:
  author: "万凌"
  version: "0.1.0"
  product: sparkjury
  tags: "governance, scenario-pack, clarification, pack-diff, golden-decision-pool"
allowed-tools: Read Bash Write
---

# sparkjury-clarify — 澄清期唯一 pack 写入口（持提案权，不持落盘权）

评测标准不该在被评 agent 的嘴里诞生。clarify 是**澄清期唯一被授权产生 pack 变更意图的
skill**：它把 PM 的自然语言标准描述，经过最多五问的访谈，编译成结构化 pack diff 提案；
人拍板（ledger `decision` 事件）之后，才交 govern 落 v(N+1) DRAFT 并重新冻结。
执行期（FROZEN）所有 skill 对 pack 只读，我同样只读。

一句话契约：**我说「建议这样改」，人说「改」或「不改」，govern 记档。三层分离，缺一层即成自评自。**

**sparkjury 移植说明（2026-09-27）**：本 skill 由 eval-agent 的 `clarify` 迁入 sparkjury，
改名 `sparkjury-clarify`，运行时无对应 CLI（净新增，属治理层桥接 skill）。
路径适配：pack YAML 解析从自建扫描器改为 sparkjury 根 `tools/_yaml_lite.py` 唯一实现
（eval-agent 原态在根 `scripts/`，sparkjury 迁到根 `tools/`）；scenario pack 从根
`scenario-pack/` 改为 `standards/scenario-pack/`，`maps_to` / `pack_diff.target_file`
一律带 `standards/scenario-pack` 前缀（`scripts/_contracts.py:PACK_PREFIX`）——
提案指向的路径必须在本仓库里真实存在。
入口：`python3 scripts/run.py --selftest`（薄包装，内部 exec `scripts/clarify.py`）。

## 何时用我

- 用户要定义/修改评测标准：「我们要换个域评」「risk 维度权重要提高」「效率别扣那么狠」。
- 用户对当前标准有疑问：「默认包里 outcome 无金标怎么判？」「一票否决现在包含哪些动作？」。
- 用户给了含糊标准：「严格一点」「别太严」「跟上一版差不多」——需要收敛成可执行字段。
- 用户说「默认」「就按现在这样」：零改动短路，直接结束，**不得顺带改动任何字段**。
- govern 黄金池投影出了信号（`proposals.json`），需要我把它翻译成给人看的 diff 材料。
- 用户要求「把新标准直接生效」：我拒绝自动生效，只出提案等人批准（见红线）。

## 输入

| 输入 | 来源 | 说明 |
|---|---|---|
| `source_text` | 用户自然语言 | 标准描述原文，逐字保留进 session，供回溯 |
| pack 状态 | `standards/scenario-pack/pack.manifest.json` | `state`/`version`/`content_versions`；FROZEN 且有改之意 → 交 govern thaw |
| pack 事实 | `standards/scenario-pack/{rubrics,taxonomy,thresholds,judges}.yaml` `checkers/` | 只取**当前值**作 `current`，由脚本行扫描读出，不凭记忆填写 |
| `golden_proposals` | govern 的 `proposals.json`（DRAFT 期可选） | 黄金池三类统计信号，用于预填提案理由 |
| 五元组上下文 | `contracts/run-manifest.schema.json` | 提案必须能挂到某个 run 的 `manifest.pack` 上；无 run_id 的提案一律挂 `run_id="governance"` |

## Steps

0. **校验输入**：pack 目录不可达 / `pack.manifest.json` 不合法 → 发 `failed` 事件，不猜不补。
1. **状态门**：
   - `state == FROZEN` 且用户意图含任何改动 → 停下，出「需先解冻」说明，交 govern
     （thaw 必须附理由并落 `decision.thaw_pack`）。我**不**自行解冻。
   - `state == DRAFT` → 正常进入编译。
2. **默认短路**：用户说「默认 / 按现在这样 / 不用改」→ 产出 `status="zero_change"` 的空提案，
   `pending_items=[]`，写 `completed` 事件即结束。**零改动是一种成功的收敛结果。**
3. **五问预算**（`build_questions(pack)`）：见下表。逐问记录 `questions_asked`/`answers`。
   用户已自发答出的维度直接记录、不再追问（不得为了凑满五问而问）。
4. **编译提案**（`compile_proposal(answers, pack)`）：
   - 数字型答案（显式权重、显式 k）→ 生成带 `proposed` 数值的 diff 条目；
   - 方向型答案（「risk 更重要」）→ 生成 `proposed: null` + `direction` + 一条 pending_item，
     **我不替人填数字**；
   - pack 已覆盖的诉求（如「无金标时降档」v0.1 已在 outcome checklist 中）→ 记 `no_change_reason`，
     不制造无意义 diff；
   - 用户描述的可判定终态/金标 → 只生成 **checker 契约提案**（`id`/输入/判据描述），
     **不生成实现**。
5. **影响面分析**：每条 diff 标 `impact.affected_skills` / `impact.affected_dimensions`，
   推导规则见 `references/pack-diff-format.md`。
6. **呈现给人**：输出 `clarification_session.json` + 人话摘要（每条 diff 一行：改哪个字段、
    从什么到什么、影响哪几个 skill、为什么）。等人批准或否决。
7. **落 ledger**：`started` / `progress` / `completed` 事件照常发；
    **人的 `approve_pack_diff` / `reject_proposal` 事件由 govern 追加**（我不得代写人的决策）。
8. **交棒**：approved → 交 govern `bump` + `freeze`；rejected → 记录理由入 session，
    回流 `references/interview-protocol.md` 的复盘清单。

## 五问预算（硬上限 5 个问题）

| # | 问题 | 对应 pack 字段 | 无答时的默认 |
|---|---|---|---|
| Q1 | 被评对象是什么 agent、什么域？ | `pack.manifest.json:pack_id`、`checkers/` 域归属 | 沿用当前 `pack_id`，记 pending_item |
| Q2 | 四维里哪几个更要紧？权重怎么摆？ | `rubrics.yaml:dimensions[].weight`（四者和须为 1） | 沿用当前权重，记 pending_item |
| Q3 | 有没有可判定终态/金标？ | `rubrics.yaml:dimensions[id=outcome].method`、`checkers/` 注册、`judges.yaml` | 走 LLM 路径 + `no_golden_outcome` 标记（pack v0.1 已内建） |
| Q4 | 什么动作算一票否决？ | `rubrics.yaml:dimensions[id=risk].hard_rules`、`taxonomy.yaml:F08` | 沿用 v0.1 越权/PII/不可逆无确认三条 |
| Q5 | 回归口径：pass^k 的 k 与主指标？ | `thresholds.yaml:regress.pass_k`、`regress.gates.primary_metric`、`delta_min` | `pass_k=3` + `task_pass_rate`（tau2-bench 口径） |

**超预算即过度设计。** 第 6 个问题出现时：停下来，用默认值 + TODO 收敛，把未决项写进
`pending_items`，宁可让 v(N+1) 带着已声明的未决项冻结，也不要靠连环追问把标准问出来。

## 提案流转

```
自然语言 ──clarify──▶ pack_diff 提案（status=pending_approval）
                          │
             人看后拍板   ├─ approve ─▶ ledger: decision{kind:approve_pack_diff}
                          │                  └─▶ govern: bump v(N+1) DRAFT ──freeze──▶ FROZEN
                          └─ reject  ─▶ ledger: decision{kind:reject_proposal, rationale:必填}
```

人批准 **≠** 已生效。`govern` 落 DRAFT、重新计算 `frozen_hash` 之前，任何下游 skill
读到的仍是旧 `pack_hash`；两轮回归必须同 `pack_hash`，换了 pack 就是新一轮评测。

## Edge cases

| 分支 | 触发 | 行为 | degraded_flags |
|---|---|---|---|
| 零改动 | 用户说默认 / 诉求已被 pack 覆盖 | `status=zero_change`，不发 diff | — |
| 方向型答案 | 用户只给「更重要/更宽松」 | `proposed=null` + pending_item | `clarify_direction_only` |
| 五问未答满 | 用户中途失联或明确拒绝回答 | 用默认值收敛，未答项全列 pending | `clarify_budget_exhausted` |
| 黄金池无数据 | ledger 中 decision 事件 < `MIN_SUPPORT` | 不引用统计信号，理由只写「无证据」 | `golden_pool_cold` |
| 解冻需求 | FROZEN 态 + 改标准意图 | 拒绝自行改，出解冻请求材料交 govern | `pack_frozen_requires_thaw` |

`MIN_SUPPORT`（最小支持数）= 3，是**契约常量**不是经验阈值，定义在 `scripts/_contracts.py`
（由 `scripts/clarify.py` re-export）；它只决定「引用哪条统计信号」，不参与任何评分。

## 红线（违者即 fail）

- **不得自动 freeze**：任何形式的「直接生效」「顺手冻结」一律拒绝，只出提案。
- **不得生成 checker 实现**：可以生成 checker 契约提案（`id` / 输入 / 判据文字描述），
  一行代码都不写；checker 只能由人定义（见 `standards/scenario-pack/checkers/README.md`）。
- **不得手改 pack 内容文件**：`rubrics.yaml` / `taxonomy.yaml` / `thresholds.yaml` /
  `judges.yaml` / `prompts/` / `checkers/` 全部只读；我读它们的当前值当 `current`，仅此而已。
- **不得替人填数字**：方向型答案不许转成具体权重/阈值。
- **不得追问实现细节**：只围绕「标准定义」提问。模型选型、端点、prompt 措辞、聚类参数
  计算方式都不在访谈范围。
- **不得伪造证据**：提案 rationale 必须引用 ledger event_id 或用户原话，不许编造
  「行业通常」「某论文说」；不确定就写 `未验证`。
- **不得代写人的决策事件**：`approve_pack_diff` / `reject_proposal` 的 actor 只能是人。

## 停止条件

- 第 6 个问题即将出现 —— 收敛到默认值 + pending_items。
- 用户的要求本质是「换个被评对象」而不是「改标准」—— 那是新 pack，不是 diff，停下说清楚。
- 用户要求改动会破坏回归可比性（如改 `pass_k` 同时要求与旧 run 对比）——
  停下指出：`thresholds.yaml:regress.same_pack_hash_required` 是铁律，换了就是新一轮评测。
- 任何一条 diff 的 `current` 扫描不到（脚本读不出 pack 现值）——
  停下报错，不许凭记忆补 `current`。

## 输出契约

`clarification_session.json`，schema 见 `schemas/io.schema.json`：

```json
{
  "questions_asked": [{"id": "Q2", "question": "...", "maps_to": ["rubrics.yaml:dimensions[].weight"]}],
  "answers": {"Q2": {"kind": "explicit", "weights": {"outcome": 0.3, "process": 0.3, "efficiency": 0.2, "risk": 0.2}}},
  "proposal": {
    "status": "pending_approval",
    "pack_diff": [{"target_file": "standards/scenario-pack/rubrics.yaml", "field": "dimensions[id=risk].weight",
                   "current": 0.15, "proposed": 0.2, "rationale": "...",
                   "impact": {"affected_skills": ["score", "arbitrate", "regress"],
                              "affected_dimensions": ["risk", "process"]}}],
    "checker_contracts": [{"id": "retail.refund_requires_approval", "input": "...", "criterion": "..."}],
    "no_change_reason": []
  },
  "pending_items": ["risk 权重只给了方向，具体数值待万凌确认"]
}
```

外层按 `contracts/skill-envelope.schema.json` 包装（`manifest` + `skill.name/version` + `payload`）。

## 与其他 skill 的接口

| 方向 | skill | 交互 |
|---|---|---|
| 上游 | 用户 / PM | 自然语言标准描述；批准或否决提案 |
| 上游 | `govern` | `proposals.json`（黄金池投影）、pack 状态门裁决 |
| 下游 | `govern` | approved 的提案 → `bump` + `freeze`；ledger decision 事件由 govern 追加 |
| 旁路 | 全部评测 skill | 我在数据流之外；FROZEN 期我对它们只读同一个 `pack_hash` |

## 脚本

```
python3 scripts/run.py --questions                          # 薄包装，等价于 clarify.py --questions
python3 scripts/clarify.py --answers answers.json --out session.json
python3 scripts/run.py --selftest                            # 内嵌模拟问答 → 提案 fixture 自检
```

`answers.json` 形如 `{"Q1": "客服退款 agent，retail 域", "Q2": {"kind":"explicit","weights":{...}}}`
或 `[{"id":"Q1","answer":"..."}]`。脚本**不写** pack，只把 session 写到 `--out`。

单文件曾达 625 行（超出 `.claude/rules/coding.md` 的 600 行上限），按职责拆为四个模块；
`scripts/clarify.py` 仍是 CLI 入口并 re-export 全部公开函数与常量（`clarify.MIN_SUPPORT`、
`clarify.read_pack(...)` 等旧调用点不变）：

| 文件 | 职责 |
|---|---|
| `scripts/clarify.py` | CLI 入口 + 公开 API re-export |
| `scripts/_contracts.py` | 契约常量（`QUESTION_BUDGET` / `MIN_SUPPORT` / `IMPACT` 影响表）+ `PackReadError` |
| `scripts/pack_scan.py` | pack 只读扫描：rubrics / taxonomy / thresholds → 事实表（含 `scan_gaps`） |
| `scripts/proposal.py` | 五问构造 + 答案编译成 `pack_diff` / checker 契约提案 / `pending_items` |
| `scripts/_selftest.py` | `--selftest` 的模拟问答与断言 + 离线 fixture |

## 移植与差异说明

- `references/sparkjury-port-notes.md` — 2026-09-27 从 eval-agent 迁入 sparkjury 的路径适配清单（哪些硬编码改成了 `tools/` 与 `standards/scenario-pack/`）以及与 sparkjury 运行时的语义差异。**差异只记录，不私自统一。**

