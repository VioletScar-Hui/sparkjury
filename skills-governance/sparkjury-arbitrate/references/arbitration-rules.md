# arbitration-rules — 仲裁规则映射手册（sparkjury 实现口径）

> 上游事实源：`standards/scenario-pack/thresholds.yaml` 的 `arbitration` 段（FROZEN）、
> `standards/scenario-pack/judges.yaml` 的 `arbiter` 段、
> `src/sparkjury/arbiter/arbiter.py`、`src/sparkjury/judges/panel.py`。
> 本文件只做**解释与操作化**：把 pack 的规则字符串变成 sparkjury 实际执行的口径。
> 规则语义不得在此改写；要改规则走 govern → pack v0.2。

## 1. pack 的规则表（原文）

`standards/scenario-pack/thresholds.yaml`：

```yaml
arbitration:
  unanimous: "3:0 → 直接记分"
  majority_with_golden: "2:1 且该维有确定性 gold 源 → gold 裁决"
  majority_without_golden: "2:1 无 gold 源 → 升级 Jev"
  split: "1:1:1 → 升级 Jev"
  jev_fallback: "Jev 不可达 → calibrate 画像中的 lead judge 裁决，degraded 标记 jev_unreachable"
  jev_choice_limit: 255
  sample_audit_rate: 0.05
```

`standards/scenario-pack/judges.yaml` 的 `arbiter` 段：

```yaml
arbiter:
  primary:
    type: jev
    endpoint: "api.typesafe.ai/v1/systemone"   # 云 API
    cost_note: "$0.04/百万输入 token，输出免费"
  fallback:
    type: judge_a
    degraded_flag: "jev_unreachable"
```

以及两条写在 `judges.yaml` 头部的红线：

- **红线 1**：被评 agent 模型不得与任一裁判同族（JudgeBench：同族自评准确率 64%→45%）
- **红线 2**：仲裁方可降级，降级必须进 run manifest 的 `degraded_flags`

## 2. 与 v0.1 架构文档差异（四条，逐条列出）

`eval-agent/skills/arbitrate/references/arbitration-rules.md` 描述的是 v0.1 架构文档的仲裁协议。
**sparkjury 的实现与它有四处实质差异**，照旧文档解释产物会得出错误结论：

| # | v0.1 架构文档写的 | sparkjury 实际实现 | 实操后果 |
|---|---|---|---|
| 1 | `unanimous: 3:0 → 直接记分`，即三票完全相同 | `decide_agreement()`：outcome 维看**标签全一致**；其余维看 **score spread ≤ 1**（`tolerance=1`）且无裁判失败 | **4/4/3 记一致**，中位数取 4。一级的真实分歧被吸收，不触发仲裁。读 `by_source` 时要知道一致里混着 spread=1 的情形 |
| 2 | `majority_with_golden`：2:1 且有 gold 源 → 直接取 gold，省一次 Jev | **完全没有 gold 路由**。`_arbitrate()` 只看 Jev 可用性与本地裁判成败，从不看 `trace.outcome.success` | 2:1 一律升级 Jev（有 gold 也升级）。pack 的「省一次 Jev 调用」优化在 sparkjury 里不存在 |
| 3 | `jev_fallback`：Jev 不可达 → 按 `prompts/arbitrate.md` 三条顺序裁决（`evidence` → `checklist` → `reliability`，取 calibrate 画像最可靠裁判的票），`degraded_flag: jev_unreachable` | **只有一条**：本地裁判（`panel.judges[0]`，即 Judge A）直接打分，`source=local, degraded=true`。没有 evidence/checklist/reliability 三级顺序，**没有读 calibrate 画像**，**不产出 `jev_unreachable` 字串** | 降级通道比 pack 设计的更简单也更弱。`judges.yaml` 的 `fallback.type=judge_a` 与 sparkjury 一致；`degraded_flag` 字串不一致（见 §5） |
| 4 | `judge_repeats: 2`，每裁判每单元跑 2 遍后按裁判归约（不一致取低分 + `repeat_split`），少于 2 个有效裁判分走 `insufficient` | **没有 repeats 归约**。每裁判每维一票；`decide_agreement()` 在 `len(ok) < 2` 时直接 `agreed=false`，reason `only N usable verdict(s)`，然后走正常分歧通道 | 没有 `repeat_split` 标记，没有 `insufficient` channel。裁判抖动不可见 |

另外：pack 的 `sample_audit_rate: 0.05` **已实现**，但判据不同——v0.1 说「仲裁后抽样审计」，
sparkjury 用 `sha1("audit|<trace_id>") % 10000 / 10000 < audit_rate` 做**确定性哈希抽样**，
与遍历顺序无关。审计裁判取 `panel.judges[-1]`。

## 3. sparkjury 的决策流程（真实代码路径）

```
        agreement = panel.agreement[i]  for each dimension
                    │
        agreed? ────┴─────
         │是              │否
         ▼                ▼
   _from_panel()      _arbitrate()
   source=panel       │
   degraded=False     │  self.jev is not None?
                      │      │是                │否
                      │      ▼                  │
                      │  _via_jev()             │
                      │  source=jev             │
                      │  degraded=False         │
                      │      │                  │
                      │  JevError? ─是──────────┤
                      │      │否                │
                      │      ▼                  ▼
                      │  返回 jev 结果      local_judge is not None?
                      │                           │是              │否
                      │                           ▼                │
                      │                     local score()         │
                      │                     source=local          │
                      │                     degraded=True         │
                      │                           │                │
                      │                     v.ok? ─否──────────────┤
                      │                           │是               │
                      │                           ▼                ▼
                      │                     返回 local 结果   _from_panel()
                      │                                        source=panel_fallback
                      │                                        degraded=True
                      │                                        error=<合并原因>
```

红线：**`_arbitrate()` 里不存在「跳过 Jev 直接用本地」的分支。** 只要 `self.jev` 非 None
（即传入了 `JevClient` 且 `configured` 为真），就一定会调 `JevClient.ask()`。
省调用的唯一合法方式是 `--jev off` 全局关闭——那是显式声明的策略，不是单例省略。

## 4. 票型 → 通道映射表（读产物时用这张）

| 观测到的票型（panel_scores / panel_labels） | sparkjury 走的通道 | source | degraded |
|---|---|---|---|
| 3 票同分（outcome: 3 个同标签） | `panel` | `panel` | false |
| 4/4/3 或 3/4/4 等 spread ≤ 1 | `panel`（中位数） | `panel` | false |
| spread ≥ 2，Jev 可用且成功 | `jev` | `jev` | false |
| spread ≥ 2，Jev 未配置/超时/4xx/非 JSON | `local` | `local` | **true** |
| spread ≥ 2，Jev 失败且本地裁判也失败 | `panel_fallback` | `panel_fallback` | **true** |
| 某裁判 failed，其余 2 票相同 | 视为分歧 → 上表第三至五行 | `jev`/`local`/`panel_fallback` | 视通道 |
| 可用裁判 < 2 | 视为分歧（reason `only N usable verdict(s)`） | 视通道 | 视通道 |

pack 的 2:1 / 1:1:1 都落在「spread ≥ 2」里（outcome 维是「标签分裂」）：
**sparkjury 不区分 2:1 与 1:1:1，一律升级**。这正好符合 pack 的 `majority_without_golden`
与 `split` 两条都指向「升级 Jev」，只是**没有按 pack 的 `majority_with_golden` 提前短路**。

## 5. 降级标记：pack 说 `degraded_flags`，sparkjury 落 `degradations[]`

pack 红线 2 与 `contracts/run-manifest.schema.json` 都要求降级进 run manifest 的
`degraded_flags`（字符串数组，文档举例 `jev_unreachable` / `judge_c_api_fallback` /
`cluster_rule_fallback`）。

**sparkjury 的 run manifest 没有 `degraded_flags` 字段。** 实测 `runs/demo-1/manifest.json` 的
降级记录形如：

```json
"degradations": [
  {
    "stage": "ARBITRATE",
    "component": "jev",
    "reason": "TYPESAFE_API_KEY not set",
    "fallback": "local judge arbitration"
  }
]
```

因此验证 pack 红线 2 时，**三处都要读，且必须一致**：

| 检查点 | 字段 | 读法 |
|---|---|---|
| 逐条裁决 | `Arbitration.degraded` + `source` | `n_degraded == by_source['local'] + by_source['panel_fallback']` |
| run manifest | `degradations[]` | 有 `stage == "ARBITRATE"` 且 `component == "jev"` 的条目 |
| 证据卡 | `CardQuality.n_degraded` | 与 `arbitration_summary().n_degraded` 相等 |

**三处不一致 = 有裁决被静默降级**，按红线处置：不信任本轮 cluster 与 report 的数字。

`judges.yaml` 的 `degraded_flag: "jev_unreachable"` 这个字串在 sparkjury 里**不存在**。
要在下游按字符串过滤降级，唯一可靠的键是 `source` 的枚举值与 `degraded` 布尔。

## 6. 5% 抽样审计

- 判据：`int(sha1("audit|<trace_id>"), 16) % 10000 / 10000 < audit_rate`
- `audit_rate <= 0` 直接不抽样；默认 0.05，CLI `--audit-rate` 可调
- 审计裁判 = `panel.judges[-1]`；单裁判 panel 时 `audit_judge = None`，**完全不审计**
- 一致性判据：outcome 维比 `label`；其余维比 `abs(score - final_score) > 1`
- 审计裁判失败 → `audit_disagrees = None`，不是 false
- 小样本下 `n_audited` 常为 0：**这是 no-op，不是「审计通过」**

`Arbiter._audit()` 只写审计字段，**不回改 `final_score`**。审计的用途是让人看见偏差，
不是自动纠错。自动纠错路径在代码里不存在，这是有意的。

## 7. Jev 调用构造（`_via_jev()`）

`state` 文本由 `_state_text()` 拼装：task id、trial、domain、gold outcome（`not available` /
`SUCCESS` / `FAIL`）、被仲裁维度、三位裁判的分与理由（含 `evidence steps`）、
以及 `trace.transcript(width=transcript_width)`（默认 400 字符截断）。

`questions` 恒含 `score`（`q_score`，criteria 为 `rubric_levels(dim)`）；
outcome 维额外含 `pass`（`q_noul`）。**arbitrate 不用 `q_choice`**——choice 是 cluster
打标签时用的，别混。

返回值：`jev_confidence`、`jev_raw_score`（未夹紧位置）、`latency_ms`（`JevClient.last_latency_ms`）。
`parse_score()` 用 `legend` 键的最小值做偏移归一再夹紧，`jev_raw_score` 保留夹紧前的值。

## 8. 未验证清单（不要当成已验证）

- Jev 在本 pack 的 0-4 量表上是否校准：**未验证**（无实测，无本地 key）
- Jev 的延迟、可用性、限流行为：**未验证**
- `$0.04/百万输入 token` 是 pack 记载口径，**非本项目实测账单**
- 降级通道（Judge A 兜底）与 Jev 通道结论的系统性偏差：**未验证**——
  只有 5% 审计能看到，且小样本下常抽不到
- spread ≤ 1 的一致判据在真实裁判上的误一致率：**未验证**
