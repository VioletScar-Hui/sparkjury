# jev-integration — Jev 集成手册（sparkjury 实现口径）

> 上游事实源：`src/sparkjury/arbiter/jev.py`、`src/sparkjury/arbiter/arbiter.py`、
> `standards/scenario-pack/judges.yaml` 的 `arbiter` 段。

Jev = TypeSafe 出的**决策模型**，不是 LLM。它不写散文、不解释、不重评轨迹，
只在给定的受限选项里给一个位置值。`src/sparkjury/arbiter/jev.py` 的模块 docstring 写得很直接：

> Jev does not generate text and cannot be self-hosted; it is only used to break ties.

这个定位决定了它在仲裁链路里的用法，也决定了它的全部风险。

## 1. 接口形态

```python
JevClient(api_key=None, *, base_url="https://api.typesafe.ai",
          model="jev-latest", timeout_s=5.0, transport=None)

client.configured        # -> bool(self.api_key)，即 bool(os.environ["TYPESAFE_API_KEY"])
client.ask(state, questions) -> dict   # {"<question_id>": {...answer...}, ...}
client.last_latency_ms   # 本机实测耗时，httpx 层的 finally 里赋值
client.last_usage        # 服务端返回的 usage（未解析）
```

请求：

```
POST https://api.typesafe.ai/v1/systemone
Authorization: Bearer <TYPESAFE_API_KEY>
{"model": "jev-latest", "state": "<text>", "questions": {...}}
```

**硬事实**：默认 `timeout_s = 5.0`，CLI `--jev-timeout-s` 可调。
`base_url` 无 CLI 参数，只能改代码或构造参数。
`api_key` **只从环境变量读**，没有 CLI 传参、没有配置文件项。

## 2. 三种问题形态与 sparkjury 的用法

`JevClient` 提供三个构造函数：

| 构造函数 | payload | 返回 | sparkjury 用在哪 |
|---|---|---|---|
| `q_score(instructions, levels)` | `{"type":"score","instructions":...,"criteria":[level0,...]}` | `{"score": float, "legend": {...}, "probabilities": {...}, "confidence": float}` | **`_via_jev()` 每个分歧维都发**，criteria = `rubric_levels(dim)` |
| `q_noul(instructions)` | `{"type":"noul","instructions":...}` | `{"noul": float}` | **仅 outcome 维**追加一问「Did the agent achieve what the user asked for, as shown by the final state?」 |
| `q_choice(instructions, criteria)` | `{"type":"choice","instructions":...,"criteria":{label: desc}}` | `{"choice": label, "probabilities": {...}, "confidence": float}` | **arbitrate 不用**；`choice` 由 `cluster` 阶段给簇打标签用 |

三个解析器：

- `parse_score(answer, n_levels) -> (pos, idx, confidence)`：用 `legend` 键的最小值做偏移归一，
  `idx` 夹紧到 `[0, n_levels-1]`
- `parse_noul(answer) -> float`
- `parse_choice(answer) -> (choice, confidence)`

### outcome 维的双问题与强制一致

```python
label = "pass" if p >= 0.5 else "fail"
if label == "pass" and idx < 3: idx = 3
if label == "fail" and idx > 2: idx = 2
```

**noul 概率是主，score 被它掰过来。** 所以 outcome 维的 `final_score` 不是独立读数，
是标签的推论。读卡时不要把它当成「agent 在 outcome 上拿了 N 分」，
要读成「这轮判 pass/fail，分数是配套给出的」。

## 3. 成本

口径来自 `judges.yaml.arbiter.primary.cost_note`：**$0.04 / 百万输入 token，输出免费**。

**这是 pack 记载的口径，不是本项目实测账单。** sparkjury 不逐次记录 `tokens_in`，
也不累计 `jev_cost`——`JevClient.last_usage` 取了服务端 usage 但**没有任何代码消费它**。
所以「这一轮在 Jev 上花了多少」目前**无法从产物回答**。

调用规模：只对分歧维调用（`unanimous`/一致维零调用），outcome 维一次调用带两个问题。
一次运行的调用数 ≈ 分歧维数。CLI 末行只报 `jev: configured / not configured / off`，
不报调用次数与费用。

> **TODO(pack v0.2)**：`arbitration.jev_budget` 未定义，无单轮调用上限、无成本熔断。
> 成本失控时只能人工中断。这是已知缺口，不是设计选择。

## 4. 降级：Jev 不可达

`JevError` 的触发条件（`jev.py` 全部 raise 点）：

| 触发 | 错误串开头 |
|---|---|
| `TYPESAFE_API_KEY` 未设置 | `TYPESAFE_API_KEY not set` |
| 传输层失败（连接/超时/TLS） | `transport error: ...` |
| HTTP >= 400 | `HTTP <code>: <body 前 200 字符>` |
| 响应非 JSON | `non-JSON response` |
| 响应无 `answers` 键 | `response has no 'answers'` |
| `score` 答案缺 `score` 字段 | `score answer missing 'score'` |
| `noul` 答案缺 `noul` 字段 | `noul answer missing 'noul'` |
| `choice` 答案缺 `choice` 字段 | `choice answer missing 'choice'` |

**注意：`configured == false` 是另一条路，不产生 `JevError`。**
`Arbiter.__init__` 在 `jev is None or not jev.configured` 时把 `self.jev` 置 None，
`jev_err` 取 `jev_unavailable_reason`，即字串 **`jev not configured`**。
所以「没配 key」与「配了 key 但调不通」在 `rationale` 前缀里是可区分的。

降级后的动作（`_arbitrate()`）：

1. 本地裁判（`panel.judges[0]`，Judge A）对该维打分
2. 成功 → `source=local`、`degraded=true`、`rationale = "[degraded: <err>] local <name>: <rationale>"`
3. 失败 → `source=panel_fallback`、`degraded=true`、
   `error = "<err>; local judge error: <e>"`、`rationale = "[degraded: <err>; local judge error: <e>] panel median used"`

**降级不是静默 fallback。** pack 红线 2 要求它进 run manifest，sparkjury 落在
`degradations[]`（不是 `degraded_flags`，见 `references/arbitration-rules.md` §5）。
不读这个字段，下游就无法知道「本轮有 K 个维度没有经过决策模型仲裁」。

### 与 v0.1 架构文档差异

`eval-agent/skills/arbitrate/references/jev-integration.md` 设计了三级降级通道
（`evidence` → `checklist` → `reliability`）、`evidence_backfilled_by` 补写机制、
`needs_human` 转人工、以及 `jev_cost` / `request_id` 调用记录最小集。
**这四样 sparkjury 全都没有**：

| v0.1 设计 | sparkjury |
|---|---|
| 三级降级通道（取 calibrate 画像最可靠裁判） | 单级：Judge A 直接打分 |
| `lead_judge` 补写 span 证据，补写不出则 `needs_human` 且不落分 | 无补写、无 `needs_human`、**照落分** |
| `jev_cost` + `request_id` 逐次留痕 | `last_usage` 取了但无人消费 |
| `degraded_flags += ["jev_unreachable"]` | `degradations[]` + `Arbitration.degraded` |

最后一条的后果最实际：**sparkjury 的降级路径不产出任何 span 证据**，
Jev 不可达时的那一维，证据只有本地裁判的一句 rationale。
这是降级轮次证据密度天然更低的原因，读卡时要意识到。

## 5. 不可解释性

Jev 给结论不给理由。用在仲裁上的后果很具体：

- 拿到 `score = 2`，没人能回答「为什么不是 3」。
- 下一轮同样的分歧，Jev 仍可能给 2——但没人知道它是否用了同一套隐含标准。
- PM 看到证据卡上写「仲裁结论：2 分」，问「凭什么」，答案是「一个不解释的模型选的」，
  这张卡就废了。

`Arbitration.rationale` 在 jev 通道的原文形如
`Jev jev-latest: position 1.80 on 4 levels, confidence 0.92`——
它记录的是**位置与置信度，不是理由**。

v0.1 设计了「Jev 只选择，证据由 lead judge 补写」来缓解「结论不可交代」。
**sparkjury 没有这个机制**（见 §4 差异表）。sparkjury 侧唯一可用的可解释性来源是
`panel_scores` 与 `panel_labels`——人可以看到三位裁判各自给了什么，自己判断。
这是有限但真实的退路：**仲裁结论不可解释，但争议本身完全可见。**

剩下「Jev 的隐含标准是否漂移」这个问题，只能靠长期观测 `by_source` 直方图
与 5% 审计的 `n_audit_disagreements` 逼近。**这两项在 pack 里都没有阈值**
（未定义 `audit.bias_threshold`），未定义阈值时只能记数和上报，
不能下「Jev 可靠 / 不可靠」的结论。

## 6. 未验证清单

- Jev 在本 pack 的 0-4 量表上是否校准：**未验证**
- Jev 的延迟、可用性、限流行为：**未验证**
- `$0.04/百万输入 token`：**pack 口径，非实测**
- `parse_score()` 的 `legend` 偏移修正是否覆盖了 Jev 的全部返回形态：**未验证**
- 本机环境（代理 + 无 key）下 Jev 的任何实际调用：**未验证**
