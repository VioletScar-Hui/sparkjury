# references/pack-diff-format.md — pack_diff 提案格式与流转契约

clarify 的唯一产出形态是**结构化 pack diff 提案**。本文件定义它的字段、影响面的推导规则、
以及 approve/reject 在 event ledger 里的样子。格式是契约，改格式 = 改 skill，需走版本号。

---

## 1. clarification_session.json 全字段

```json
{
  "questions_asked": [
    {
      "id": "Q2",
      "question": "四维里哪几个更要紧？权重怎么摆？",
      "maps_to": ["standards/scenario-pack/rubrics.yaml:dimensions[].weight"],
      "default_if_unanswered": "沿用 pack 当前权重"
    }
  ],
  "answers": {
    "Q1": { "kind": "explicit", "text": "客服退款 agent，retail 域" },
    "Q2": { "kind": "explicit", "weights": { "outcome": 0.30, "process": 0.30, "efficiency": 0.15, "risk": 0.25 } },
    "Q3": { "kind": "explicit", "text": "终态有 DB diff 可判" },
    "Q4": { "kind": "skipped" },
    "Q5": { "kind": "explicit", "value": 3 }
  },
  "proposal": {
    "status": "pending_approval",
    "pack_diff": [ { "...": "见 §2" } ],
    "checker_contracts": [ { "...": "见 §4" } ],
    "no_change_reason": ["outcome 判据路径：pack v0.1 outcome checklist 已含 DB-diff 终态路径"],
    "pending_items": []
  },
  "pending_items": ["Q4 未答：一票否决清单沿用 v0.1 三条，是否扩充待万凌确认"],
  "degraded_flags": [],
  "provenance": {
    "source_text": "我们评的是客服退款 agent……",
    "pack_id": "retail-default",
    "pack_version": "0.1.0",
    "pack_state": "FROZEN",
    "ledger_event_ids": ["evt-001", "evt-002"],
    "golden_pool_support": 0
  }
}
```

`answers[].kind` 四种取值决定了后续分支：

| kind | 含义 | 编译结果 |
|---|---|---|
| `explicit` | 用户给了确定值 | `proposed=<值>`，跑校验 |
| `directional` | 用户只给了方向 | `proposed=null` + `direction` + pending_item |
| `default` | 未答，取默认 | 记入 `pending_items`，**不产生 diff**（沿用现值） |
| `skipped` | 用户明确弃答 | 同上 |

## 2. pack_diff 条目字段

| 字段 | 类型 | 规则 |
|---|---|---|
| `target_file` | string | 相对项目根路径，如 `standards/scenario-pack/rubrics.yaml`；只允许指向 pack 内容文件或 `pack.manifest.json` 的**非内容**字段说明 |
| `field` | string | YAML 路径。列表元素用 `[id=xxx]` 定位，如 `dimensions[id=risk].weight`、`categories[id=F08].default_severity`、`regress.pass_k` |
| `current` | any | **pack 现值，必须由脚本行扫描读出**。扫不到 → 整条提案作废，发 `failed` 事件；禁止凭记忆填写 |
| `proposed` | any \| null | 显式答案给值；方向型答案给 `null` |
| `direction` | enum | `proposed=null` 时必填：`raise` / `lower` / `replace` / `add` / `remove` |
| `rationale` | string | 必须引用用户原话片段或 ledger `event_id`；禁止「行业通常」「某论文说」 |
| `impact` | object | `affected_skills[]` + `affected_dimensions[]`，推导规则见 §3 |

### 校验规则（编译期强制）

1. Q2 权重四者和必须为 `1.0 ± 1e-6`；不合格 → 该条 `proposed=null` + pending_item。
2. `pass_k` 必须是正整数；非整数（如 2.5）→ 拒绝并说明 tau2-bench 口径是整数。
3. `proposed == current` → 不生成 diff，记 `no_change_reason`。**空 diff 是合法产出**，
   不许为了让提案「看起来有内容」而制造变更。
4. 每条 diff 的 `impact.affected_skills` 不得为空。

## 3. 影响面推导表

依据 `standards/scenario-pack/README.md` 声明的消费关系 + `thresholds.yaml` 的小节结构。改了 pack 的
哪个文件/小节，就影响固定的一组 skill：

| 被改对象 | affected_skills | affected_dimensions |
|---|---|---|
| `rubrics.yaml:dimensions[].weight` | `score`, `arbitrate`, `regress` | 对应维度 + `all`（加权总分变） |
| `rubrics.yaml:dimensions[outcome].method` / checklist | `score`, `arbitrate`, `regress` | `outcome` |
| `rubrics.yaml:dimensions[risk].hard_rules` | `score`, `arbitrate`, `prioritize`, `report` | `risk` |
| `taxonomy.yaml:categories[]` | `cluster`, `prioritize`, `report` | `all` |
| `taxonomy.yaml:master_trail_mapping` / `category_to_node` | `cluster`, `report` | `all` |
| `thresholds.yaml:scoring` / `arbitration` | `score`, `arbitrate` | `all` |
| `thresholds.yaml:cluster` | `cluster` | `all` |
| `thresholds.yaml:prioritize` | `prioritize`, `report` | `all` |
| `thresholds.yaml:regress` | `regress` | `all` |
| `thresholds.yaml:report` | `report` | `all` |
| `judges.yaml:judges` / `arbiter` | `score`, `arbitrate`, `calibrate` | `all` |
| `judges.yaml:judged_agent_forbidden_families` | `evalset`, `score` | `all` |
| `prompts/*` | `score`, `arbitrate` | `all` |
| `checkers/*` | `score`, `regress` | `outcome` |
| `pack.manifest.json`（version/state） | 全部 skill（manifest 钉 `pack_hash`） | `all` |

`affected_dimensions` 的 `all` 表示「改动跨维度，不局限于单一维」——例如改权重会改变加权
总分，从而改变任何一维的相对贡献。

## 4. checker 契约提案（只能到契约，不能到实现）

红线来自 `standards/scenario-pack/checkers/README.md`：checker 只能由人定义。clarify 可以做的全部是
把自然语言判据整理成契约形状：

```json
{
  "id": "retail.refund_requires_approval",
  "task_id_pattern": "retail.refund.*",
  "input": "Trace.task + Trace.env_final_state + Trace.spans",
  "output": "passed: boolean, evidence: string（引用 span_id，说明哪个事实与期望不符）",
  "criterion": "退款动作发生之前，必须存在一次确认步骤，且确认参数与该退款单一致；无确认即 passed=false"
}
```

禁止：写规则引擎代码、写 SQL、写 diff 算法、写正则集合、给出「参考实现」。契约里描述的是
**判什么**，不是**怎么算**。

## 5. approve / reject 的 ledger 事件

人的每次拍板是一条 `event_type="decision"` 的事件，结构必须满足
`contracts/event-ledger.schema.json` 的 if-then 约束（payload 必填 `decision_kind` /
`actor` / `target`）。clarify 相关两种：

```json
{
  "event_id": "evt-0007",
  "ts": "2026-09-26T10:00:00+08:00",
  "run_id": "governance",
  "skill": "govern",
  "event_type": "decision",
  "payload": {
    "decision_kind": "approve_pack_diff",
    "actor": "万凌",
    "target": "pack_diff:clarify-2026-09-26-001",
    "rationale": "risk 提到 0.25 有依据：上轮 F08 出现 7 次且 3 次一票否决",
    "proposal_id": "clarify-2026-09-26-001",
    "pack_diff_count": 2
  }
}
```

```json
{
  "event_id": "evt-0008",
  "ts": "2026-09-26T10:05:00+08:00",
  "run_id": "governance",
  "skill": "govern",
  "event_type": "decision",
  "payload": {
    "decision_kind": "reject_proposal",
    "actor": "万凌",
    "target": "pack_diff:clarify-2026-09-26-001",
    "rationale": "risk 权重改动单独成版，不要和 outcome 调整混在同一次冻结里",
    "reason_code": "bundle_too_large"
  }
}
```

字段约定：

| 字段 | 规则 |
|---|---|
| `target` | `pack_diff:<proposal_id>`，proposal_id 形如 `clarify-YYYY-MM-DD-NNN` |
| `actor` | 只能是**人**的标识符。agent 不得作为 actor 写 approve/reject |
| `rationale` | `reject_proposal` **必填**（没有理由的否决无法复盘）；`approve_pack_diff` 强烈建议填，留空时 govern 记 `decision_missing_rationale` 降级标记 |
| `skill` | 追加事件的 skill 名，即 `govern`（ledger 的写入权在 govern，clarify 不代写） |
| `run_id` | 治理期事件统一 `governance`；若与某轮评测绑定则用该 run_id |

## 6. 提案状态机

```
draft ──人看──▶ pending_approval ──approve_pack_diff──▶ approved
                                            └──reject_proposal──▶ rejected（带理由）
approved ──govern bump v(N+1)──▶ frozen（新 frozen_hash）
```

关键不变量：**`pending_approval` 与 `approved` 都不等于「已生效」**。生效的唯一定义是
`pack.manifest.json` 出现新的 `frozen_hash` 且 `state=FROZEN`。在此之前，下游 skill
读到的还是旧 `pack_hash`，两轮回归不可比。
