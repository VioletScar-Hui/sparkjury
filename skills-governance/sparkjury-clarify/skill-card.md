# Skill card: sparkjury-clarify

## Description: <br>
SparkJury 澄清期唯一入口：把 PM 的自然语言评测标准描述编译成 `standards/scenario-pack` diff 提案，人批准后才可能生效。 <br>

This skill is ready for commercial/non-commercial use. <br>

## Owner
万凌 <br>

### License/Terms of Use: <br>
MIT <br>

**Version:** 0.1.0 &nbsp;|&nbsp; **Product:** SparkJury agent evaluation harness &nbsp;|&nbsp; **Skill directory:** `skills-governance/sparkjury-clarify/` <br>

## Use Case: <br>
PM / 评测标准 owner 在开始一轮评测前，把脑子里的「什么叫做好」翻译成 pack 里可执行的字段
（维度权重、outcome 判据路径、一票否决清单、回归口径），并留下可追溯的批准记录。
用于：初始定标准、换域定标准、按黄金池统计信号微调权重、审查既有 pack 为什么这样定。 <br>

### Deployment Geography for Use: <br>
Local (DGX Spark) — 治理期人机协作，无推理端点依赖 <br>

## Requirements / Dependencies: <br>
**Requires API Key or External Credentials:** <None — 本 skill 不调任何外部服务；checker 契约提案也不含实现> <br>
**Credential Type(s):** <None> <br>

Do not include secrets in prompts/logs/output; use least-privilege credentials; rotate keys as appropriate. <br>

**运行时依赖：** Python 3.10+ 标准库，零第三方硬依赖 —— pack 的 YAML 由 sparkjury 根 `tools/_yaml_lite.py`（全库唯一实现）解析：有 PyYAML 走 PyYAML，无 PyYAML 走内置子集解析器，解析失败显式报错而非静默错读。只读 `standards/scenario-pack/`。 <br>

## Known Risks and Mitigations: <br>
Risk: 澄清期是标准被「随口定义」的高危期 —— agent 连环追问会把 PM 逼给出不负责的权重，
或把方向性意见固化成精确数字，事后无人记得依据。 <br>
Mitigation: 五问硬预算 + 方向型答案只产出 `proposed=null` + pending_item（不替人填数字）
+ 每条 diff 必须挂 rationale 与 ledger 依据；零改动短路避免为改而改。 <br>

Risk: 提案被人批准后未真正冻结，下游仍按旧 `pack_hash` 跑，两轮 regression 被误认为可比。 <br>
Mitigation: 提案状态机区分 pending_approval / approved / frozen；`frozen_hash` 与版本链
只由 govern 写；SKILL.md 明示「换了 pack = 新一轮评测」。 <br>

## Reference(s): <br>
- `references/interview-protocol.md` — 五问的设计原理：每问对应哪个 pack 字段、为什么这五个维度足够、过度追问的代价 <br>
- `references/pack-diff-format.md` — pack_diff 提案 JSON 格式、影响面分析规则、approve/reject 的 ledger 事件定义 <br>
- `contracts/event-ledger.schema.json`（只读）— decision 事件（黄金决策池原始条目）的 schema 约束 <br>
- `standards/scenario-pack/README.md`（只读）— pack 状态机与「唯一事实源」定位 <br>

- `references/sparkjury-port-notes.md` — 移植适配清单 + 与 sparkjury 运行时的语义差异（只记录，不统一） <br>
## Skill Output: <br>
**Output Type(s):** [JSON] <br>
**Output Format:** `contracts/skill-envelope.schema.json` envelope + `clarification_session.json`
（schema 见本 skill `schemas/io.schema.json`） <br>
**Output Parameters:** 多维 — `questions_asked[]` / `answers{}` / `proposal.pack_diff[]` /
`proposal.checker_contracts[]` / `pending_items[]` <br>
**Other Properties Related to Output:** `proposal.status` ∈ {zero_change, pending_approval}；
降级标记 `clarify_direction_only` / `clarify_budget_exhausted` / `golden_pool_cold` /
`pack_frozen_requires_thaw`；**不产出任何评分数字** <br>

## Evaluation Agents Used: <br>
- Claude Code <br>
- Codex <br>

## Data handling: <br>
只读本地 pack（`standards/scenario-pack/`）取 `current` 值，不外传；PM 的自然语言原文逐字写进 session JSON 供回溯，可能含业务描述，仅在仓库内流转。不调用任何模型或外部服务，不写 pack，不写任何密钥或令牌。 <br>

## Risk level: <br>
**medium** — 产出的是**评测标准变更提案**（pack diff），影响下游所有 skill 的评分口径；但本 skill 无落盘权、无自动生效路径，必须人批准后由 govern 执行，残余风险是「提案被误读为已生效」。 <br>
