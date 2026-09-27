# Skill card: sparkjury-calibrate

## Description: <br>
SparkJury 给 LLM 裁判做上岗体检：用校准集在正式打分前测量每个裁判×维度 vs 金标的可靠性，产出裁判画像与 PASS/DEGRADE/FAIL 门禁，并生成 judges.yaml override 提案交 govern。当用户说「裁判准不准」「校准裁判」「judge 可靠性」「裁判画像」「上岗门禁」「agreement 对齐率」「裁判漏判」「over/under 打分偏差」「顺序交换敏感度」「lead judge 选谁」「Jev 不可达谁来兜底」「裁判提示词要不要改」「先校准再打分」「小模型当裁判行不行」时使用。同样用于换裁判模型后重跑体检、某维度裁判争议过大、以及回归前确认尺子有没有变。 <br>

This skill is ready for commercial/non-commercial use. <br>

## Owner
万凌 <br>

### License/Terms of Use: <br>
MIT <br>

**Version:** 0.1.0 &nbsp;|&nbsp; **Product:** SparkJury agent evaluation harness &nbsp;|&nbsp; **Skill directory:** `skills/sparkjury-calibrate/` <br>

## Use Case: <br>
评测工程团队与 AI agent 在正式打分前需要确认"尺子准不准"。三裁判跨模型族跑本地 vLLM，
但裁判本身有已知系统性毛病（同族自评掉点、位置偏误、漏判罕见失败、倾向于打中间分），
不测就会把小模型的漏判当成被评 agent 的成绩。本 skill 用有外部真值的校准集逐 (裁判×维度) cell
度量 agreement、over/under 方向偏差、score 方差、顺序交换敏感度、时延，给出 PASS/DEGRADE/FAIL
门禁与 lead judge 排序，并把改动建议收敛成 judges.yaml/pack 的 override 提案交 govern 人批。
解决的核心问题：让"这条 badcase 没被报出来"这类静默失效在打分前就暴露，而不是等回归。 <br>

### Deployment Geography for Use: <br>
Local (DGX Spark) / Cloud hybrid per run manifest <br>

## Requirements / Dependencies: <br>
**Requires API Key or External Credentials:** None（本 skill 不调用模型，只消费已产出的投票 JSON 与金标；三裁判本身跑本地 vLLM 端点，无需外部凭据） <br>
**Credential Type(s):** None <br>

Do not include secrets in prompts/logs/output; use least-privilege credentials; rotate keys as appropriate. <br>

## Known Risks and Mitigations: <br>
Risk: 最隐蔽的失效是"跳过校准直接打分"和"用裁判互投当金标"。前者放弃唯一的外部基准，
后者是循环论证让画像系统性偏乐观；两者的共同症状都是下游静默失效 —— 漏判的 badcase 不进 cluster、
不进证据卡，报告看起来干净，而问题要等回归才暴露，甚至永远不暴露。<br>
Mitigation: SKILL.md 把"不得跳过校准""不得把裁判互投当金标"列为红线，违者即 fail；
金标只接受人注册的 checker 与 tau2 自带终态判定（pack checkers/README.md 已定）；
无金标的维度显式标 no_gold_for_dimension 而不是找个代理顶上。<br>

Risk: 第二类失效是阈值缺失时给出看起来判过的结论。pack v0.1 没有 thresholds.calibrate.*，
此时若按"没报警就当通过"行事，整个运行会建立在没验证过的裁判上。<br>
Mitigation: 门禁四条状态里 UNKNOWN 优先于所有阈值判定，缺阈值一律 UNKNOWN + calibrate_thresholds_undefined
告警 + 补阈值提案；唯一在无阈值时仍然生效的是阈值无关的零方差退化裁判规则。<br>

Risk: 第三类失效是校准层越界自己改评测标准（改 rubric / 改 prompt / 删裁判）来"让数字好看"。<br>
Mitigation: 画像只出 proposals[]，每条带 agreement/n/维度/裁判 四项证据，去向 govern 的
decision_kind=approve_pack_diff，人批准才生效；SKILL.md 红线明令不得自行改 rubric 或 panel。<br>

## Reference(s): <br>
- `references/reliability-metrics.md` — 六个指标的定义/公式/最小样本量推导/方差注意事项 <br>
- `references/gate-policy.md` — 四态门禁语义、降级路径、lead judge 选择机制、与 arbitrate 的联动、提案边界 <br>

- `references/sparkjury-port-notes.md` — 移植适配清单 + 与 sparkjury 运行时的语义差异（只记录，不统一） <br>
## Skill Output: <br>
**Output Type(s):** JSON <br>
**Output Format:** 统一 skill envelope（`contracts/skill-envelope.schema.json`）+ payload 契约（`schemas/io.schema.json`） <br>
**Output Parameters:** 多维（judges 画像 / gates / lead_judges / proposals / coverage / provenance） <br>
**Other Properties Related to Output:** `degraded_flags`（`calibrate_thresholds_undefined` / `calibrate_insufficient_n` / `calibrate_dim_without_gold` / `judge_no_discrimination_<judge>_<dim>` / `judge_degraded_<judge>_<dim>` / `panel_reduced_to_2` / `jev_unreachable`）；产出带 run manifest 五元组引用，含三裁判模型版本 <br>

## Evaluation Agents Used: <br>
- Claude Code <br>
- Codex <br>

## Data handling: <br>
所有输入均为本地 JSON（投票 / 金标 / calibration 集）与本地 FROZEN pack 的只读内容；本 skill 不调用任何模型、不访问网络、不写 pack、不落任何密钥或令牌。输出的 `judge_profile.json` 含裁判模型名与 agreement 数值，属内部评测数据，按团队仓库范围共享。 <br>

## Risk level: <br>
**low** — 只读输入 + 只写调用方指定的输出文件；无外部端点、无凭据、不改评测标准。 <br>
