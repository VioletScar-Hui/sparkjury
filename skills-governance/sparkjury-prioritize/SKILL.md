---
name: sparkjury-prioritize
description: SparkJury failure prioritization (sparkjury-prioritize)：把 cluster 产出的失败类排成"先修哪一类"的修复顺序，用 frequency_norm × severity_weight × fixability_boost 三项相乘（系数全部读 standards/scenario-pack thresholds.prioritize，禁止自造权重），并且证据卡只推一个 top_recommendation。当用户说"先修哪个""优先级怎么排""哪一类最该先改""排个序""修复顺序""top 建议""override 排序"，或输入是 clusters.json 需要给出行动顺序时使用。消费 PM 在 ledger 里的 override_priority 人工修正作为长期记忆。只排顺序与给理由，不承诺修复成本与收益的绝对值，不定位根因。
license: MIT
compatibility: Python 3.9+ 标准库，零第三方依赖（YAML 由 sparkjury 根 tools/_yaml_lite.py 唯一实现解析，PyYAML 优先、子集兜底）；无独立容器、不需要 GPU、不需要任何模型调用 —— 排序是确定性计算，可被独立复算与审计；standards/scenario-pack 为 FROZEN 只读态，本 skill 只读不改。
metadata:
  author: "万凌"
  version: "0.1.0"
  product: sparkjury
  tags: "evaluation, agent-trace, prioritization, triage, evidence-card"
allowed-tools: Read Bash Write
---

# sparkjury-prioritize — 把失败类排成"先修哪一个"

评测流水线第八环，紧接 `cluster`。`cluster` 回答"失败有几种"，
本 skill 回答**"先修哪一种"**——并且只推一个。

这是整个 demo 的高潮，也是千富公司 PM 痛点的正面回答：他们不缺失败列表，
缺的是**一个能说清"为什么先修这个"的顺序**。排序结果直接进 `report` 的证据卡。

**sparkjury 移植说明（2026-09-27）**：本 skill 由 eval-agent 的 `prioritize` 迁入 sparkjury，
改名 `sparkjury-prioritize`，运行时无对应 CLI（净新增，属治理层桥接 skill）。
路径适配：YAML/hash 等根工具从 `<repo>/scripts/` 改为 `<repo>/tools/`（`pack_freeze.py` 的
`frozen_hash` 仍只 import 那一份权威实现）；scenario pack 从根 `scenario-pack/` 改为
`standards/scenario-pack/`。入口：`python3 scripts/run.py --selftest`
（薄包装，内部 exec `scripts/prioritize.py`）。
与 sparkjury 运行时的语义差异（taxonomy `F01-F99` + severity/fixability 系数 vs 运行时
`FailureLabel` 枚举）见 `references/sparkjury-port-notes.md` —— **只记录差异，不做统一**。

## 何时用我

- 输入是 `clusters.json`，需要给出行动顺序
- 用户问"先修哪个 / 优先级怎么排 / 哪类最该先改 / 排个序 / top 建议"
- PM 说"给我一个能拿去排期的顺序"
- PM 上一次**改过**排序（ledger 里有 `override_priority`），这一轮要按人工修正重排

**不用于**：定位根因（cluster 已划边界）、估算具体工时、做技术方案选型、
比较两轮回归（那是 `regress`）。

> **v0.1 生产者缺位声明**：本 skill 期望的 `clusters[]`（F 编号标签 + `severity_max` 序数 +
> `trace_ids`）与 sparkjury 运行时产物（`FailureLabel` 文本标签 + `severity` 浮点 +
> `member_trace_ids`）形状不同。直接喂 `sparkjury cluster` 产物会**全量拒排**（fail-safe，
> 每条带 reason，见 BENCHMARK 真实产物三步）；映射表见 BENCHMARK，正式对齐走 pack v0.2（D3）。

## 输入

| 输入 | 来源 | 说明 |
|---|---|---|
| `clusters.json` | 上游 cluster | `{clusters: [...], unclassified: [...]}`；只用 `clusters` |
| `cluster_report.json` | 上游 cluster | **取 `total_badcases` 作为分母**（同目录自动发现） |
| `standards/scenario-pack/thresholds.yaml` | pack（FROZEN） | `prioritize` 段：公式、`severity_weights`、`fixability_boost`、`top_recommendation_count` |
| 事件账本 | 可选 | `decision_kind=override_priority` 的历史人工修正 |
| `--pack <dir>` | 可选 | 含 `pack.manifest.json` 的 pack 目录；给了就从 manifest 读 `pack_id` / `version` / `state` 写进 run-manifest，并校验 FROZEN 的 `frozen_hash` |

## run-manifest 的 pack 段怎么来（v0.2 起）

v0.1 这里把 pack 段写死成 `{pack_id: "retail-default", state_at_run: "FROZEN"}`，
于是在 DRAFT pack 上跑也标 FROZEN，回归对比的"同 pack hash 铁律"被一个常量骗过。
现在只有两条来路：

| 情形 | `pack` 段 | degraded flag |
|---|---|---|
| 给了 `--pack` | `pack_id` / `version` / `state_at_run` 全部读自 manifest，`pack_bound: true`，`pack_source: manifest:<path>` | `pack_not_frozen`（state≠FROZEN）或 `pack_frozen_hash_missing`（FROZEN 但无 frozen_hash） |
| 没给 `--pack` | 显式传参原样记录，`state_at_run: "UNKNOWN"`，`pack_bound: false`，`pack_source: cli_args` | `pack_manifest_unbound` |

`--pack` 指向的目录缺 `pack.manifest.json`、或 manifest 声明 FROZEN 但 `frozen_hash`
与现场内容不符 → **显式失败（退出码 3）**，不带着一个对不上的 hash 往下跑。
`frozen_hash` 算法只 import 项目根 `tools/pack_freeze.py` 的 `compute_hash`，
本 skill 不自造等价副本（与 `evalset/scripts/_contracts.py` 同一模式）。

## 公式（pack 原文，禁止自造）

```
priority = frequency_norm × severity_weight × fixability_boost
```

| 因子 | 定义 | 来源 |
|---|---|---|
| `frequency_norm` | 类内 badcase 数 / 总 badcase 数 | 由输入计算，分母取 `cluster_report.total_badcases` |
| `severity_weight` | `severity_weights[str(severity_max)]` | pack：`{3: 1.0, 2: 0.6, 1: 0.3}` |
| `fixability_boost` | `fixability_boost[category_id]` | pack：F01-F12 逐类人工先验 |

**三个系数一个都不得在本 skill 里自造。** 缺系数 → 该类进 `rejected_candidates`，
附 `reason` 说明缺哪个。**不补默认值、不夹逼、不猜。**
理由见 `references/priority-formula.md`：一个静默补上的默认值，
会让"先修哪一类"的结论看起来和正常算出来的一样可信。

### 为什么分母必须是 total_badcases

cluster 的 `share` 分母含 F99（`total_badcases`），而 `clusters` 数组里**不含** F99 簇。
若本 skill 退化成"只累加 clusters 的 size"，同一类在两个 skill 里会打印出两个不同的百分比
——这会被人当成 bug 报上来。因此分母优先级：

```
显式 --total-badcases  >  同目录 cluster_report.json 的 total_badcases  >  累加 clusters.size
```

最后一次兜底只在前两者都拿不到时用，并在输出的 `total_badcases_source` 记明来源。

## Steps

0. **校验输入**。clusters 不可解析 / pack 缺 `prioritize` 段 → fail 事件，退出非零。
1. **定分母**。按上面的优先级取 `total_badcases`，记 `total_badcases_source`。
2. **逐类算分**。`compute_priority(cluster, thresholds, total)`；
   缺系数的类不猜，进 `rejected_candidates`。
3. **排序**。`priority` 降序；`category_id` 升序兜底 → 保证幂等。
   赋 `rank = 1..N`。
4. **黄金池 hook**。读 ledger 里 `override_priority` 事件，`apply_overrides()` 提升被人工修正过的类，
   在 `rationale` 追一句"按历史人工决策修正（ledger event …，rank A→B，actor …）"，
   并重排 rank。
5. **取 top**。只取 `top_recommendation_count` 个（pack = **1**）。
6. **产出**。每条含 `rank / category_id / priority / frequency / severity / fixability / rationale`。
7. **落账**。ledger `started` / `checkpoint` / `completed`；写 `priority.json` + `run-manifest.json`。

## 黄金池 hook：人的修正如何进入下一轮

PM 若不认可排序，会在证据卡上直接改。那次拍板以 `decision` 事件进 ledger
（`decision_kind=override_priority`，契约见 `contracts/event-ledger.schema.json`）：

```json
{"event_type": "decision",
 "payload": {"decision_kind": "override_priority", "actor": "pm-qianfu",
             "target": "CL-F06", "taxonomy_id": "F06", "to_rank": 1,
             "system_top": "F02", "human_top": "F06",
             "rationale": "冗余空转把 P99 延迟打到 8s，投诉最直接"}}
```

**匹配键是 `taxonomy_id`，不是 `target`。** 这是 v0.2 修掉的一处跨 skill 错配：
report 侧规定 `target = cluster_id`（PM 语义，形如 `C03`），而本 skill 的排序表
`by_id` 的键是 `category_id`（taxonomy 标签，形如 `F06`）。v0.1 直接用
`target in by_id` 匹配，于是 PM 在卡上改排序**永远匹配不上、不报错、也不生效**。
现在 `target` 保留 cluster_id 不动，另立 `taxonomy_id` 作权威键。

各字段分工：

| 字段 | 谁写 | 谁消费 | 含义 |
|---|---|---|---|
| `target` | report | 人 / 回溯 | PM 语义上的被改对象 = `cluster_id` |
| `taxonomy_id` | report | **prioritize** | 权威匹配键 = `category_id`，排序表 `by_id` 的键 |
| `to_rank` | report | prioritize | 人工给出的名次；缺省视为只标记不改序 |
| `system_top` | report | govern 投影 3 | 改排序前系统的 top 类（`fixability_boost` 校准对象） |
| `human_top` | report | govern 投影 3 | 改排序后人工的 top 类 |

下一轮本 skill **必须**消费这个信号：

- 把 `taxonomy_id` 指定的类提升到人工曾给出的名次（`to_rank`），其余顺延
- **只提升、不压低**：若该类本轮排名已经 ≤ 人工名次，说明人跟公式结论一致，不动
- `rationale` 必须注明修正来源（`event_id` + `from_rank→to_rank` + `actor`）
- `override_applied` 数组逐条记录用了哪个人工决策
- **匹配不上的事件不许静默丢弃**：缺 `taxonomy_id`、类不在本轮、只有标记没有名次，
  一律进 `priority.json` 的 `meta.override_unmatched`（带 reason 与 debug 级 detail）
  并在 stderr 打一行。否则「PM 改了排序但下一轮毫无反应」看起来跟「没人改过」一模一样。
- v0.1 兼容：`target` 直接写 `F\d\d`（旧示例即如此）仍按 `target` 匹配，
  记 `match_key_source=payload.target(legacy)`。`cluster_id` 形如 `C03`，形状不同不会误判。

这是长期记忆的雏形：**公式是默认判断，人是校准源**。人对得越多，
这个 hook 就越接近"学到的偏好"；目前 v0.1 只做"重排 + 标注"，不做权重更新
（更新权重需要足够多的决策样本，且要经 `govern` 走 pack 变更，不是 skill 自己能改的）。

**幂等**：同一个 override 事件重复应用不改变结果（提升后 rank ≤ to_rank，第二次不触发）。
selftest 第 6 组用输出 sha256 验证这一点，第 8 组验权威键与不静默。


## 呈现层温度（--temperature，v0.1 新增）

产品定义（万凌，2026-09-27）：像大模型的 temperature 一样用一个滑杆决定输出浓度——
拉低只看 P0 级重点修，拉高粒度从粗到细、出卡与待拍板内容变多。

| 档位 | 区间 | 浮出门槛 | 拍板队列 |
|---|---|---|---|
| p0 | t<0.35 | severity=3 且占比≥10% | 上限 1 |
| standard | 0.35–0.70 | severity≥2 | 上限 3 |
| full | t≥0.70 | 全部（含低严重度） | 不设限 |

三条红线：①抑制≠删除（suppressed 逐条带 reason）；②人工 override 钉住温度（人的决策
不被滑杆压掉）；③不传温度=行为与旧版逐字节一致。全部有 selftest 断言（第 9 组）。

诚实边界：这是**呈现层**温度——badcase 判定与聚类已发生，温度只管"多少浮上卡片"。
**判定层**温度（badcase 阈值/聚类粒度随温度变）需运行时配合，见接口需求文档；档位
映射为技能层常量，TODO pack v0.2 收编 `thresholds.prioritize.focus`。

## Edge cases

| 条件 | 行为 |
|---|---|
| `severity_max` 不在 `severity_weights` 表内 | 该类进 `rejected_candidates`，写"未在 pack 定义" |
| `category_id` 不在 `fixability_boost` 内（F99 的真实情形） | 进 `rejected_candidates`，写"缺系数不可排序" |
| 分母 ≤ 0 或不可用 | 该类进 `rejected_candidates` |
| F99 混进 `clusters`（上游若这么给） | 同上，拒排 —— pack 未定义它的系数，不许猜 |
| `top_recommendation_count` 非法 | 立即 fail。这是 pack 完整性问题，不是可降级项 |

**本 skill 自身不需要模型调用，因此没有降级通道。** 它是确定性计算：
给定 clusters + pack + override 历史，输出唯一。这也意味着它可以被任何人独立复算。

## 红线（违者即 fail）

- **禁止一次给多个"先修"**。`top_recommendation` 只给 1 个。
  人的注意力是稀缺资源，卡片推三个等于没推。
- **禁止把可修复性系数拍死**。必须引用 pack；且 v0.1 的人工设定必须在输出注明
  "人工先验，未经黄金池校准"（`fixability_source: human_prior_pack_v0.1_uncalibrated`）。
- **禁止自造权重**。缺系数 → `rejected_candidates`，不补默认值。
- **排序依据必须逐条可解释**。每条 `rationale` 要能还原成三个数的乘法，
  不接受"综合评估"这类无法复算的说法。
- **不得改写 scenario pack**（sparkjury 内为 `standards/scenario-pack/`）。
- **不得自动生成 checker**（全库红线）；不得让被评 agent 参与排序判断（自己不打分自己改）。

## 停止条件

- **所有类都被拒排** → pack 与这批 clusters 不匹配，走 `govern` 补 pack，不要硬排
- F99 占比 > 50%（来自 cluster_report）→ 分类体系不适配，回到 `cluster` 的停止条件
- `top_recommendation` 与 PM 上一次 override 的结论连续冲突 → 说明公式缺一个 PM 在意
  的维度。**这时该考虑扩公式，不是反复覆盖人工决策**
- 输入 clusters 为空 → 上游有问题，不要输出一个"无需修复"的结论

## 脚本与文件结构

`scripts/run.py` 是 sparkjury 规范薄包装（exec 下列主脚本，参数透传）。
`scripts/prioritize.py` 是 CLI 入口（`--clusters/--thresholds/--overrides/--pack/--ledger/--selftest`，
退出码 0 成功 / 2 缺参 / 3 输入或 pack 不可用），并 re-export 全部公开函数与常量，
`import prioritize as impl` 的老调用点（`_selftest.py` 即如此）无需改动。

单文件曾达 623 行（超出 `.claude/rules/coding.md` 的 600 行上限），按职责拆为四个模块：

| 文件 | 职责 |
|---|---|
| `scripts/prioritize.py` | CLI 入口 + 公开 API re-export |
| `scripts/pack_binding.py` | pack 身份绑定：`--pack` manifest 读取、`FROZEN` 的 `frozen_hash` 校验、`pack_bound` 自证字段 |
| `scripts/ranking.py` | 优先级公式 `compute_priority` + 排序 `rank`（含唯一 `top_recommendation`、拒排理由） |
| `scripts/overrides.py` | 黄金池 hook：override 事件解析、`taxonomy_id` 权威匹配键、`apply_overrides`（不静默丢弃） |
| `scripts/pipeline.py` | `run()` 编排 + IO 助手（`sha256_obj` / `write_json` / `emit` / 分母解析） |
| `scripts/_selftest.py` / `scripts/_fixtures.py` | `--selftest` 的断言与内嵌 fixture |

## TODO（待 pack v0.2）

- `fixability_boost` 当前 12 个类的人工先验，**未经黄金池校准**。校准需要积累足够的
  override 决策样本，并走 govern 提案，不是本 skill 能自己更新的
- severity=3 的一票否决类：v0.1 没有单独硬否决逻辑，severity=3 通过 `severity_weight=1.0`
  体现。若产品要求"severity 3 无条件最优先"，需在 pack 加显式规则 —— TODO
- `fixability_boost=0` 的类如何呈现：当前会算出 `priority=0` 并排在末位。
  pack v0.1 无 0 值类，语义待真实出现后确定（见 `references/priority-formula.md`）

## 移植与差异说明

- `references/sparkjury-port-notes.md` — 2026-09-27 从 eval-agent 迁入 sparkjury 的路径适配清单（哪些硬编码改成了 `tools/` 与 `standards/scenario-pack/`）以及与 sparkjury 运行时的语义差异。**差异只记录，不私自统一。**

