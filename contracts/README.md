# contracts/ — 治理层文档契约（JSON Schema）

治理 Skill（calibrate/prioritize/clarify/govern/arbitrate）的产物与账本所引用的四份契约。
**运行时权威在 `src/sparkjury/models/`（Pydantic）**；本目录是治理层跨 skill 接缝的文档契约，
两者各管一层，冲突时以运行时为准并在下面登记。

| 文件 | 用途 | 消费方 |
|---|---|---|
| `event-ledger.schema.json` | append-only 决策账本（decision 事件 = 黄金池入口） | govern `_ledger.py`、prioritize `overrides.py` |
| `skill-envelope.schema.json` | 治理 skill 产物的统一信封（manifest+payload） | calibrate/prioritize 产物外层 |
| `run-manifest.schema.json` | 运行五元组指纹 | prioritize/cluster 的 run-manifest 段 |
| `trace.schema.json` | trace.v1 统一轨迹（治理层视角） | calibrate 校准集引用 |
| `cross-skill-interfaces.md` | 跨 skill 接缝契约与事故表（target 命名空间 / override 字段与匹配键 / 标签翻译与 severity 尺度 / regress 门禁语义） | 各 SKILL.md 与代码注释统一引用此处 |

## 已知漂移（登记不隐瞒，裁决走 pack v0.2 流程，见 docs/PROPOSAL-skill-governance.md D3）

- `run-manifest.schema.json` 写 `state_at_run: {const: "FROZEN"}` 且 required 含 `started_at`，
  而实现允许 `UNKNOWN/DRAFT/null`、不写 `started_at`——**改掉之前本 schema 不能当门禁用**。
- 运行时 manifest 的降级字段是 `degradations[{stage,component,reason,fallback}]`，
  本目录口径是 `degraded_flags: string[]`，两者并存至 v0.2 统一。
