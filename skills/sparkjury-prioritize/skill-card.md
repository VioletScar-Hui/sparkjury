# Skill card: sparkjury-prioritize

## Description: <br>
SparkJury 把 cluster 产出的失败类排成"先修哪一类"的修复顺序，用 frequency_norm × severity_weight × fixability_boost 三项相乘（系数全部读 standards/scenario-pack thresholds.prioritize，禁止自造权重），并且证据卡只推一个 top_recommendation。当用户说"先修哪个"、"优先级怎么排"、"哪一类最该先改"、"排个序"、"修复顺序"、"top 建议"、"override 排序"，或输入是 clusters.json 需要给出行动顺序时使用。消费 PM 在 ledger 里的 override_priority 人工修正作为长期记忆。只排顺序与给理由，不承诺修复成本与收益的绝对值，不定位根因。 <br>

This skill is ready for commercial/non-commercial use. <br>

## Owner
万凌 <br>

### License/Terms of Use: <br>
MIT <br>

**Version:** 0.1.0 &nbsp;|&nbsp; **Product:** SparkJury agent evaluation harness &nbsp;|&nbsp; **Skill directory:** `skills/sparkjury-prioritize/` <br>

## Use Case: <br>
评测 agent 的团队与 PM：已经知道失败有几种，但不知道**先修哪一种**。
千富公司 PM 的典型困境不是"看不到失败"，而是拿到一份失败清单后无从排期 ——
哪一类影响面大、哪一类严重、哪一类有明确修法，三件事天天在脑子里打架。
本 skill 把这三件事乘成一个可复算的数，给出一个顺序，并且**只推一个** top 建议
（人的注意力是稀缺资源，卡片推三个等于没推）。
同时它是长期记忆的入口：PM 若不认可排序直接改，那次拍板进 ledger，
本 skill 下一轮按人工修正重排并在 rationale 注明来源。
它只回答"先修哪一类"，**不承诺修复成本与收益的绝对值，也不定位根因**。 <br>

### Deployment Geography for Use: <br>
Local (DGX Spark)；纯 CPU 标准库确定性计算，无需 GPU、无需模型调用 <br>

## Requirements / Dependencies: <br>
**Requires API Key or External Credentials:** <None>
**Credential Type(s):** 无。本 skill 是确定性计算，不调任何模型或外部服务，
也因此任何人可独立复算其输出。<br>

Do not include secrets in prompts/logs/output; use least-privilege credentials; rotate keys as appropriate. <br>

**Runtime dependencies:** Python 3.9+ 标准库，零第三方依赖（YAML 阈值由
`tools/_yaml_lite.py` 全库唯一实现解析——PyYAML 优先、子集兜底，解析失败显式报错而非静默错读）。 <br>

## Known Risks and Mitigations: <br>
Risk: **fixability_boost 是人工先验，未经校准**（pack v0.1 原文注释："可修复性先验，v0.1 人工设定，黄金池校准"）。
读者可能把它当成有依据的结论，据此排期。 <br>
Mitigation: 每条输出强制带 `fixability_source: human_prior_pack_v0.1_uncalibrated`，
`meta.disclaimer` 显式写明"只表达相对顺序，不承诺成本与收益绝对值"。
系数缺失一律拒排进 `rejected_candidates`，不补默认值 —— 一个静默补上的默认值
会让结论看起来和正常算出来的一样可信。 <br>

Risk: **分母口径不一致**。cluster 的 share 分母含 F99，而 clusters 数组不含 F99 簇；
若本地退化成只累加 clusters.size，同一类会在两个 skill 里显示两个百分比。 <br>
Mitigation: `resolve_total_badcases()` 按 显式入参 &gt; 同目录 cluster_report.json &gt; 累加
取分母，并把来源写进 `meta.total_badcases_source`。selftest 第 7 组三种路径全测。 <br>

Risk: **override hook 被误用成"人的偏好永远压过公式"**，导致公式永不改进。 <br>
Mitigation: hook 语义刻意保守 —— 只提升、不压低，且本轮排名已 ≤ 人工名次时不动。
SKILL.md 停止条件另列一条：若 top 建议与 PM 上次 override 连续冲突，
应反馈"公式缺了一个维度"，而不是反复覆盖人工决策。 <br>

## Reference(s): <br>
- `references/priority-formula.md` — 公式语义、归一化方式、边界情况（severity=3 一票否决类的处理、fixability=0 如何呈现） <br>
- `references/pain-point-mapping.md` — 千富公司 PM"归因后不知道先改哪个"的痛点如何被本 skill 回答（调研报告 §4.3 痛点 4） <br>
- `standards/scenario-pack/thresholds.yaml`（FROZEN）— `prioritize` 段公式与系数唯一真相源 <br>
- `contracts/event-ledger.schema.json` — `override_priority` decision 事件契约 <br>

- `references/sparkjury-port-notes.md` — 移植适配清单 + 与 sparkjury 运行时的语义差异（只记录，不统一） <br>
## Skill Output: <br>
**Output Type(s):** [JSON, event ledger] <br>
**Output Format:** 固定 envelope（`contracts/skill-envelope.schema.json`）+ payload 由
`skills/sparkjury-prioritize/schemas/io.schema.json` 定义 <br>
**Output Parameters:** [一维排序 + 多维解释：rank / priority / frequency / severity / fixability / rationale] <br>
**Other Properties Related to Output:** `top_recommendation_count=1` 强制单推；
`rejected_candidates` 说明每个未排序类缺什么系数；`override_applied` 记录用了哪条人工决策；
`fixability_source` 标注人工先验未经校准；产出带 run-manifest 五元组引用
（trace 版本 + pack_hash + skill 版本 + model 版本 + 输出 hash） <br>

## Evaluation Agents Used: <br>
- Claude Code <br>
- Codex <br>

## Data handling: <br>
输入为上游 cluster 的本地 JSON 与本地 FROZEN pack 阈值；排序是确定性计算，不调模型、不访问网络。ledger 里的 override 事件含 PM 的人工决策与理由，属治理数据，只在仓库内流转；不写 pack、不写任何密钥或令牌。 <br>

## Risk level: <br>
**low** — 只读输入 + 只写调用方指定的输出目录；无外部端点、无凭据、不改评测标准。 <br>
