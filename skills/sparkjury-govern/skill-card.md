# Skill card: sparkjury-govern

## Description: <br>
SparkJury scenario-pack（standards/scenario-pack/）生命周期治理 + 决策账本 + 黄金决策池：冻结/解冻/校验评测标准、维护版本链、管理 append-only event ledger、把人的每次拍板投影成三类统计信号。 <br>

This skill is ready for commercial/non-commercial use. <br>

## Owner
万凌 <br>

### License/Terms of Use: <br>
MIT <br>

**Version:** 0.1.0 &nbsp;|&nbsp; **Product:** SparkJury agent evaluation harness &nbsp;|&nbsp; **Skill directory:** `skills/sparkjury-govern/` <br>

## Use Case: <br>
评测标准 owner / PM 在评测流水线之外管理「标准本身」：什么时候能冻结、什么理由才能解冻、
两轮回归凭什么可比、PM 历史拍板沉淀出了哪些校准信号。用于：pack 定版与回归基准固化、
治理审计（谁在何时以什么理由改过标准）、把黄金池统计转成给人看的变更提案、
阻止「系统自己改标准」的自迭代请求。 <br>

### Deployment Geography for Use: <br>
Local (DGX Spark) — 治理期人机协作，无推理端点依赖 <br>

## Requirements / Dependencies: <br>
**Requires API Key or External Credentials:** <None — 不调任何外部服务，不访问网络> <br>
**Credential Type(s):** <None> <br>

Do not include secrets in prompts/logs/output; use least-privilege credentials; rotate keys as appropriate. <br>

**运行时依赖：** Python 3.10+ 标准库；`tools/pack_freeze.py`（唯一 `frozen_hash` 实现，
govern 通过 subprocess 调用它，不复制其逻辑）。 <br>

## Known Risks and Mitigations: <br>
Risk: 冻结/解冻是标准静默漂移的唯一入口 —— 一次无据解冻会让此后的回归对比全部失真，
而漂移在数字上完全看不出来。 <br>
Mitigation: thaw 必须 `--reason`，且**先落 `decision.thaw_pack` 事件再动作**；
freeze 追加 `checkpoint` 事件记录 hash 与版本；`verify` 把一致性结果写进治理日志；
缺理由的批准放行但打 `decision_missing_rationale` 标记。 <br>

Risk: 黄金池统计被当成「系统建议自动改权重」，评测标准失去人的主权，回归失去可比性。 <br>
Mitigation: 投影只给方向不给数字（`proposed` 恒为 null），每条提案 `status=pending_human_approval`，
`proposals.json` 无任何执行入口；生效必须经人 `approve_pack_diff` → clarify 编译 diff →
`bump` → `freeze`。 <br>

## Reference(s): <br>
- `references/state-machine.md` — DRAFT/FROZEN 状态机、每个迁移的前置条件与 ledger 证据要求 <br>
- `references/decision-ledger-spec.md` — ledger 决策事件 schema、黄金池三种统计投影定义、投影如何变 pack diff 提案 <br>
- `contracts/event-ledger.schema.json`（只读）— append-only 账本契约与 decision if-then 约束 <br>
- `tools/pack_freeze.py`（仓库级，只调用不改写）— `frozen_hash` 唯一实现 <br>

- `references/sparkjury-port-notes.md` — 移植适配清单 + 与 sparkjury 运行时的语义差异（只记录，不统一） <br>
## Skill Output: <br>
**Output Type(s):** [JSON, JSONL] <br>
**Output Format:** `governance/pack_history.json` + `governance/governance_log.json` +
`governance/proposals.json`（追加型/投影型，schema 见本 skill `schemas/io.schema.json`）；
ledger 本体为 `governance/events.jsonl`（append-only JSONL） <br>
**Output Parameters:** 多维 — 版本链记录 / 治理动作日志 / 三类统计提案 <br>
**Other Properties Related to Output:** 降级标记 `pack_hash_mismatch` /
`ledger_corrupt_lines` / `decision_missing_rationale` / `golden_pool_cold`；
`proposals[].status` 恒为 `pending_human_approval`，`proposed` 恒为 null <br>

## Evaluation Agents Used: <br>
- Claude Code <br>
- Codex <br>

## Data handling: <br>
读写本地 `governance/` 账本与日志，以及 pack manifest 的治理字段（version/state/history，被 `frozen_hash` 排除）；pack 内容文件只读。ledger 含 PM 的每次拍板与理由，属治理数据，只在仓库内流转；不调用任何模型、不访问网络、不写任何密钥或令牌。 <br>

## Risk level: <br>
**high** — 唯一被授权动 pack 生命周期（freeze/thaw/bump）的 skill，动作直接影响「两轮回归是否可比」；缓解见 Known Risks —— 无据解冻拒绝、hash 唯一实现只 import 不自造、路径守卫拒绝 /tmp 副本、proposals 永不自动 apply。 <br>
