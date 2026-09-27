# 模块验收记录

每个模块完成后在这里记一条：做了什么、怎么自测、怎么人工验证、验收人。

各节里的「自测结果」是**当时那次运行**的记录（每节都带日期）。模块交付后测试还在往上加，所以那几个数字不是当前值；当前用例数以 `docs/ARCHITECTURE.md` 的模块表为准，那张表由 `scripts/certificate.py` 逐行核对。

---

## M1 数据契约 + 输入适配 + 存储（2026-09-26）

**做了什么**

- `models/trace.py`：统一的 Trace / Step / ToolCall / ToolResult / Outcome / TraceMetrics 数据模型，对齐 OTel GenAI 语义约定。
- `adapters/tau2.py`：τ²-bench（tau3）`Results` JSON → Trace。支持 `messages` 和 `ticks` 两种布局，`MultiToolMessage`，用户侧工具调用，金标 `reward_info.reward >= 1` 判成功。
- `adapters/otel.py`：OTLP/JSON 或扁平 span 列表 → Trace。按 `invoke_agent / chat / execute_tool` 三层 span 重建步骤；识别 `error.type` 与 status 错误；支持 `sparkjury.*` 自定义属性带入 task_id / trial / success。
- `store/sqlite.py`：单文件 SQLite，`traces` 与 `runs` 两张表，幂等 upsert，JSONL 导出，统计（任务数、trial 分布、pass^1、pass^k、平均步数、终止原因分布）。
- `cli.py`：`sparkjury ingest / stats / show / list / export`。
- `scripts/make_samples.py`：生成 12 条 τ²-bench 风格 retail 样本（4 任务 × 3 trial，含未确认即取消、用错工具、工具后端 503、编造信息四种典型坏例）和 2 条 OTel 样本。
- `tests/`：9 个 pytest 用例覆盖适配器、存储、统计、CLI。

**自测结果**：`uv run pytest` 9 passed。

**人工验证步骤**

```bash
cd sparkjury
uv sync
uv run python scripts/make_samples.py
uv run sparkjury ingest --path data/samples/tau2_retail_sample.json --source tau2
uv run sparkjury ingest --path data/samples/otel_sample.json --source otel
uv run sparkjury stats
uv run sparkjury list --failed
uv run sparkjury show retail_task_002-t1
uv run pytest
```

预期：stats 显示 14 条 trace、6 个任务、pass^1 64.3%、pass^3 25.0%；`list --failed` 列出 5 条失败；`show` 打印可读的对话与工具调用流水。

**验收人**：待确认

---

## M2 Precheck 假 badcase 打标（2026-09-26）

**做了什么**

- `models/precheck.py`：PrecheckKind / PrecheckFlag / PrecheckResult 数据模型。
- `precheck/rules.py`：7 条确定性规则，不依赖模型。
  - `empty_trace`：没有步骤或没有 assistant 步骤
  - `infra_error`：termination_reason 是 infrastructure_error / unexpected_error / user_error
  - `timeout`：termination_reason 是 timeout，或单步延迟超过阈值（默认 120 秒），或整体时长超过阈值（默认关闭）
  - `context_overflow`：termination_reason 是 context_window_exceeded
  - `tool_unavailable`：同一工具连续 ≥2 次返回错误，且错误文本像基础设施故障（5xx、unavailable、connection reset、timeout、rate limit）。"order not found" 这类业务错误不算
  - `permission_denied`：错误文本含 permission denied / unauthorized / 403 / access denied；但"user not authenticated"这类是 Agent 没走认证流程，属于 Agent 的错，明确排除
  - `user_sim_broken`：模拟用户消息为空，或同一句话重复 ≥3 次
- 所有阈值在 `PrecheckConfig` 里可调。
- `store/sqlite.py`：新增 `precheck` 表，`put_precheck / get_precheck / list_precheck / scorable_traces / precheck_summary`。`scorable_traces()` 就是后面 M3 judge 的输入。
- `cli.py`：`sparkjury precheck [--step-latency-ms N] [--max-duration-s N] [--json]`。
- `tests/test_m2_precheck.py`：10 个用例，每条规则一个正例加一个反例，另外对全部 14 条样本跑一遍确认只有 1 条被标。

**自测结果**：`uv run pytest` 19 passed（M1 9 个 + M2 10 个）。

**人工验证步骤**

```bash
cd sparkjury
uv run pytest
uv run sparkjury precheck
uv run sparkjury precheck --json
```

预期：`precheck` 显示 14 条里 1 条环境失败、13 条进裁判；表格里只有 retail_task_003-t1，原因是 tool_unavailable，工具 find_user_id_by_email 连续 3 次 503。OTel 样本里那条"permission denied: user not authenticated"没有被标，因为那是 Agent 没认证就去取消订单，属于真 badcase。

**验收人**：待确认

---

## M3 三裁判面板（2026-09-26）

**做了什么**

- `models/verdict.py`：Dimension（outcome / tool_use / efficiency / safety）、Verdict、DimensionAgreement、PanelResult 数据模型。
- `judges/rubrics/*.md`：四个维度各一份 rubric，0 到 4 分的评分标准写死，要求模型只输出一个 JSON 对象。
- `judges/prompts.py`：拼 prompt（rubric + 金标摘要 + 带 [n] 步骤序号的对话流水）；`parse_verdict_json` 容忍代码围栏和废话，校验分数范围和标签。
- `judges/client.py`：
  - `OpenAICompatJudge`：任何 OpenAI 兼容接口（DGX 上的 vLLM、StepFun、OpenRouter）。超时、重试可配；输出不是 JSON 时追问一次；后端挂了返回带 error 的 Verdict，绝不抛异常。
  - `MockJudge`：基于规则的裁判，用于离线测试和断网兜底；可加确定性抖动，让 mock 面板也会出现分歧。
- `judges/heuristics.py`：规则裁判的规则，编码零售客服策略：先认证、先读后写、破坏性操作前要确认、不能编造订单状态、用户反对后不能重复同一写操作。
- `judges/panel.py`：`PanelConfig`（TOML 或内置 mock 三人组）、`Panel.score(trace)` 并发跑 3 judge × 4 维，`decide_agreement` 判定一致性：outcome 看三票 pass/fail 是否相同，分数维度看极差是否 ≤ 1，任一 judge 出错即视为不一致。
- `deploy/judges.example.toml`：三 judge 的真实配置模板（Qwen 本地 8001、Gemma 本地 8002、StepFun API），API key 只从环境变量读。
- `store/sqlite.py`：新增 `verdicts`、`panel` 两张表，`put_panel_results / get_panel_result / list_panel_results / verdict_summary`。
- `cli.py`：`sparkjury score [--judges mock|文件.toml] [--dims ...] [--trace ID] [--limit N] [--json]` 和 `sparkjury verdicts <trace_id>`。
- `tests/test_m3_judges.py`：13 个用例，覆盖 prompt、JSON 解析、四类坏例的规则打分、LLM 裁判的解析与追问与容错（用桩后端）、一致性规则、TOML 配置、存储与 CLI。

**自测结果**：`uv run pytest` 32 passed（M1 9 + M2 10 + M3 13）。

**人工验证步骤**

```bash
cd sparkjury
uv sync
uv run pytest
uv run sparkjury score
uv run sparkjury verdicts retail_task_002-t1
uv run sparkjury verdicts retail_task_001-t2
```

预期：`score` 打印每条 trace 四个维度三位裁判的分数，最后一行显示 scored 13 traces, 156 verdicts，并列出需要仲裁的条数。`verdicts retail_task_002-t1` 能看到 tool_use 和 safety 都很低，理由是"用错工具、用户反对后重复同一写操作"。`verdicts retail_task_001-t2` 的 safety 是 2 分，理由是"取消订单前没有拿到明确确认"，evidence 指向第 6 步。

**接真实模型**：复制 `deploy/judges.example.toml` 为 `deploy/judges.toml`，填好 vLLM 地址，`export STEPFUN_API_KEY=...`（PowerShell 用 `$env:STEPFUN_API_KEY="..."`），然后 `uv run sparkjury score --judges deploy/judges.toml`。

**验收人**：待确认

---

## M4 仲裁与审计（2026-09-26）

**做了什么**

- `models/arbitration.py`：Arbitration（每条 trace 每个维度一条最终裁决）、TraceDecision、DecisionSource（panel / jev / local / panel_fallback）。
- `arbiter/jev.py`：Jev 客户端。接口按 TypeSafe 官方文档：POST `https://api.typesafe.ai/v1/systemone`，Bearer 鉴权，一次请求可带多个问题；三种问题类型 score（有序等级列表）、noul（是否为真的概率）、choice（多选一）。响应里 score 的 legend 可能是 1 起编号，客户端自动归一到 0 起。超时默认 5 秒，任何错误统一抛 JevError。
- `arbiter/arbiter.py`：
  - 三票一致：取中位数（outcome 取多数票），来源 panel。
  - 三票不一致：把三位裁判的分数、理由、证据步骤和对话流水拼成 state 送 Jev。score 问题的五个等级直接取自 rubric 里的 0 到 4 分定义，outcome 额外问一个 noul。
  - Jev 未配置或调用失败：本地 Judge A 仲裁，标 degraded=true，理由里写明降级原因。
  - 本地也失败：退回面板中位数，标 degraded 并记录错误。
  - 5% 审计：按 trace_id 哈希确定性抽样，抽中的 trace 全部维度再由审计裁判打一遍，记录是否与最终裁决相差超过 1 分。
- `judges/prompts.py`：新增 `rubric_levels()`，从四份 rubric 里解析出 0 到 4 分的描述，供 Jev 的 score 问题使用。
- `store/sqlite.py`：新增 `arbitration` 表，`put_decisions / get_decision / list_decisions / arbitration_summary`。
- `cli.py`：`sparkjury arbitrate [--jev auto|off] [--jev-timeout-s 5] [--judges mock|文件.toml] [--audit-rate 0.05] [--trace ID] [--json]`。Jev 的 key 从环境变量 TYPESAFE_API_KEY 读。
- `tests/test_m4_arbiter.py`：10 个用例。用假的 HTTP 传输层验证 Jev 请求体和响应解析（含 1 起编号的 legend）；四条决策路径各一个用例：一致取中位数、分歧送 Jev、Jev 失败退本地并标降级、本地也失败退面板中位数；审计抽样的确定性和比例；存储与 CLI。

**自测结果**：`uv run pytest` 42 passed（M1 9 + M2 10 + M3 13 + M4 10）。

**人工验证步骤**

```bash
cd sparkjury
uv sync
uv run pytest
uv run sparkjury arbitrate
uv run sparkjury arbitrate --json
```

预期：`arbitrate` 表格只列出有分歧或被审计抽中的行，能看到 panel 三个分数、final 最终分、source 为 local、degraded 为 yes。最后一行显示 decided 13 traces x 52 dimensions，以及 jev: not configured。因为本机没有 Jev 的 key，所有分歧都走了本地降级路径，这正是断网演示要展示的效果。

**接真实 Jev**：`export TYPESAFE_API_KEY=你的key`（PowerShell 用 `$env:TYPESAFE_API_KEY="..."`），再运行 `uv run sparkjury arbitrate`，有分歧的维度 source 会变成 jev，degraded 为空。

**验收人**：待确认

---

## M5 badcase 聚类与优先级（2026-09-26）

**做了什么**

- `models/cluster.py`：FailureLabel 失败分类（从 MAST / TRAIL 压缩成 10 类：wrong_tool、wrong_args、missing_lookup、missing_confirmation、unauthenticated_action、hallucinated_info、loop、premature_stop、policy_violation、other）、BadCase、Cluster、ClusterRun。严重度权重：safety 3、outcome 2、其它 1。
- `cluster/badcase.py`：从最终裁决里挑 badcase。规则：outcome 为 fail，或任一维度 ≤ 1 分，或 safety ≤ 2 分（safety 更严，因为 2 分的含义就是"没确认就执行了"）。特征文本 = 失败维度 + 工具名列表 + 证据步骤原文 + 三位裁判和仲裁的理由。
- `cluster/embed.py`：两种向量化。`OpenAIEmbedder` 接 vLLM 的 embeddings 接口（DGX 上跑 Qwen3-Embedding）；`HashingEmbedder` 纯 Python 的词加字符三元组哈希向量，零依赖、确定性，离线和兜底用。
- `cluster/group.py`：HDBSCAN（scikit-learn）为主；样本太少或全是噪声时自动退回确定性的平均链接凝聚聚类。每簇取离中心最近的 2 到 3 条代表。优先级 = 簇大小 × 平均严重度，噪声桶永远排最后。
- `cluster/taxonomy.py`：给每簇贴标签。有 Jev 时用 choice 问题从 10 类里选一个；没有或失败时用关键词规则从裁判理由里投票。每个标签配一条改进建议，措辞是"建议先看这里"，不是"根因是这个"。
- `store/sqlite.py`：新增 `badcases`、`clusters` 表，`put_cluster_run / get_cluster_run / list_badcases`。每次聚类整体替换上一次结果。
- `cli.py`：`sparkjury cluster [--embedder hash|openai] [--embed-base-url] [--embed-model] [--method auto|hdbscan|threshold] [--min-cluster-size 3] [--jev auto|off] [--json]`。embedding 服务连不上时自动退回哈希向量并提示。
- `tests/test_m5_cluster.py`：9 个用例：badcase 阈值、样本库上挑出 5 条 badcase 及其证据、哈希向量确定性、阈值聚类、端到端聚类与规则标签、样本太少时的回退、空输入、Jev 贴标签与失败回退、存储与 CLI 含 embedding 服务不可达的兜底。

**自测结果**：`uv run pytest` 51 passed（M1 9 + M2 10 + M3 13 + M4 10 + M5 9）。

**人工验证步骤**

```bash
cd sparkjury
uv sync
uv run pytest
uv run sparkjury cluster --min-cluster-size 2
uv run sparkjury cluster --min-cluster-size 2 --json
```

预期：从 13 条已裁决记录里挑出 5 条 badcase，聚成 2 簇左右。用错工具的那两条 retail_task_002 会在同一簇，标签是 wrong_tool 或 loop，每簇下面有一行 Suggestion。样本只有 5 条所以要把 `--min-cluster-size` 降到 2，真实跑几百条时用默认 3。

**接真实服务**：DGX 上起 embedding 后加 `--embedder openai --embed-base-url http://127.0.0.1:8003/v1`；有 Jev key 时标签来源会显示 jev。

**验收人**：待确认

---

## M6 证据卡片 + 回归对比（2026-09-26）

**做了什么**

- `models/report.py`：EvidenceCard 及其子结构（总量、质量指标、按优先级排好的簇、每簇代表 trace 的证据摘录、三位裁判意见、裁决来源、一条建议、免责声明）。免责声明写死："聚类和建议只指出先看哪里，不是根因结论"。
- `report/cards.py`：`build_card(store)` 从库里汇总 M1 到 M5 的全部结果；三种渲染 `render_json / render_markdown / render_html`；HTML 是自包含单文件，不依赖外网，可直接发给 PM 或嵌进 Cockpit。
- `models/regress.py` 和 `regress/passk.py`：`compare(before_db, after_db)` 对比两次评测。输出 pass^1 与 pass^k 前后差、哪些任务从 fail 变 pass、哪些从 pass 变 fail、各维度均分变化、badcase 数量变化、每个失败标签的簇大小变化，并给一句结论：improved / improved with regressions / unchanged / regressed。
- `judges/pairwise.py`：成对比较。同一任务同一 trial 的前后两条记录送裁判比，A/B 顺序交换跑两遍，两遍结论一致才算数，不一致记为 inconsistent。这是针对位置偏差的标准做法。`MockPairwiseJudge` 用规则分数比较，`OpenAIPairwiseJudge` 接真实模型。
- `cli.py`：`sparkjury report [--out runs/card] [--format all|json|md|html] [--title]` 和 `sparkjury regress --before A.db --after B.db [--pairwise mock|文件.toml] [--out 报告.md] [--json]`。
- `tests/test_m6_report_regress.py`：8 个用例。卡片的总量和簇内容、三种格式渲染、空库；回归：同库对比为 unchanged、修好一条后为 improved 且列出 retail_task_004、反向对比为 regressed；成对比较的交换一致性，包括一个"永远选 A"的偏见裁判被识别为不一致；CLI。

**自测结果**：`uv run pytest` 59 passed（M1 9 + M2 10 + M3 13 + M4 10 + M5 9 + M6 8）。

**人工验证步骤**

```bash
cd sparkjury
uv run pytest
uv run sparkjury report --title "Retail eval demo"
# open runs/card/card.html in a browser: macOS `open runs/card/card.html`, Windows `start runs\card\card.html`
uv run sparkjury regress --before runs/sparkjury.db --after runs/sparkjury.db --pairwise mock
```

预期：`report` 在 runs/card/ 下写出 card.json、card.md、card.html 三个文件，终端打印一句 recommendation。浏览器打开 card.html 能看到顶部指标块、推荐、免责声明、按优先级排列的簇和每簇的证据摘录与三位裁判意见。`regress` 用同一个库对比自己，结论是 unchanged，所有 delta 为 0，成对比较全部 tie 且一致。

**验收人**：待确认

---

## M7 Harness 编排器（2026-09-26）

**做了什么**

- `harness/config.py`：RunConfig，一份 TOML 描述整次评测：输入文件、要跑哪些阶段、Precheck 阈值、评测集裁剪、裁判面板、仲裁与审计设置、聚类设置、报告设置。`RunConfig.demo()` 内置离线演示配置。模板在 `deploy/run.example.toml`。
- `harness/events.py`：事件总线。每次状态变化发一条事件（run_start / stage_start / stage_end / progress / degraded / warning / error / run_end），带序号和时间戳，同步写进 `runs/<run_id>/events.jsonl`，可订阅。M8 的 SSE 时间线直接消费它。
- `harness/orchestrator.py`：状态机 INGEST → PRECHECK → EVALSET → SCORE → ARBITRATE → CLUSTER → REPORT。
  - 每个阶段记录耗时和产出计数，写进 `runs/<run_id>/manifest.json` 和数据库 runs 表；manifest 还记录配置快照、各角色用了哪个模型、全部降级记录。
  - 三处自动降级：LLM 裁判健康检查不通过则换成 mock 裁判；Jev 没配 key 或调用失败则本地仲裁；embedding 服务连不上则换哈希向量。每次降级都发事件并写进 manifest。
  - 某阶段抛异常：记录错误和堆栈，停止后续阶段，manifest 状态 failed，事件流仍然正常收尾。
  - 支持只跑子集阶段（比如在已有库上只重跑 SCORE → ARBITRATE）和评测集裁剪（限制条数或指定任务）。
- `judges/client.py`：给 LLM 裁判加了 `healthcheck()`，3 秒内探一下 /models 接口。
- `cli.py`：`sparkjury run --demo` 或 `--config 文件.toml`，可选 `--stages`、`--run-id`、`--db`、`--quiet`；`sparkjury runs` 列出历史运行；`sparkjury events <run_id> [--kind degraded]` 看事件流。
- `tests/test_m7_harness.py`：6 个用例：离线演示端到端并核对每个阶段的计数、事件序列和落盘文件；不可达的 LLM 裁判被自动换成 mock 并标降级；输入文件不存在时阶段失败、运行终止、manifest 记录错误；子集阶段加评测集裁剪；TOML 配置解析含 deploy 模板；CLI 三个命令。

**自测结果**：`uv run pytest` 65 passed（M1 9 + M2 10 + M3 13 + M4 10 + M5 9 + M6 8 + M7 6）。

**人工验证步骤**

```bash
cd sparkjury
uv run pytest
uv run sparkjury run --demo --run-id demo-1
uv run sparkjury runs
uv run sparkjury events demo-1 --kind degraded
# open runs/demo-1/card/card.html in a browser: macOS `open ...`, Windows `start ...`
```

预期：`run --demo` 依次打印七个阶段的开始和结束，每条记录打分时有一行 progress，ARBITRATE 阶段有一条黄色 degraded 说明 Jev 没配 key 走了本地仲裁，最后 status: ok 并打印 recommendation。`runs` 列表里有 demo-1。`runs\demo-1\` 下有 manifest.json、events.jsonl、evalset.json、sparkjury.db 和 card 目录。

**验收人**：待确认

---

## M8 API + Agent Cockpit（2026-09-26）

**做了什么**

- `api/runs.py`：RunManager。在后台线程里启动编排器，把事件同时写入内存和分发给订阅者，供 SSE 实时推送；已结束的运行从 `runs/<run_id>/` 回放。
- `api/app.py`：FastAPI 后端。端点：
  - `POST /runs`：启动一次评测（demo 或指定 TOML 配置，可选阶段子集和评测集上限），立即返回 run_id
  - `GET /runs`、`GET /runs/{id}`：运行列表和清单
  - `GET /runs/{id}/events`：SSE 事件流。先回放历史事件，再实时推送直到 run_end；`?since=N` 断线续传；15 秒心跳
  - `GET /runs/{id}/events.json`：同一事件流的普通 JSON 版
  - `GET /runs/{id}/card`、`/card.html`：证据卡片
  - `GET /runs/{id}/clusters`、`/traces?cluster=`、`/traces?failed=true`、`/traces/{trace_id}`：簇、badcase 列表、单条记录含三裁判意见和裁决
  - `POST /runs/{id}/confirm`：PM 点"先修这一类"，落盘并返回下一步的 regress 命令
  - `GET /runs/{id}/regress?before=`：两次运行的回归对比
  - `GET /dgx`：nvidia-smi 采样（显存、利用率、温度）加四个模型端点的可达性探测，2 秒缓存
  - `GET /`：静态 Cockpit 页
- `api/dgx.py`：DGX 资源面板的数据采集。
- `api/static/index.html`：自包含的 Cockpit 兜底页，按备赛指南的三栏布局：左 USER TASK（本轮配置、模型、计数），中 AGENT TIMELINE（七个阶段的状态灯加实时事件流），右 DGX SPARK（GPU 显存条、模型端点红绿灯、本地与云端裁决计数）；底部 FINAL ARTIFACT 是证据卡片，每簇有"PM: fix this first"按钮。顶部可以直接点 Run demo。纯 HTML 加原生 JS，不依赖外网。
- `cli.py`：`sparkjury serve [--host 0.0.0.0] [--port 9000] [--runs-dir runs] [--no-probe]`。DGX 节点上用 9000 端口，对应公网 9030。
- 路径与鉴权：`run_id` 只接受单层目录名（`RunManager.run_dir()` 是所有读写的公共出口），`db` 必须落在 `runs_dir` 之内，`config_path` 必须落在服务进程工作目录之内，越界一律 400；没配 token 时中间件只放行回环来源。机制与锚点见 `docs/ARCHITECTURE.md` 的 M8 一节。
- `tests/test_m8_api.py`：11 个用例：通过 API 启动 demo 运行并读遍全部端点（SSE 回放、卡片、簇、记录、确认、自比回归）；第二次运行加评测集上限并做跨运行回归；错误路径；DGX 探测对不可达端点和缺 nvidia-smi 的处理；以及请求里的路径出不了 `runs_dir`（非法 `run_id`、越界 `db`、越界 `config_path` 一律 400，`resolve_within` 连 symlink 一起展开后再比）、没 token 时非回环来源 403。

**自测结果**：`uv run pytest` 69 passed（M1 到 M7 共 65 + M8 4）。另外实际起了服务做了冒烟：/health、首页、启动 demo、SSE 回放都正常。

**人工验证步骤**

```bash
cd sparkjury
uv sync
uv run pytest
uv run sparkjury serve --host 127.0.0.1 --port 9000
```

然后浏览器打开 http://127.0.0.1:9000/ ，点右上角 Run demo。预期：中间时间线里七个阶段依次亮起，事件逐行滚动，ARBITRATE 阶段出现一条黄色 degraded；右栏显示本机 GPU 显存和四个端点的红点（本机没起模型所以是红的）；底部出现证据卡片，点某一簇的"PM: fix this first"按钮后该簇变绿并显示下一步命令。Ctrl+C 停止服务。

**给剑乔**：前端可以直接对着这些端点开发，`/runs/{id}/events` 用 EventSource 接就行；静态页只是兜底。

**验收人**：待确认

### M8 补充：检查台三块视图与中文化改版（2026-09-27，剑乔）

`docs/TEAM.md` 第 5 轮给看板定的产出口是「把视频里需要切终端才能看的环节搬进看板」。这一版在证据卡片下面加了「检查台」，三个页签，全部只调用 M8 已有端点，后端和契约没动。同一次改版把整页视觉换成 `docs/AGENT_VS_WORKFLOW.html` 的语言（象牙白纸面、炭黑侧栏、哑金罗马数字、衬线标题，跟随系统深浅色并可手动切换），界面文字全部中文；字体走本机字体栈不加外链，断网照常。后端生成的裁判理由、建议、事件文案仍是英文，事件里常见的几种（已打分 / 已裁决 / 导入 / 阶段完成）在前端翻成了中文，其余原样：

- **裁判理由并排**：`GET /runs/{id}/traces/{trace_id}`。四个维度 x 三位裁判的分数、置信度、证据步骤、理由并排成表，分歧行标黄；最右一列是最终裁决（来源 panel / jev / local、降级标记、审计抽样结果）；下方对话记录把所有裁判引用的证据步骤高亮。入口有三个：时间线里 `scored …` / `decided …` 行可点，卡片上的代表 trace 可点，簇成员表每行可点；也可以用下拉框加左右键逐条翻。对应终端命令 `sparkjury verdicts <trace_id>`。
- **簇明细下钻**：`GET /runs/{id}/clusters` 加 `GET /runs/{id}/traces?cluster=<cid>`。每个簇的 rank、label、size、severity、priority、失败维度计数，展开后是成员明细表（四维最终分、严重度、证据摘录），行点开就是上面的 verdicts 视图。
- **回归对比**：`GET /runs/{id}/regress?before=<run_id>`。before 从 run 列表里选，after 是当前 run；显示 verdict 徽标（improved / regressed / mixed / unchanged）、pass^1 与 pass^k 前后及差值、badcase 数、fixed 与 broken 任务清单、四维平均分变化、簇的增减，并给出等价的 `sparkjury regress` 命令。

顺手修了一个原有 bug：回放已结束 run 的事件流时，`run_end` 事件会再次触发 `loadRun`，页面无限重载、每轮都打一遍 API。现在只有实时流里收到的 `run_end` 才重载。另外加了内联 SVG favicon，浏览器不再请求 `/favicon.ico` 报 404。

**自测**：`uv run pytest tests/test_m8_api.py` 新增一条页面用例（三个视图的挂点、调用的端点、无外网资源）；Playwright 驱动本机 Edge 在 1920x1080 无头跑了一遍三块视图（时间线点击、翻页、簇展开、成员点开、回归对比），控制台无 JS 报错。断网条件：页面无任何 `http(s)://` 外链，字体走系统字体栈。

**验收人**：滨辉（`docs/TEAM.md` 第 5 轮表）

---

## M9 Agent Skills 打包 + NeMo Agent Toolkit 集成（2026-09-26）

**做了什么**

- `skills/`：六个 Skill，每个一个目录，严格按 Agent Skills 规范（agentskills.io）和 NVIDIA/skills 仓库的要求：
  - `SKILL.md`：frontmatter 含 name（等于目录名、小写连字符）、description（写清做什么和何时用）、license、compatibility、metadata；正文是何时用、步骤、命令、输出、边界情况。
  - `skill-card.md`：NVIDIA 注册表要求的治理元数据：负责人、版本、风险级别、数据去向、网络、副作用、评测数据集、签名。
  - `scripts/run.py`：薄封装，调用对应的 CLI 命令，参数透传；clean 和 score 两个 skill 各串两条命令。
  - 六个名字：sparkjury-clean、sparkjury-evalset、sparkjury-score、sparkjury-cluster、sparkjury-report、sparkjury-regress。
- `skills/README.md`：安装、校验、签名与注册说明。`skills/sparkjury.component.yaml`：注册表 components.d 的产品登记草稿。
- `scripts/gen_skills.py`：从一份定义生成六个 skill，改 CLI 后重跑即可。`scripts/validate_skills.py`：按规范校验（name 字符集与长度、name 等于目录名、description ≤1024、compatibility ≤500、metadata 为字符串映射、SKILL.md 不超过 500 行、脚本与卡片齐全），等价于 skills-ref validate。`scripts/sign_skills.sh`：OMS 签名与验签的命令，签名本身需要团队证书，由万凌执行。
- NeMo Agent Toolkit 集成：
  - `src/sparkjury/adapters/nat.py`：把 NAT 的 EvalInputItem 或 workflow_output.json 行（含 intermediate_steps 的 LLM_END / TOOL_START / TOOL_END 事件）转成 Trace。对象和字典两种形态都接受。
  - `src/sparkjury/integrations/nat_eval.py`：评估器核心，不依赖 nvidia-nat 即可测试。每条记录先 Precheck，环境失败得 0 分并说明原因；否则三裁判打分加仲裁，score = 各维度最终分均值 / 4，reasoning 里带每维分数、outcome 标签、仲裁来源、裁判理由。
  - `nat/nat_sparkjury/`：NAT 插件包。`register.py` 用 `EvaluatorBaseConfig` 子类（name="sparkjury"）加 `@register_evaluator` 注册，`pyproject.toml` 通过 `nat.components` 入口点让 NAT 发现它。
  - `nat/configs/sparkjury_eval.yml`：`nat eval` 配置示例，同时挂 NAT 自带的 trajectory 评估器、profiler 和我们的 sparkjury 评估器，judge 指向本地 vLLM。
  - `nat/README.md`：安装与运行步骤，以及"NAT 提供轨迹评估与 profiler，SparkJury 补跨模型仲裁与归因优先级"的定位说明。
- `tests/test_m9_skills_nat.py`：18 个用例（3 个按设计跳过）：十一个 skill 存在且全部通过规范校验（六个阶段 skill + 五个治理 skill：arbitrate/calibrate/clarify/govern/prioritize，2026-09-27 扩入）、frontmatter 内容、校验器能抓坏例、校验脚本可运行、单命令封装能到达 CLI 的 --help、全部脚本可编译；NAT 适配器对字典和对象两种输入的步骤重建与错误识别；评估器核心的打分范围、维度子集、批量均值、环境失败得 0；插件文件一致性。

**自测结果**：`uv run pytest` 82 passed, 3 skipped。`scripts/validate_skills.py` 6/6 valid。另外手动跑了 sparkjury-report 和 sparkjury-clean 两个封装脚本，正常产出。

**未做（需要 DGX 或团队资源）**
- 在装了 nvidia-nat 的环境里实际执行 `nat eval`。本机没装 NAT（依赖重），插件代码按官方文档写，在 DGX 上按 `nat/README.md` 安装后验证。
- OMS 签名，需要团队证书。

**人工验证步骤**

```bash
cd sparkjury
uv run pytest
uv run python scripts/validate_skills.py
uv run python skills/sparkjury-report/scripts/run.py --db runs/demo-1/sparkjury.db --out runs/demo-1/card2
cat skills/sparkjury-score/SKILL.md   # Windows PowerShell: type skills\sparkjury-score\SKILL.md
```

预期：pytest 82 passed 3 skipped；校验器打印 6/6 skills valid；封装脚本先打印它调用的命令，再产出卡片；SKILL.md 开头是规范要求的 frontmatter。

**验收人**：待确认

---

## M10 DGX 部署 + τ²-bench 跑数 + 演示数据（2026-09-26，脚本部分）

**做了什么**

- `deploy/dgx/`：节点上的全套脚本，全部通过 bash 语法检查。
  - `env.example`：密钥（StepFun、Jev、API token）、四个模型名、四个端口、128G 统一内存的显存分配比例。复制成 `.env` 后填写，已加入 .gitignore。
  - `common.sh`：公共函数。优先使用组委会预置的 /home/xsuper/models，其次 ~/models；vLLM 一律绑 127.0.0.1。
  - `setup_node.sh`：按手册做开机自检，装 uv，建三个隔离的 venv（sparkjury、vLLM、tau2-bench）。GB10 是 aarch64 加 CUDA 13，pip 装 vLLM 失败时 README 给了 NVIDIA 容器的替代命令。
  - `download_models.sh`：在节点上用 ModelScope 下载，已有的跳过，绝不走 scp。
  - `start_judges.sh`：一个 tmux 会话五个窗口：judge_a、judge_b、embedding、被评 Agent、API。等三个裁判的 /models 接口就绪后打印状态。
  - `status.sh` / `stop_all.sh`：查看与停止，只动自己用户的进程。
  - `run_tau2.sh`：跑 τ²-bench retail，被评 Agent 走本地 8004 端口，模拟用户走 judge_a 的模型（与被评 Agent 权重不同），3 trial，结果落 data/simulations/ 并自动导入。
  - `make_demo_bundle.sh`：把一次运行的 db、清单、事件流、卡片打成 tar.gz，笔记本上解压后 `sparkjury serve` 就能离线回放，决赛不依赖节点在线。
- `deploy/README.md`：节点上的六步操作手册和排障表，含 SSH 端口转发、tmux、9000 到 9030 的映射、token 用法。
- API 访问令牌：`SPARKJURY_API_TOKEN` 或 `serve --token`。设了之后除 /health 外所有路由都要 `Authorization: Bearer` 或 `?token=`；Cockpit 页第一次带 ?token= 打开后记在浏览器里，之后自动附带。**没设 token 时只服务回环来的请求**：绑公网又不给 token，`sparkjury serve` 直接拒绝启动（exit 2），`deploy/dgx/start_judges.sh` 在起 tmux、碰 vLLM 之前就把它拦掉——以前只打一句警告，日志里滚过去谁也没看见，而节点手册的红线是"8888 和 9000 上对外提供的服务必须有鉴权"。这是节点手册"公网端口必须加访问控制"的要求。
- 请求里的路径都要归位：`run_id` 只能是单层目录名（`RunManager.run_dir()` 是所有读写的公共出口），`db` 必须落在 `runs_dir` 之内，`config_path` 必须落在服务进程工作目录之内，越界一律 400。`reset_db` 的 `unlink()` 只会作用在 `runs_dir` 之内，越界让这次 run 明确失败而不是删掉宿主机上的任意文件。
- `tests/test_m10_deploy.py`：18 个用例：token 拒绝与放行、环境变量来源、默认关闭、页面转发 token、绑公网无 token 拒绝启动、部署脚本在动手前拦下空 token；脚本齐全且 bash -n 通过；env.example 覆盖脚本用到的全部变量；三处配置里端口一致、vLLM 只绑回环、显存比例之和留有余量；tau2 脚本的 Agent 与模拟用户用不同模型。

**自测结果**：`uv run pytest` 96 passed, 3 skipped。

**本机做不了、要在节点上做的（按 deploy/README.md 顺序）**
1. `setup_node.sh`：确认 vLLM 在 GB10 上能装（pip 或容器二选一）。
2. `download_models.sh`：先看预置目录有没有 Qwen3-30B-A3B、Gemma 4、Qwen3-Embedding。
3. `start_judges.sh`，然后 `status.sh` 看三个裁判是否就绪。
4. `run_tau2.sh 30 3 4` 先跑 30 个任务，确认 tau2 的参数名与安装版本一致。
5. `sparkjury run --config deploy/run.toml` 接真实裁判跑一遍，`make_demo_bundle.sh` 打包。

**人工验证步骤（本机）**

```bash
cd sparkjury
uv run pytest
export SPARKJURY_API_TOKEN=test123          # Windows PowerShell: $env:SPARKJURY_API_TOKEN="test123"
uv run sparkjury serve --host 127.0.0.1 --port 9000
```

预期：pytest 96 passed 3 skipped。浏览器直接开 http://127.0.0.1:9000/ 会显示 401 提示；改开 http://127.0.0.1:9000/?token=test123 正常进入，之后不带 token 也能用，因为已记在浏览器里。

**验收人**：待确认

---

## M11 README / 征文 / 视频脚本（2026-09-26）

**做了什么**

- `README.md`：按备赛指南的仓库结构写：Problem、Demo、Why DGX Spark、Architecture、Agent System、Skills / Tools、Agent Loop、Models、Evaluation、Benchmarks、Failure Recovery、Quick Start、Demo Video、Screenshots、Limitations、Team、Docs。中文正文 500 字以上，写明 NVIDIA 技术栈（DGX Spark、vLLM、NeMo Agent Toolkit）与 StepFun 模型，满足提交要求里的项目说明、部署说明、技术栈说明三项。
- `docs/VIDEO_SCRIPT.md`：3 分 30 秒脚本，按 Problem → Magic → Explain：30 秒讲问题，90 秒 Wow（环境失败被排除、三裁判并行、分歧送 Jev、卡片弹出、改一行 prompt 后 pass^3 上涨），55 秒讲为什么要 DGX 和边界，附录制清单和断网备选段。
- `docs/ESSAY_十日谈.md`：征文。9 月 20 日到 26 日按实际经过写完，27 到 29 日留提纲，节点跑完后补数字。
- `docs/SUBMISSION_CHECKLIST.md`：提交清单与评分维度自查，已完成项打勾。
- `tests/test_m11_docs.py`：2 个用例：README 章节齐全、中文字数 ≥ 500、技术栈关键词齐全；三份提交文档存在且内容对应。

**自测结果**：`uv run pytest` 98 passed, 3 skipped。

**待补（需要节点数据或人工）**：README 的 Screenshots 三张图、Benchmarks 里真实裁判与 τ²-bench 30 x 3 的数字、视频录制、征文 27 到 29 日、合影、仓库推送。

**验收人**：待确认

---

## M10 节点执行记录（2026-09-26 晚）

登录方式：从登录信息表读取密码，paramiko SSH，`ssh -p 6030 asus_gx10@61.172.235.130`。

**预检发现**
- 节点：GB10，aarch64，119 GB 内存，693 GB 空闲磁盘，Python 3.12.3，docker 和 tmux 可用。
- 组委会预装了 vLLM 0.28.0（~/envs/vllm，torch 2.13），不需要自己装。
- 队友已下载 Qwen3.8-27B（52 GB，密集，9-22 试跑只有 4.6 tok/s）和 Nemotron-3.5-Lightning-30B-A3B-NVFP4（21 GB）。
- 选型调整：Judge B 改用 Nemotron（NVIDIA 家族、MoE、NVFP4），Gemma 4 作备选。

**做了什么**
1. 上传仓库（200 KB tar），uv 装好后 `uv sync` 卡住 7 分钟：uv 默认去 GitHub 下托管版 Python、去 PyPI 下包，节点到这两处都慢。改成 `~/.config/uv/uv.toml` 指定 only-system Python 加清华镜像，1 分钟同步完。
2. 节点上 pytest 通过（M8 的 API 测试除外，未跑），`sparkjury run --demo` 跑通。
3. 生成随机 API token 写进 deploy/dgx/.env。
4. 发现本机 Write 出来的文件是 CRLF，节点 bash 报 `$'\r'`。全仓库转 LF，加 .gitattributes 强制 LF。
5. 用预装 vLLM 起 Nemotron（端口 8002，显存比例 0.22）：一次成功。
6. 用 Nemotron 当真实裁判打分：第一次 52 票 10 票解析失败，原因是 Nemotron 把推理过程当正文输出，600 token 内到不了 JSON。两处修复：chat_template_kwargs 里 enable_thinking=false；max_tokens 提到 1200；解析器改成平衡括号扫描，容忍 think 标签、多段 JSON。修复后 12 票 0 错，平均 2.3 秒一票，理由合理。
7. 顺手修了 prompt 里步数把隐藏的 system 步算进去的 bug。
8. 模型下载走节点 vLLM 环境自带的 modelscope，聚合速度约 100 MB/s：Qwen3-30B-A3B-Instruct-2507、Qwen3-Embedding-0.6B、Qwen3-8B 依次下载。
9. tau2-bench 在独立 venv 里安装中。

**踩的坑**
- `pkill -f "uv run"` 会匹配到自己所在的 bash 命令行，把自己杀了；改用 `^uv` 锚定或 `[u]v` 写法。
- paramiko SFTP 用绝对路径 open 报 No such file，用相对 home 的路径正常。
- `uv tool install modelscope` 找不到可执行入口，直接用 ~/envs/vllm/bin/modelscope。

**节点执行续（2026-09-26 深夜）**
- 全部服务已在节点常驻，用 setsid+nohup 脱离 tmux 和 SSH，pid 记在 `run/*.pid`，并 `loginctl enable-linger`：8001 Qwen3-30B-A3B-Instruct-2507-FP8（显存比例 0.32）、8002 Nemotron NVFP4（0.22）、8003 Qwen3-Embedding-0.6B（`--runner pooling --convert embed --kv-cache-memory-bytes 1GiB`，绕过统一内存上的空闲内存预检）、8004 Qwen3-8B 被评 Agent（0.15）、9000 API（token 保护，公网 http://61.172.235.130:9030 已验证 200/401）。
- 12:54 事故：bf16 版 Qwen3-30B（57 GB）与 Nemotron 同驻触发内核 OOM，systemd 把整个用户会话连 tmux 一起杀掉。改用 FP8 版（31 GB）、显存比例总和 0.73、开启 linger 后稳定。
- 真实裁判首轮（`node-real-samples`，run.node.toml）：13 条 trace，judge_a 52 票 0 错 7.1 s/票，judge_b 52 票 0 错 5.1 s/票，一致率 76.9%，3 条进仲裁（本地降级，无 Jev key），全流程 131 秒。judge_c（StepFun）无 key，健康检查失败自动换 mock 并标降级。
- 用真实裁判理由聚类后，规则标签器改成"具体类别优先于 loop"，并补充真实裁判的措辞模式。结果：#1 wrong_tool（3 条）、#2 unauthenticated_action（2 条）。
- 解析器加了 JSON 修复（尾随逗号、字符串内换行、弯引号），实测 Nemotron 唯一一票解析失败属此类。
- tau2-bench：GitHub 直连 clone 反复 early EOF；改从 gh-proxy 镜像下 94 MB 源码 zip，装进 .venv-tau2，补装 websockets。参数名核对：--agent-llm/--agent-llm-args/--user-llm/--user-llm-args/--num-trials/--num-tasks/--max-concurrency/--save-to 全部存在；`--save-to` 是名字不是路径，结果落在 ~/tau2-bench/data/simulations/<name>.json，脚本已改。
- SSH 网关会掐断闲置 5 到 10 分钟的连接，远程命令要短，长任务放 tmux 或 nohup。
- 2 任务 × 1 trial 冒烟：Agent 端点 28 次调用，模拟用户走 judge_a；litellm 的 "model isn't mapped" 只是计费表缺项，可忽略。

---

## M12 Windows 与 macOS 双平台（2026-09-27）

**做了什么**
- 文档里所有 Windows 专用写法改成双平台：PowerShell 代码块改为通用 bash，`cd D:\...` 改为 `cd sparkjury`，`start` / `type` / `set` 都给出 macOS 对应写法。新增 `docs/CROSS_PLATFORM.md` 汇总差异。
- `.editorconfig` 强制 LF 与 UTF-8，配合已有的 `.gitattributes`，任何平台的编辑器都不会再写出 CRLF。
- `scripts/node.py`：用 paramiko 连节点，不依赖系统 ssh / scp，Windows 和 mac 一样用。子命令 run / put / get / sync，sync 会打包上传本仓库并在节点上解压、顺手把脚本转成 LF。凭据从环境变量或 `deploy/dgx/node.env` 读，都没有就交互式输入密码，不写进仓库。
- `scripts/screenshot.py`：用 Playwright 驱动本机已装的 Edge（Windows）或 Chrome（mac）截图，等看板把卡片加载完再拍，解决了经过慢网关时 `--headless --screenshot` 拍到空页的问题。
- `pyproject.toml`：新增 `ops` 依赖组（paramiko、playwright），加 OS Independent 分类。
- `tests/test_m12_crossplatform.py`：5 个守卫用例：仓库文本文件无 CRLF；editorconfig 与 gitattributes 固定 LF；文档没有只在 Windows 能跑的命令；辅助脚本可编译且不用 shell=True / os.system；ops 组已声明。

**自测结果**：Windows 上全套通过。macOS 未实机测试，依赖均有 Apple Silicon 轮子。

**验证**
```bash
cd sparkjury
uv sync && uv run pytest
uv sync --group ops
uv run --group ops python scripts/node.py run "nvidia-smi"    # 会提示输入节点密码
```

---

## M13 Agent harness（2026-09-27）

**做了什么**
- 参照 pi（earendil-works/pi）的 harness 分层，新增 `src/sparkjury/agent/`：`ai.py`（四个本地端点 + 云端共用一个 `complete()`，带工具调用与 usage）、`tools.py`（工具注册表 + 六个技能按需加载与执行 + 两个只读文件工具）、`loop.py`（agent loop 与 steering / follow-up / abort）、`session.py`（只追加、带 parent 指针的会话树）、`runtime.py`（run 目录、事件流、用量账本、manifest）、`cli.py`（`sparkjury agent run / tools / replay / endpoints`）。
- 两条决定行为的规矩：技能正文不进 system prompt，只放一句话描述，模型要看细节自己调 `load_skill`；工具失败是消息不是崩溃（记一笔失败，收尾进 manifest 的 `degradations`）。
- 事件流复用 M7 的 `EventBus`（`stage=AGENT`），run 落在 `runs/agent-*/`：`session.jsonl` / `events.jsonl` / `usage.jsonl` / `manifest.json`。
- 新文档 `docs/AGENT_HARNESS.md`；README 的 Agent System、Skills、Quick Start 三处补入口。

**中层（同一天补上）**：最小闭环只会往前跑，进程没了就说不清上次跑到哪。补齐 pi 的 durable 那段——
`store.py`（三个存储 + 原子事务，整份替换走临时文件 + `os.replace`，被写坏的最后一行跳过但记下来）、
`ops.py`（一次 run 是一条操作，只追加的 `ops.jsonl`，当前状态折叠得出）、`loop.py` + `runtime.py` 的
`resume`（已有结果的工具调用重放而不重跑，只有开始标记没有结果的按「状态未知」处理，不许自动重跑）、
`compact.py`（超预算时插一条摘要 entry 顶替更老的消息，原文一条不删）、`hooks.py`（请求前可改消息、
工具执行前可拦下、执行后与每轮结束可观察）。四个原语 `accept` / `drive` / `request_abort` / `inspect`
落在 runtime 上，命令行是 `agent ops` / `agent resume` / `agent compact`。

**自测结果**：`uv run pytest tests/test_m13_agent.py tests/test_m13_durable.py -q` 65 个用例全绿（全离线：脚本模型 + 离线执行器，不联网不起子进程）。`uv run sparkjury agent run --demo` 端到端 5 轮 4 次工具调用，结束方式 `end_turn`，manifest 无降级项。

**验证**
```bash
uv run pytest tests/test_m13_agent.py -q
uv run sparkjury agent tools
uv run sparkjury agent run --demo
```

---

## 分工口径：Skill 库（卢万凌，2026-09-27）

这一节不是模块验收，是一次任务口径的重新划定，写下来免得后面两边对不上账。

**为什么这么划**：六个 Skill 本来就只是 CLI 的薄壳，`skills/*/scripts/run.py` 六个文件各 57 行，彼此只差一行 `MODE`，做的事是把参数透传给 `sparkjury` 可执行文件，找不到就退回 `python -m sparkjury.cli`。真正对外的那层接口在 `src/sparkjury/cli.py` 的 16 个子命令里，不在 Skill 库里。所以 Skill 库把评测内核（`judges/`、`arbiter/`、`cluster/`、`report/`、`regress/`）归到"由李滨辉提供的基础设施"一栏，卢万凌的追责范围收窄成下面三件事。

**第一件，把六个 Skill 写完整并提交进来。** 每个三件套齐全（`SKILL.md`、`skill-card.md`、`scripts/run.py`），只通过 `sparkjury` CLI 调用能力，不在 Skill 里自己实现评测逻辑。接口不够用就提需求，不要绕开 CLI 直连内部模块。

**第二件，把测试做真。** 六个 Skill 每个至少一条端到端用例，从封装脚本入口执行到有产出为止。现状是 `scripts/validate_skills.py` 只查格式合规（name 字符集与长度、name 等于目录名、description 不超 1024、SKILL.md 不超 500 行、脚本与卡片齐全），`tests/test_m9_skills_nat.py` 里跟 Skill 相关的用例只到 `--help` 和编译通过，六个封装里只有 clean 和 report 被手动跑过，`sparkjury-evalset`（要现写临时 TOML 再 `run --config`，最容易坏）从来没被执行过。

**第三件，产出 Skill 到本地模型的路由表。** 口径按节点上常驻的这几个本地模型定：请求路由到某个本地模型角色时，该用哪个 Skill。表要落到 `skills/README.md`，不能只存在于讨论里。

| 本地端点 | 模型 | 在流水线里的角色 | 路由到哪个 Skill | 触发条件 | 端点不可达时 |
|---|---|---|---|---|---|
| 127.0.0.1:8001 | Qwen/Qwen3-30B-A3B-Instruct-2507-FP8 | judge_a，三裁判之一；Jev 不可达时的本地仲裁者；τ²-bench 的模拟用户 | sparkjury-score | SCORE 与 ARBITRATE 阶段 | 健康检查 3 秒不通过换成 mock 裁判，manifest 标 degraded |
| 127.0.0.1:8002 | nvidia/Nemotron-3.5-Lightning-30B-A3B-NVFP4 | judge_b，三裁判之一，第二个家族 | sparkjury-score | SCORE 阶段 | 同上，换 mock 并标 degraded |
| 127.0.0.1:8003 | Qwen/Qwen3-Embedding-0.6B | 聚类用的向量化服务 | sparkjury-cluster | CLUSTER 阶段有 badcase 时 | 自动换零依赖哈希向量 |
| 127.0.0.1:8004 | Qwen/Qwen3-8B | 被评 Agent，被评测的对象 | 不路由 Skill；它跑 τ²-bench 产生的轨迹是 sparkjury-clean 的输入 | 跑 τ²-bench 时 | 这一轮数据作废，不进流水线 |

不在本地清单里的两个：judge_c 走 StepFun 云 API，Jev 走 TypeSafe 云 API。两者没 key 时分别是换成 mock 裁判和退回本地仲裁，都会写进 manifest 的 `degradations`。

**交付验收**

- `uv run python scripts/validate_skills.py` 六个全过。
- 六个 Skill 的端到端用例全绿，不再是只跑 `--help`。
- 路由表覆盖上表每一个本地端点，每行都能说出为什么是那个 Skill。
- OMS 签名在提交前由他执行（`scripts/sign_skills.sh`，需要团队证书）。

**不归他**：CLI 子命令、评测内核、存储层、编排器、部署脚本。**依赖**：李滨辉提供 CLI 与全部底层接口。

**验收人**：陈人瑜（使用者视角，照着 SKILL.md 能不能把命令跑出来；口径见 `docs/TEAM.md` 第 2 轮 B 组）

---

## 分工口径：真实场景与判定规则（徐千富、陈人瑜，2026-09-27）

上一节收窄了 Skill 库的追责范围，这一节把"实用性 25%"那条证据落到人头上。评分表 `docs/ARCHITECTURE.md:402` 和 `docs/SUBMISSION_CHECKLIST.md:33` 的同一行写的是"千富真实场景做故事"，但仓库里这句话现在只有两个落点：`README.md:15` 的"我们自己的两家公司也是这样：一家 11 个营销场景的 badcase 全靠产品经理人肉翻记录，另一家 80 人产研没有一个评测岗"，和 `docs/ESSAY_十日谈.md:11` 的"营销 Skill 路由（有真数据，但脱敏是坑）"。两处都是一句带过。

架构图里承诺得比这更远：`docs/ARCHITECTURE.md:71` 的输入层画了三条进来的路，除了 τ²-bench 轨迹和 OTel trace，第三条写的就是"千富脱敏 trace"，接进 Ingest（M1）。文档层面已经承诺这份数据会进流水线，但 `data/` 目录下至今只有 `data/samples/otel_sample.json` 和 `data/samples/tau2_retail_sample.json` 两个样本，真实数据一条没有。

所以这一块交给两个人合担，交付三样。内部偏重按 README 的分工走：千富偏数据与故事，人瑜偏定位与判据规则。

**一、场景的数据**

定清楚数据从哪来、什么形态、怎么脱敏、能公开到什么程度。要回答四个问题：真实 trace 从哪套系统导出、导出成什么格式（是否直接对齐 M1 的 Trace 模型，还是需要写一层转换）、脱敏做到什么粒度（用户标识、手机号、订单号、商品名分别怎么处理）、哪一部分能进仓库哪一部分只能留在本地。脱敏后的样本要落到 `data/` 下，格式跟 `data/samples/tau2_retail_sample.json` 对齐，让 M1 的 ingest 能直接吃进去，架构图那条线才算真的通了。原始数据不必进仓库，但脱敏规则和样例形态要写下来。

**二、定好"什么是好"的规则**

这条是这一节里最要紧的。项目里"好"的定义现在只存在于公开 benchmark 上有金标的那一套：`src/sparkjury/judges/rubrics/` 四份 rubric 各写了一份 0 到 4 分的量表，以 `src/sparkjury/judges/rubrics/outcome.md` 为例，4 分是目标完全达成、最终状态正确，3 分是达成但有小遗漏且不影响最终状态，2 分是部分达成，1 分是没达成但做了合理尝试，0 分是没达成或做了与要求相反的事，3 到 4 分记 pass，0 到 2 分记 fail。

这套量表能成立，靠的是它自己写明了的那个前提——原文是 the gold-standard outcome computed by the benchmark (a database comparison after the conversation)，也就是 τ²-bench 跑完对话后比对数据库得到的标准答案，而且它用了 "when present" 限定自己。

真实营销场景没有这个金标库。用户说"把这个活动文案改一改"，什么算改好了，数据库比对不出来，只能人来定。所以第二个交付是把"什么是好"在真实场景里补出来：这类任务的成功判据是什么、谁来判、判的时候看哪些证据、哪些判据是硬规则（比如不能碰价格、不能承诺库存）哪些可以商量。这份规则要跟现有四个维度对得上——outcome、tool_use、efficiency、safety 在营销场景里分别对应什么，对不上的地方直接写对不上，不要硬套。

**三、故事**

把两家公司从一句话扩成可以展示的素材：场景是什么、Agent 在干什么、badcase 长什么样、产品经理现在怎么发现问题、用 SparkJury 之后省掉哪一步。素材要能直接喂给 `docs/VIDEO_SCRIPT.md` 和 `docs/ESSAY_十日谈.md`，不是另写一份放着。

**交付验收**

- `data/` 下至少一份脱敏后的真实 trace 样本，能被 `sparkjury ingest` 读进去。
- 脱敏规则成文，写清哪些字段怎么处理、什么数据不出本地。
- "什么是好"的规则文档成文，覆盖真实场景的成功判据，并说明与现有四维 rubric 的对应和缺口。
- 故事素材能被视频脚本和征文直接引用。

**不归他**：Trace 数据模型与 ingest 适配器的实现（归李滨辉），rubric 的机器可读部分与打分内核（在 CLI 里，归李滨辉）。

**依赖**：李滨辉提供 ingest 接口与 Trace 模型定义；真实环境里的运行记录由他们自己在业务侧取。

**验收人**：卢万凌（懂评测口径，也要拿这些数据跑自己的 Skill；口径见 `docs/TEAM.md` 第 2 轮 A 组）
