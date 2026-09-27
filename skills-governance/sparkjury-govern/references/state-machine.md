# references/state-machine.md — pack 生命周期状态机

scenario pack（standards/scenario-pack/）的生命周期只有两个状态、三条迁移。**少即是安全**：迁移越少，
「标准是什么时候变成这样的」这个问题越好回答。

---

## 1. 状态机全图

```
                     ┌──────────────────────────────────────────────┐
                     │                                              │
                     │           人的显式解冻指令 + 理由             │
                     │                                              │
   clarify 提案 ──▶  DRAFT  ◀──────────────────────────────────┐   │
                     │  │                                     │   │
   人批准            │  │ freeze：调 tools/pack_freeze.py     │   │
   approve_pack_diff │  │ 需 --summary，产出 frozen_hash         │   │
        + bump      ▼  ▼                                       │   │
                  FROZEN ──────────────────────────────────────┘   │
                     │      thaw：必须先落 decision.thaw_pack       │
                     │                                             │
                     └──────────────────────────────────────────────┘

回归铁律：两轮对比必须同一个 frozen_hash。换了 pack = 新一轮评测，不是回归。
```

状态语义：

| 状态 | 谁能写 | 谁能读 | 用途 |
|---|---|---|---|
| `DRAFT` | 人（在 approve_pack_diff 之后编辑 pack 内容文件）；govern 只写 manifest 治理字段 | 全部 skill | 标准修订期 |
| `FROZEN` | 无人直接写内容；只有 `pack_freeze.py` 能改 manifest 的 `state`/`frozen_hash` | 全部 skill 只读 | 评测执行期，每次 run 的 manifest 钉同一个 `pack_hash` |

---

## 2. 每条迁移的前置条件与 ledger 证据

| 迁移 | 命令 | 前置条件 | 必填 | ledger 证据 | 失败即拒的情形 |
|---|---|---|---|---|---|
| — → DRAFT | `thaw` | `state == FROZEN` | `--reason`（非空） | **先**追加 `decision.thaw_pack`（`target=pack:<pack_id>`，`rationale` 必填），再调 `pack_freeze.py --draft` | 缺理由、为空理由、`decision_kind` 不在枚举 |
| DRAFT → FROZEN | `freeze` | `state == DRAFT` | `--summary` | 冻结成功后追加 `checkpoint`（`artifact`/`hash`/`resumable_from=false`） | state 非 DRAFT；`pack_freeze.py` 返回非零 |
| FROZEN → FROZEN | `freeze` | `state == FROZEN` 且 `pack_freeze.py --verify` 通过 | — | 无新事件（幂等） | verify 失败 → 拒绝并提示 `pack_hash_mismatch` |
| 版本自增 | `bump` | `version` 可解析为 semver | `--summary` | `progress` 事件记录 version 变更；`--decision-event` 挂批准事件 id | version 非 semver |
| 校验 | `verify` | — | — | 结果写 `governance_log.json`（不写 ledger） | — |

**顺序不可换的原因**：thaw 若先动作后记账，一次崩溃就会留下「已解冻但无理由」的状态。
pack 的教训是：**证据先于动作。**

---

## 3. 证据链长什么样

一次完整的标准变更，账本上必须能看到这条链（缺少任何一环都算治理缺陷）：

```
evt-0004  progress   govern      bump v0.1.0→v0.2.0（change_summary, decision_event_ref=evt-0007）
evt-0005  checkpoint govern      artifact=standards/scenario-pack/retail-default hash=3d73… resumable_from=false
evt-0007  decision   万凌        approve_pack_diff target=pack_diff:clarify-2026-09-26-001 rationale=…
evt-0009  decision   万凌        thaw_pack        target=pack:retail-default  rationale=…（下一次变更的起点）
```

注意 `evt-0007`（人的批准）时间上早于 `bump`/`freeze`——**先有批准，后有版本**。
如果 freeze 时没传 `--decision-event`，history 里的 `decision_event_id` 为空，
这在治理审计里是可发现的缺陷（`decision_missing_rationale` 一类标记）。

---

## 4. DRAFT 期谁改 pack 内容文件

状态机只规定「谁能改状态」，不规定「谁改内容」。内容写入的实际规则：

- **DRAFT 态**：人（或人在批准后明确授权的编辑动作）改 `rubrics.yaml` /
  `taxonomy.yaml` / `thresholds.yaml` / `judges.yaml` / `prompts/` / `checkers/`。
- **clarify** 不写这些文件：它产出 diff 提案，人是执行的最后一环。
- **govern** 也不写这些文件：它只写 `pack.manifest.json` 的治理字段
  （`version` / `state` / `frozen_hash` / `history`）。
  该文件被 `tools/pack_freeze.py` 排除在 `frozen_hash` 计算之外
  （`SKIP = {"pack.manifest.json"}`），因此改它不会污染内容哈希——
  这正是把治理元数据放在 manifest 的原因。

---

## 5. 不可跨越的边界（红线校验清单）

1. FROZEN 态下任何 skill 读到的 pack 必须一致 → `verify` 是唯一校验入口。
2. 不存在「DRAFT → DRAFT」的静默重冻结：`freeze` 必须拿到新的 `frozen_hash`。
3. 不存在无理由解冻：`thaw` 无 `--reason` 直接退出码 2。
4. 不存在由系统发起的标准变更：自迭代最多产出提案（`proposals.json`）。
5. 不存在手改 `pack.manifest.json` 的 `state`/`frozen_hash`：一律走 `pack_freeze.py`，
  保证 hash 计算只有一份实现。

---

## 6. 已知限制（诚实记录）

- `tools/pack_freeze.py` 的 `frozen_at` 是**人工确认日期常量**（当前 `2026-09-26`），
  脚本不读系统时间以保证可复现。因此 manifest 的 `frozen_at` 不适合做权威时间戳；
  权威时间在 ledger 的 `checkpoint` 事件 `ts` 与 `governance_log.json`。
- `pack_freeze.py` 自己不做版本自增，版本链由 govern 的 `bump` 补齐。
  这是有意为之：版本是治理语义（内容标准变了才 minor），不是 hash 的附属品。
- `frozen_hash` 覆盖 `standards/scenario-pack/` 下除 manifest 外的**全部文件**，
  包括 `prompts/` 与 `checkers/`：改一个 prompt 也是新 pack。
