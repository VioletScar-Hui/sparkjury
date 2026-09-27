---
name: sparkjury-arbitrate
description: Resolve judge disagreements into one final per-dimension decision for each trace. Three-family panel agreement is taken as-is; disagreements go to the cloud Jev decision model, and when Jev is unreachable a local judge arbitrates with the decision marked degraded, followed by a deterministic 5% audit sample re-scored by an independent judge. Use when three judges disagree on a trace, when you need to know how a contested score was resolved and whether it was degraded, or before clustering and reporting so that downstream stages read one decision per dimension. Prefer this skill over sparkjury-score when the question is specifically about HOW a disagreement was resolved (which channel: Jev / local / degraded), arbitration rules, or the audit sample - sparkjury-score covers the full scoring run that includes arbitration as one stage.
license: MIT
compatibility: Requires Python 3.12+ and the sparkjury package (uv sync in the SparkJury repo); Jev needs TYPESAFE_API_KEY, without it every disagreement is resolved locally and flagged degraded
metadata:
  author: "万凌"
  version: "0.1.0"
  product: sparkjury
  command: "sparkjury arbitrate"
allowed-tools: Read Bash Write
---

# sparkjury-arbitrate

## When to use
- After `sparkjury-score`, to turn three verdicts per dimension into one decision per dimension.
- Whenever someone asks "the judges disagreed on this trace — what is the final score, and can I trust it?"
- Before `sparkjury-cluster` and `sparkjury-report`, which read decisions, not verdicts.

## Steps
1. Make sure the store has panel results: `sparkjury score --db <store.db> --judges mock|<panel.toml>`
   (no panel results → `arbitrate` exits 1 with `no panel results in store; run sparkjury score first`).
2. Resolve disagreements:
   `sparkjury arbitrate --db <store.db> [--judges mock|<panel.toml>] [--jev auto|off] [--jev-timeout-s 5.0] [--audit-rate 0.05] [--trace <trace_id>] [--json]`
   - `--jev auto` (default) uses `TYPESAFE_API_KEY` if set; `off` skips the cloud entirely.
   - `--judges` selects the panel whose Judge A doubles as the local fallback and whose last judge is the auditor.
3. Read the source per dimension. `--json` returns `{summary, decisions}`; the table prints
   `trace_id | dimension | panel | final | source | degraded | audit`.
4. Check the summary line: `decided N traces x M dimensions; sources: panel=..., jev=..., local=...; degraded=K; audited=...`

One call: `python scripts/run.py --db <store.db> --jev off`

## Decision paths
Agreement is decided by `decide_agreement()` in `src/sparkjury/judges/panel.py`:

| dimension | agreed when |
|---|---|
| `outcome` | all judges returned ok **and** their pass/fail labels are unanimous |
| `tool_use` / `efficiency` / `safety` | all judges returned ok **and** score spread (max - min) <= 1 |

`source` then takes one of four values (`DecisionSource`, `src/sparkjury/models/arbitration.py`):

| source | when | degraded | final score |
|---|---|---|---|
| `panel` | agreed | no | panel median (majority label for outcome) |
| `jev` | disagreement, Jev answered | no | Jev's score, reconciled with the label |
| `local` | disagreement, Jev failed, local judge ok | **yes** | local judge's score |
| `panel_fallback` | disagreement, Jev failed, local judge failed | **yes** | panel median |

An errored judge always forces disagreement: `len(ok) == len(vs)` is part of the agreement test.

## Output
- `arbitration` table; `arbitrate --json` returns `{summary, decisions}` where each decision carries
  `final_score`, `final_label`, `source`, `degraded`, `panel_scores`, `panel_labels`,
  `jev_confidence`, `jev_raw_score`, `rationale`, `latency_ms`, `error`, and the audit fields.
- `summary.by_source` and `summary.n_degraded` are the two numbers to read first.

## Edge cases

**Jev unreachable.** `JevClient.configured` is false without `TYPESAFE_API_KEY`, or `JevClient.ask()`
raises `JevError` (transport error / HTTP >= 400 / non-JSON / no `answers` key). Either way the
disagreement goes to the local judge (Judge A, `panel.judges[0]`): `source=local`, `degraded=true`,
`rationale` prefixed `[degraded: <reason>]`. The `--jev auto` + unset key case yields
`jev not configured`.

**本地裁判也失败.** If the local judge returns `ok=false`, the code appends
`; local judge error: <e>` and falls to `panel_fallback`: `source=panel_fallback`, `degraded=true`,
`error` carrying the combined reason, `rationale` `[degraded: <reason>] panel median used`.

**可用裁判少于 2.** `decide_agreement()` returns `agreed=false` with reason `only N usable verdict(s)`.
Two votes contain no majority, so it is routed as a disagreement rather than guessed.

**某裁判失败但其余一致.** Still disagreement (`len(ok) == len(vs)` fails), so a single backend
outage costs one Jev or local call rather than silently becoming a two-judge panel.

**outcome 维走两条问题.** The Jev path sends a `score` question *and* a `noul` question
("Did the agent achieve what the user asked for, as shown by the final state?").
`label = "pass"` when the noul probability >= 0.5, then the score is forced to be consistent
(`pass` -> index >= 3, `fail` -> index <= 2). Non-outcome dimensions send only the score question.

**Jev 分数越界或 1-based.** `JevClient.parse_score()` normalises using the `legend` keys and clamps
the index to `[0, n_levels-1]`. `jev_raw_score` keeps the unclamped position so the clamp is visible.

**5% 抽不到样本.** The audit sample is `sha1("audit|<trace_id>") % 10000 / 10000 < audit_rate`,
so on small runs `n_audited` is often 0. That is a no-op, **not** "audit passed". With a
single-judge panel (`--judges` giving one judge) there is no auditor at all and no audit runs.

**票型 3:0 / 2:1 / 1:1:1.** `thresholds.yaml:arbitration` names `unanimous`, `majority_with_golden`,
`majority_without_golden` and `split`. **sparkjury 的实现与这四条的对应关系见
`references/arbitration-rules.md` §2——不是字面照搬**：一致判据是 spread <= 1 而非 3:0，
且完全没有 gold 路由。读产物时按 `references/arbitration-rules.md` 的映射表解释，不要按
pack 的字符串解释。

## Red lines
1. **降级必须进 manifest。** `standards/scenario-pack/judges.yaml` 红线 2 原文：
   「仲裁方可降级，降级必须进 run manifest 的 degraded_flags」。sparkjury 把降级同时落在
   三处且都可读：`Arbitration.degraded`、`source ∈ {local, panel_fallback}`、run manifest 的
   `degradations[]`。**三者必须一致**；不一致即有裁决被静默降级。字段名差异见
   `references/arbitration-rules.md` §5。
2. **2:1 无 gold 必须升级，不许省 Jev 调用。** 省掉这次调用，`by_source` 的直方图就无法区分
   「2:1 被认真仲裁过」与「2:1 直接采多数」，后续所有仲裁质量统计都失真。
3. **仲裁不改分。** `_arbitrate()` 只选择口径，不存在调分路径。审计发现系统性偏差时只能上报，
   自行改分是越权。
4. **被评 agent 不得与任一裁判同族。** `judges.yaml` 红线 1；同族自评准确率 64% -> 45%。
5. **降级不等于无结论，但必须打折读。** `n_degraded > 0` 时，下游 cluster 与 report 的数字
   都要带上这个脚注。

## References
- 仲裁规则映射与降级口径：`references/arbitration-rules.md`
- Jev 集成、成本与不可解释性：`references/jev-integration.md`
- 架构与数据契约：`../../docs/ARCHITECTURE.md`
- 场景包阈值（FROZEN）：`../../standards/scenario-pack/thresholds.yaml`
