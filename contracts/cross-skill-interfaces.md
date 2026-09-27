# cross-skill-interfaces.md — 跨 skill 接缝契约与事故表

治理层五个 skill 之间、以及治理层与运行时（`src/sparkjury/`）之间的接缝，
每一条都对应一次真实错配或一次审查发现（#19，2026-09-27）。本文件是这些接缝的**唯一真相源**：
代码注释与 SKILL.md 只准引用这里，不准各自复述——复述就是下一次漂移的起点。
早期草案引用过的 `card-spec.md §8.1` 与 `gate-semantics.md` 从未落盘，内容分别收编进 §B 与 §E。

## §A `target` 命名空间（写严读宽）

decision 事件的 `target` 统一 `命名空间:键`：

| decision_kind | target 形式 | 例 |
|---|---|---|
| `accept_card` | `card:<cluster_id>` | `card:C03` |
| `override_priority` | `priority:<taxonomy_id>` | `priority:F06` |
| `reject_proposal` | `proposal:<id>` | `proposal:add-F13` |
| `approve_pack_diff` / `thaw_pack` | `pack_diff:<id>` / `pack:<pack_id>` | `pack:retail-default` |

- **写入侧强制**：`sparkjury-govern/scripts/_ledger.py` 的 `validate_event` 拒收无 `:` 的裸 id。
  生产端（`src/sparkjury/governance/ledger.py`，#19）按此写。
- **读取侧兼容**：`_group_key()` 只取 `:` 后的部分；v0.1 旧账本的裸 id 原样通过，不回填不改写。
- PM 语义的 `cluster_id` 不再占用 `target`：如需回溯，放可选的 `payload.cluster_id`。

**事故**：需求文案写 `"target":"cluster_id"`、SKILL.md 旧示例写 `CL-F06`、校验器要求带 `:`，
三处各说各话——照抄示例写出的事件自家校验器读不了（#19 审查项 1）。

## §B `override_priority` 权威字段（原 card-spec §8.1 的落点）

| 字段 | 必填 | 消费方 | 含义 |
|---|---|---|---|
| `taxonomy_id` | **是**（`F\d\d`） | prioritize `overrides.py` | 权威匹配键 = 排序表 `by_id` 的键 |
| `to_rank` | **是** | prioritize | 人工名次；只提升不压低 |
| `system_top` | **是** | govern 投影 3 | 改序前系统 top 类 |
| `human_top` | **是** | govern 投影 3 | 改序后人工 top 类 |
| `target` | 是（§A 格式） | 人 / 回溯 | 非匹配键 |
| `cluster_id` | 否 | 人 / 回溯 | PM 语义的被改簇 |

缺 `taxonomy_id` 的事件生产端直接 400（#19），不写一条永远不会生效的记录；
缺 `system_top`/`human_top` 的旧事件计入 `override_audit.unusable`，显式曝光不静默。

## §C override 匹配键优先级

1. `payload.taxonomy_id`（形如 `F\d\d`）——唯一权威键；
2. `payload.target` 且**裸值**形如 `F\d\d`——v0.1 遗物兼容路径，记 `match_key_source=payload.target(legacy)`；
3. 都取不到 → 进 `meta.override_unmatched`（带 reason），stderr 打 debug 级一行，**不许静默丢弃**。

**事故**（v0.1 真实故障）：只用 `target` 匹配，PM 在卡上改排序永远匹配不上、无报错、无 override——
「人改了但系统毫无反应」和「没人改过」在输出里长得一模一样。

## §D 运行时标签与 severity 尺度

- **标签翻译**：pack `taxonomy.yaml` 每类自带 `runtime_label`（v0.2 起），
  消费端 `pipeline.translate_runtime_labels()` 凭它自动翻译（如 `unauthenticated_action→F08`），
  **pack 是权威**；`standards/label-taxonomy-map.yaml` 只是 pack v0.1 时代的兜底表，
  必须逐行对齐当前 pack，pack 有 `runtime_label` 时不得使用。
- **severity 不做数值换算**（PM 拍板 evt-0009）：运行期 `Cluster.severity` 是加权连续值，
  只属于运行时公式（`formula_runtime`，卡片主显）；治理侧序数 severity（1–3）以
  taxonomy 的 `default_severity` 为准，`translate_runtime_labels` 对连续值回落
  `default_severity` 并标注来源即是规范行为。`clusters.json` **不加** `severity_max` 字段。

## §E regress 门禁语义（原 gate-semantics.md 的落点）

三判据，阈值治理来源 = pack `thresholds.yaml` 的 `regress.gates`（v0.2.1 起含 `severe_min`），
实现处的默认值只是 pack 不可达时的镜像，改阈值先改 pack：

1. **同 pack 铁律**：两轮 manifest 的 pack 指纹不同 → 不是回归，是新一轮评测，"涨没涨"不成立；
   任一侧缺指纹（老 run）→ UNKNOWN，如实标注不阻断。
2. **主指标提升 ≥ `delta_min`**（0.02）：用 pass^k 组合估计的最大公共 k；缺数据 → UNKNOWN。
3. **不引入新的高严重簇**：after 独有的簇标签且 severity ≥ `severe_min`（3.0，运行期尺度）→ FAIL。

门禁报告每条标 `source=pack/default/cli`；`new_severe_cluster_blocks: false` 时判据 3 只报告不阻断。
