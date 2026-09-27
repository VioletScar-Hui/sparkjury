---
name: sparkjury-govern
description: SparkJury pack lifecycle governance (sparkjury-govern)：scenario-pack 生命周期治理 + 决策账本 + 黄金决策池。冻结/解冻/校验评测标准、维护 v0.1→v0.2 版本链、管理 append-only event ledger、把人的每次拍板投影成三类统计信号（accept/reject 校准、簇重命名=taxonomy 缺口、排序 override=fixability 先验）。当用户说「冻结这个标准」「解冻要改标准」「这个 pack 还是原来那个吗」「PM 为什么总否掉这类建议」「导出决策记录」「为什么不能自我迭代标准」「哪一版标准更严」「上一轮为什么不能和这轮比」时用我；也用于回归前的 pack_hash 一致性校验与治理审计。我不参与评测数据流，不自动应用任何标准变更。
license: MIT
compatibility: 治理层方法论 skill，无独立容器。骨架脚本 scripts/govern.py 只用 Python 标准库，Python 3.10+ 任意环境可跑 --selftest；freeze/thaw/verify 通过 subprocess 调用 sparkjury 根 tools/pack_freeze.py（唯一 hash 实现，本 skill 不复制它）。默认读写 <repo>/governance/{events.jsonl, governance_log.json, pack_history.json, proposals.json}，不写 standards/scenario-pack/ 内容文件，只写 pack.manifest.json 的治理字段（version/state/history；该文件被 pack_freeze.py 排除在 frozen_hash 之外）。
metadata:
  author: "万凌"
  version: "0.1.0"
  product: sparkjury
  tags: "governance, scenario-pack, event-ledger, golden-decision-pool, freeze-thaw"
allowed-tools: Read Bash Write
---

# sparkjury-govern — pack 的生命周期治理 + 黄金决策池

评测系统最贵的失败不是打分不准，是**标准悄悄换了**。govern 是唯一被授权动 pack 生命周期的
skill：它管冻结/解冻、管版本链、管 append-only 决策账本，并把账本里人的每次拍板
投影成「下一步该改什么」的证据——但**它永远不自己改**。

**sparkjury 移植说明（2026-09-27）**：本 skill 由 eval-agent 的 `govern` 迁入 sparkjury，
改名 `sparkjury-govern`，运行时无对应 CLI（净新增，属治理层桥接 skill）。
路径适配：`scripts/pack_freeze.py` → `tools/pack_freeze.py`（仍是 `frozen_hash` 唯一实现，
只 import 不自造）；`PACK_DIR` 从根 `scenario-pack/` 改为 `standards/scenario-pack/`
（与 `tools/pack_freeze.py` 的「先存在者优先」解析一致）；
`proposals.json` 的 `target_file` / `candidate_fields` 一律带 `standards/scenario-pack`
前缀（`scripts/_golden_pool.py:PACK_PREFIX`）。
**路径守卫语义不变**：`bump/freeze/thaw/verify/history` 只作用于 `tools/pack_freeze.py`
实际管辖的 `standards/scenario-pack/`，传入 /tmp 副本一律退出码 2 且零写盘。
入口：`python3 scripts/run.py --selftest`（薄包装，内部 exec `scripts/govern.py`）。

一句话契约：**动 pack 必须有据，改标准必须有人，统计只是提案。**

## 何时用我

- 用户要冻结/解冻标准：「冻结」「先别急，标准要改」「解冻一下」。
- 用户问可比性：「这个 run 和上轮能比吗？」「pack 还是原来那个吗？」「哪一版标准更严？」→ `verify` / `history`。
- 版本链问题：「v0.1 是哪天冻的、改了什么？」→ `history`。
- 黄金池问题：「PM 为什么总否掉这类建议？」「哪类失败老是改名？」→ `proposals`。
- 账本管理：「PM 这周拍了哪些板？」「导出决策记录」→ `ledger query/export`。
- 拒绝自迭代：「系统自己根据结果调高标准」→ 我拒绝，只出提案。

## 输入

| 输入 | 来源 | 说明 |
|---|---|---|
| pack 生命周期状态 | `standards/scenario-pack/pack.manifest.json` | `state` / `version` / `frozen_hash` / `history` |
| 决策账本 | `governance/events.jsonl`（默认） | append-only JSONL，一行一事件 |
| 冻结校验 | `tools/pack_freeze.py --verify` | 唯一 `frozen_hash` 实现，我调用它 |
| 五元组上下文 | `contracts/run-manifest.schema.json` | `pack.pack_hash` 是回归可比性的唯一凭据 |
| ledger 契约 | `contracts/event-ledger.schema.json` | `decision` 事件 = 黄金池原始条目 |

## Steps

0. **校验输入**：`pack.manifest.json` 不可读 / `tools/pack_freeze.py` 缺失 → `failed` 事件，退出码 2。
1. **freeze**（DRAFT → FROZEN）：
   `--summary` 必填（变更摘要进 `pack_history.json`）→ 调 `pack_freeze.py` → 读回
   `frozen_hash` → 追加 `checkpoint` 事件（`artifact`=pack、`hash`、`resumable_from=false`）
   → 写 `pack_history.json` + `governance_log.json`。
   已是 FROZEN 且 `--verify` 通过 → 幂等返回，不重复冻结。
2. **thaw**（FROZEN → DRAFT）：
   `--reason` **必填**，无理由直接拒绝（退出码 2）。顺序固定：**先追加
   `decision.thaw_pack` 事件留据，再调 `pack_freeze.py --draft`，最后写 history/log**。
   动作发生在证据之后，不留无据解冻。
3. **verify**：调 `pack_freeze.py --verify`，结果（含 hash、state）写 `governance_log.json`；
   不一致则发 `degraded` 事件 + `degraded_flags=["pack_hash_mismatch"]` 并返回非零。
4. **bump**（版本链）：语义化版本 +1（默认 minor：内容标准变更；patch：只修叙述），
   把 `{version, change_summary, decision_event_id, actor}` 追加进 `pack.manifest.json:history`。
5. **ledger**：
   - `append`：按 `contracts/event-ledger.schema.json` 校验后才写，按 `event_id` 幂等去重；
   - `query`：按 `decision_kind` / `actor` / `target` 过滤；
   - `export`：按 kind / actor / target key / event_type 计数导出。
6. **proposals**（黄金池三类投影）：见下节。产出 `proposals.json`，
   每条 `status="pending_human_approval"`、`required_decision_kind="approve_pack_diff"`。
7. **history / log**：从 ledger + manifest history 重建 `pack_history.json`；
   `governance_log.json` 为追加型治理动作日志。

## 黄金决策池：三类统计投影

`decision` 事件是黄金池的原始条目（`contracts/event-ledger.schema.json`）。
govern 只统计，不裁决：

| 投影 | 输入决策 | 统计什么 | 指向 pack 哪一层 |
|---|---|---|---|
| `priority_weight_calibration` | `accept_card` / `reject_proposal` | PM 对某类建议的 accept_rate（按 target key 分组） | `taxonomy.categories[].default_severity` / `rubrics.dimensions[].weight` |
| `taxonomy_gap` | `rename_cluster` | 同一人工标签被重复使用几次（分类体系没覆盖的真实模式） | `taxonomy.categories[]` 新增类（自动给出下一个可用 `F13` 号） |
| `fixability_prior_calibration` | `override_priority` | `(system_top, human_top)` 对被 override 的次数 | `thresholds.prioritize.fixability_boost.*` |

- `MIN_SUPPORT = 2`：同一信号重复 2 次即值得看。这是**契约常量**不是经验阈值——
  单人 PM 场景下数据稀薄，阈值设高信号永远出不来。
- **投影只给方向，不给数字**：每条提案 `proposed=null` + `direction` + `current`（pack 现值）。
  数值由人定，clarify 负责编译成 pack diff。govern 从不发明权重。
- **proposal ≠ 生效**：`proposals.json` 没有任何执行能力。生效链路固定是
  人批准（`approve_pack_diff`）→ clarify 编译 diff → `bump` → `freeze`。

## pack 状态机（详版见 references/state-machine.md）

```
DRAFT ──freeze(需 checkpoint 证据)──▶ FROZEN ──thaw(需 decision.thaw_pack 理由)──▶ v(N+1) DRAFT ──▶ FROZEN
```

- DRAFT 的唯一入口是 `thaw`（由人的解冻指令触发），内容变更意图由 clarify 的提案承载。
- FROZEN：所有 skill 只读；每次 run 的 manifest 钉同一个 `pack_hash`。
- 回归铁律：前后两轮对比必须同 `pack_hash`；换了 pack = 新一轮评测，不是回归。


## Add/Thin：规则消费证据（--runs-dir，v0.1 新增）

外部情报（2026-09-27）："Harness 组件不是永久资产，去留由真实任务的运行证据决定。"
`proposals --runs-dir <runs>` 扫描历史 run 产物（clusters.json 或 card/card.json），
在 proposals.json 输出 `thin_candidates`：定义了但从未被任何 badcase 消费过的
taxonomy 类与 fixability 系数——下一次 pack 解冻时的 Thin 讨论清单。

三条防线：①F99 人工兜底队列永不进候选；②零 run 产物 = 零证据，不产生候选；
③观测标签与 pack 类**零交集**时判 namespace_mismatch（如运行时 FailureLabel vs
pack F 编号，正是 D3 待裁决项）并拒绝产候选——空间错配的"全没用过"是假象，
照单全收会误删整套分类。只给方向：删除仍必须走 clarify 提案→人批准→冻结。

## Edge cases

| 分支 | 触发 | 行为 | degraded_flags |
|---|---|---|---|
| 幂等冻结 | state==FROZEN 且 verify 通过 | 不重复冻结，返回 0 | — |
| 冻结冲突 | state==FROZEN 但 verify 失败 | 拒绝冻结，提示先定位漂移或 thaw | `pack_hash_mismatch` |
| 无据解冻 | `thaw` 缺 `--reason` | 拒绝，退出码 2 | — |
| 非法状态 | state 不是 DRAFT/FROZEN | 拒绝，退出码 2 | — |
| 坏账本行 | ledger 有无法解析的行 | 跳过并计数，proposals.json 记 `corrupt_lines` | `ledger_corrupt_lines` |
| 拒绝漏理由 | `approve_pack_diff` 的 `rationale` 为空 | 允许落账，但记 `decision_missing_rationale` | `decision_missing_rationale` |
| 黄金池冷启动 | decision 事件 < MIN_SUPPORT | 0 条提案 | `golden_pool_cold` |

## 红线（违者即 fail）

- **任何 pack 内容变更必须可追溯到一条人批准事件**（`approve_pack_diff`）。
  freeze 的 `--summary` 不替代批准；没有 `--decision-event` 也能冻，但 history 会留空，
  这属于可审计的缺陷而非合法路径。
- **ledger 只追加不改写**：不做回填、不做重排、不删行、不修坏行（坏行跳过并计数）。
- **govern 不得自动 apply 任何 proposal**：`proposals.json` 是给人看的材料，没有执行入口。
- **系统不得自动变更任何标准**：不接受「根据上轮结果自动调权重」这类自迭代请求；
  自迭代最多产出 pack diff 提案给人批准，绝不由系统应用。
- **不自动生成 checker**：与 clarify 同一条红线，checker 只能由人定义。
- **不写 pack 内容文件**：只通过 `pack_freeze.py` 改 `pack.manifest.json` 的
  `state`/`frozen_hash`，以及写自己的治理字段 `version`/`history`。
  `rubrics.yaml` / `taxonomy.yaml` / `thresholds.yaml` / `judges.yaml` / `prompts/` /
  `checkers/` 全部只读。
- **不编造统计口径**：`accept_rate` 等只报告计数与比例，不用「显著」「通常」这类无证据措辞；
  不确定写「未验证」。

## 停止条件

- 用户要求「直接把新权重写进去 / 顺手冻结」而拿不出批准事件 → 停下要证据。
- `verify` 发现 `frozen_hash` 不一致 → 停下，先查是谁改了 pack 内容，不继续后续动作。
- 用户要求「让系统自己根据回归结果改标准」→ 停下拒绝，说明自迭代的边界。
- ledger 出现同一 `event_id` 但内容不同 → 停下报告冲突，不覆盖（幂等只按 id 判重，
  内容冲突是异常，不是覆盖的理由）。

## 输出契约

`governance/pack_history.json` / `governance_log.json` / `proposals.json`（默认写在 `<repo>/governance/`，
`--out-dir` 可改），schema 见
`schemas/io.schema.json`；外层按 `contracts/skill-envelope.schema.json` 包装。
`governance_log.json` 是追加型数组（按 `action_key` 幂等，既有条目不改写）；
`pack_history.json` 与 `proposals.json` 是可重建投影——**唯一真相源是
`governance/events.jsonl` + `pack.manifest.json:history`**。

## 脚本

```
python3 scripts/run.py freeze    --summary "<变更摘要>" [--decision-event evt-0007]
python3 scripts/run.py thaw      --reason  "<解冻理由>"      # 无理由则拒绝
python3 scripts/run.py verify
python3 scripts/run.py bump      --summary "<变更摘要>" [--level minor|patch]
python3 scripts/run.py ledger    append --kind accept_card --actor 万凌 --target card:F02 [--rationale "..."]
python3 scripts/run.py ledger    query [--kind override_priority] [--actor 万凌]
python3 scripts/run.py ledger    export [--out ledger-export.json]
python3 scripts/run.py proposals
python3 scripts/run.py history
python3 scripts/run.py --selftest
```

路径参数：`--pack-dir`（默认 `standards/scenario-pack/`）、`--out-dir`（默认 `governance/`）、
`--ledger`（默认 `governance/events.jsonl`）。

模块拆分（单文件 600 行上限，`.claude/rules/coding.md`）：

| 文件 | 职责 |
|---|---|
| `scripts/run.py` | sparkjury 规范薄包装（exec 下列主脚本，参数透传） |
| `scripts/govern.py` | CLI 入口 + `ledger`/`proposals` 两个 subcommand + 内嵌 selftest；re-export 其余模块的公开符号 |
| `scripts/_ledger.py` | 账本层：基础 JSON IO + 事件校验（按 `contracts/event-ledger.schema.json`）+ 幂等追加 |
| `scripts/_golden_pool.py` | 黄金决策池投影：pack 事实读取 + override payload 可投影性审计 + 三类统计投影 |
| `scripts/_lifecycle.py` | pack 生命周期：`freeze`/`thaw`/`bump`/`verify`/`history` + 追加型治理日志 |

⚠️ **路径守卫**：`bump/freeze/thaw/verify/history` 只作用于 `tools/pack_freeze.py`
实际管辖的 `<repo>/standards/scenario-pack/`；传入其它路径（如一个副本）一律退出码 2 且零写盘。
原因：`pack_freeze.py` 的 `PACK_DIR` 由它自身位置决定，在副本上「冻结」会得到一份与真实
pack 无关的 hash，这种静默错位比报错危险得多。只读统计（`proposals`/`ledger`）不受限。

## 移植与差异说明

- `references/sparkjury-port-notes.md` — 2026-09-27 从 eval-agent 迁入 sparkjury 的路径适配清单（哪些硬编码改成了 `tools/` 与 `standards/scenario-pack/`）以及与 sparkjury 运行时的语义差异。**差异只记录，不私自统一。**

