# references/decision-ledger-spec.md — 决策账本与黄金决策池

event ledger 里的 `decision` 事件是**黄金决策池的原始条目**：人的每一次拍板都落成一行，
只追加、不改写。govern 的价值不在于存，而在于把这些散落的拍板
投影成「下一步该改什么」的证据。

---

## 1. 账本形态与不可变约束

```
governance/events.jsonl
evt-0001 {"event_id":"evt-0001","ts":"…","run_id":"governance","skill":"govern",
          "event_type":"decision","payload":{…}}
evt-0002 {"event_id":"evt-0002", … "event_type":"checkpoint","payload":{…}}
```

| 约束 | 落实方式 |
|---|---|
| 只追加 | `append` 仅以 `open("a")` 写入；无编辑、无重排、无删除入口 |
| 幂等 | 同 `event_id` 已存在则跳过（返回 `exists`）；内容冲突是异常，不是覆盖的理由 |
| 可校验 | 每条写入前按 `contracts/event-ledger.schema.json` 的 if-then 约束校验 |
| 坏行不吞 | 无法解析的行跳过并计数，`proposals.json` 记 `corrupt_lines` + `ledger_corrupt_lines` 标记；**govern 不修历史** |

顶层字段：`event_id` / `ts`（ISO-8601）/ `run_id` / `skill` / `event_type`
`payload`。`event_type` 在治理语境下的用法：

| event_type | 何时发 | payload 要点 |
|---|---|---|
| `decision` | 人拍板（黄金池入口） | `decision_kind` / `actor` / `target` / `rationale` |
| `checkpoint` | freeze 成功 | `artifact` / `hash` / `resumable_from=false` |
| `progress` | bump 等中间动作 | `action` / `version` / `change_summary` |
| `degraded` | verify 发现 hash 不一致等 | `degraded_flags` |
| `failed` | 输入非法、命令被拒 | `reason` |

---

## 2. 六种 decision_kind 的契约

`decision_kind` 枚举由 `contracts/event-ledger.schema.json` 固定，**不可扩展**
（扩展 = 改契约 = 走契约自身的版本流程）：

| decision_kind | 发起的动作 | `target` 约定 | 额外 payload | `rationale` |
|---|---|---|---|---|
| `accept_card` | PM 接受一张证据卡 / 一类建议 | `card:<Fxx\|dimension>` | — | 建议填 |
| `override_priority` | PM 推翻系统的「先修这个」排序 | `priority:<Fxx\|dimension>`（report 侧实际写被改的 `cluster_id`，见下方注） | `taxonomy_id`, `to_rank`, `system_top`, `human_top` | `rationale` 必填 |
| `rename_cluster` | PM 把聚类改了名 | `cluster:<cluster_id>` | `old_label`, `new_label` | 建议填 |
| `reject_proposal` | PM 否决一条提案 | `proposal:<P-xxx>` 或 `card:<Fxx>` | `reason_code` | **必填** |
| `approve_pack_diff` | PM 批准 pack diff | `pack_diff:<proposal_id>` | `proposal_id`, `pack_diff_count` | 强烈建议填 |
| `thaw_pack` | 人显式解冻 | `pack:<pack_id>` | `from_version` | **必填** |

`target` 统一走 `命名空间:键` 形式。这不是装饰：统计投影完全依赖它来分组，
`card:F02` 与 `card:F08` 能被区分，才有「PM 对哪类建议反复拒收」这种可算的问题。

`actor` 只能是人的标识符。agent 不得作为 actor 写 approve/reject/thaw——
否则黄金池混入机器的自我评价，整个校准链就废了。

> **`override_priority` 的权威字段以 `contracts/cross-skill-interfaces.md` §B 为准**
> （v0.2 联调补充；早期草案引用的 card-spec §8.1 从未落盘，内容已收编进该契约文件）。
> `target` 用本文件自 v0.1 起的命名空间约定 `priority:<Fxx>`；PM 语义的
> `cluster_id`（如 `C03`）如需回溯放可选的 `payload.cluster_id`。
> **写严读宽**：写入侧 `validate_event` 强制带 `:` 前缀，拒收裸 id；
> 读取侧（`_group_key()` 只取 `:` 后的部分）对 v0.1 旧账本的裸 id 原样通过。
> 机器匹配唯一认 `payload.taxonomy_id`（= `category_id`）——
> prioritize 的排序表 `by_id` 以它为键，只靠 `target` 匹配会永远落空
> （v0.1 真实故障：PM 改了排序，下一轮毫无反应且无报错）。
> `system_top` / `human_top` 从"建议填"升为**必填**：缺其一则该事件不可投影，
> 计入 `proposals.json` 的 `override_audit.unusable`，不产出提案也不静默吞掉。

---

## 3. 三种统计投影（govern 的全部统计能力）

三条投影的输出形状统一为 `{signal, evidence, target_file, field, current, proposed: null,
direction, status: pending_human_approval}`。共同纪律：
**只报计数与比例，不裁决，不给数值。**

### 投影 1 `priority_weight_calibration` — 优先权重校准信号

- 输入：`accept_card` + `reject_proposal`，按 `target` 的键分组（`F02` 或维度名）。
- 统计：`support = accept + reject`，`accept_rate = accept / support`。
- 触发：`support ≥ MIN_SUPPORT(=2)`。
- 方向：`accept_rate > 0.5` → `raise`，否则 `lower`。
- 指向：`F\d\d` 键 → `taxonomy.categories[id=].default_severity`（备选
  `thresholds.prioritize.fixability_boost.<Fxx>` / `severity_weights`）；
  维度名键 → `rubrics.dimensions[id=].weight`。
- 语义：PM 长期拒收某类建议，说明系统高估了它的严重性或可修复性
  （或 taxonomy 把它分错了类——需要人分辨，机器不分辨）。

### 投影 2 `taxonomy_gap` — 分类体系缺口信号

- 输入：`rename_cluster` 的 `new_label`。
- 统计：同一人工标签被用了多少次（`support`）、涉及哪些 cluster。
- 触发：同名标签 `support ≥ MIN_SUPPORT`。
- 方向：`add`（建议在 `taxonomy.categories[]` 增类），并自动算出下一个
  未占用编号（当前 F01-F12、F99 已用 → `F13`）。
- 语义：人反复把不同 cluster 改叫同一个名字，说明**分类体系里没有一个类能装下这个真实模式**。
  这比权重量化重要得多——分类缺口会让后续所有频率统计失真。
- 命名红线：新类 id 由编号扫描得出，**类名取自人的 label（人给的事实），不是 agent 编的**。

### 投影 3 `fixability_prior_calibration` — 可修复性先验校准信号

- 输入：`override_priority` 的 `(system_top, human_top)`，两者都必须是 `F\d\d` 的
  `category_id`（v0.2 起缺一或形状不对即不可投影）。
- 统计：同一对被 override 的次数。
- 触发：`support ≥ MIN_SUPPORT` 且 `system_top` 非空。
- 方向：`lower`（系统高估了 `system_top` 的可修复性/严重性，
  所以把它排到了人不会选的位置）。
- 指向：`thresholds.prioritize.fixability_boost.<system_top>`
  （备选 `thresholds.prioritize.severity_weights`）。
- 语义：`thresholds.yaml` 里 v0.1 的 `fixability_boost` 是人工先验。
  本投影是它唯一的真实数据来源。
- **v0.2 起附带 `override_audit`**（`proposals.json` 顶层）：
  `usable` / `unusable` / `reasons` / `taxonomy_ids`。投影出不来信号时先看它 ——
  "人没有推翻过排序"和"上游没写全 payload"在旧版输出里长得一模一样。

`top_recommendation_count: 1`（pack 内常量）让这个信号更锋利：
证据卡只给一个「先修这个」，所以每一次 override 都是一次**完全落空**，
不是部分失准。

---

## 4. 投影如何变成 pack diff 提案（完整链路）

```
人的拍板 ──▶ event ledger（decision 事件，append-only）
                 │
        govern proposals（三类统计投影）
                 │
        proposals.json（status=pending_human_approval, proposed=null）
                 │
        人看后决定 ──▶ approve_pack_diff（decision 事件）
                 │
        clarify compile_proposal（把方向 + 人的数值编译成 pack_diff）
                 │
        govern bump → freeze（新 frozen_hash）
                 │
        下游 skill 在新 pack_hash 上跑，回归以此为基准
```

**中间那一跳（方向 → 数值）必须由人完成。** govern 不填数字，clarify 也不替人填数字
（见 clarify 的 `references/pack-diff-format.md` §2）。
这是整个产品「不自评自」的结构性保证：标准变更的速度永远等于人理解它的速度。

---

## 5. 查询与导出

```
python3 scripts/govern.py ledger query --kind override_priority --actor 万凌
python3 scripts/govern.py ledger export --out ledger-export.json
```

`export` 输出四组计数：`by_decision_kind` / `by_actor` / `by_target_key` / `by_event_type`，
外加 `events` 总数与 `corrupt_lines`。导出是**快照**，不是新的事实源；
任何「以导出口径为准」的争论都应回到 `events.jsonl`。

---

## 6. 冷启动与数据稀薄

新系统 ledger 里 decision 事件接近 0，三条投影会安静地空转
（`golden_pool_cold` 标记，`proposals: []`）。这是正确行为：
**没有人的拍板，就没有校准信号。** 前几个版本的权重只能靠澄清期访谈（clarify）确定，
黄金池在第 2-3 轮真实使用后才会开始说话。这也解释了为什么
`MIN_SUPPORT = 2` 而不是 5：单人 PM 场景下阈值设高，信号永远出不来。
