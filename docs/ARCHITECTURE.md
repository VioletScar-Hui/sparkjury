# SparkJury 架构与实现方案

第三届 NVIDIA DGX Spark 黑客松 · Agent Skills 开发挑战赛

项目代号：SparkJury（"Spark 上的陪审团"），可随时改名
整理日期：2026-09-26
提交截止：2026-09-29
基准文档：《评测 Agent：场景和我们要做什么》

## 1. 一句话目标

一台 DGX Spark，顶一个评测组。

读入别人 Agent 留下的 trace，在 DGX Spark 上用三家不同的本地模型打分，意见不一致的送云端 Jev 仲裁，把真正的 badcase 聚成几类、按频次和严重度排优先级，出一张证据卡片让 PM 拍板先修哪类，改完自动重跑并对比 pass^3。

两个角色必须分清：

| 角色 | 是谁 | 说明 |
|---|---|---|
| 被评的 | 一个电商客服 Agent | 不是我们写的，用公开题库 τ²-bench retail 域，一百多个任务，每个跑 3 次 |
| 做评的 | SparkJury | 这是我们的作品。只读别人的做题记录，不回答用户 |

## 2. 方案依据

### 2.1 参考文件

| 文件 | 用到的内容 |
|---|---|
| 飞书文档《评测 Agent：场景和我们要做什么》 | 四步流程、三家 judge、Jev 仲裁、四个维度、不做的三件事、模型清单、架构图与流程图 |
| 附件《英伟达黑客松-评测Agent调研.md》 | 技术边界、竞品格局、pass^k、假 badcase 在 harness 层打标、交换顺序、小模型 judge 可行性 |
| wiki《DGX_Spark黑客松——项目细节》 | 人员分工、排期、六个 Skill 名字、Cockpit 检查清单 |
| wiki《作品提交和评分规则》 | 评分权重、提交清单、奖品 |
| wiki《免费资源和优秀作品参考》 | StepFun Coding Plan、云节点资料 |
| 《Spark云节点访问与使用手册.docx》 | SSH、端口映射、tmux、传输限制、预置模型目录 |
| 《DGX_Spark_Hackathon_参赛标杆拆解与备赛指南.docx》 | README 结构、Problem → Magic → Explain 演示法、八要素检查表 |
| 《登录信息表.xlsx》 | 团队 DGX 节点登录地址与端口 |
| τ²-bench 源码 simulation.py / message.py | Trace 字段名、reward_info、termination_reason 取值 |
| OTel GenAI 语义约定 | OTel 适配器的 span 属性名 |

### 2.2 哪些是团队定的，哪些是本方案补的

团队文档已定：做什么、四步流程、三家 judge、Jev 仲裁、四个维度、六个 Skill、Cockpit 四块、模型清单、人员分工、不做的三件事。

本方案补充（可改）：

- 模块拆分 M1 到 M11 和构建顺序
- 技术栈具体选择：uv、Pydantic、SQLite、Typer、FastAPI、HDBSCAN
- 每个模块的验收标准、Precheck 六条规则
- 被评 Agent 用 Qwen3-8B，Judge A 用 Qwen3-30B-A3B，解决"被评模型不能当 judge"与模型清单只有一个 Qwen 的矛盾
- 按模块排的四天排期
- 风险兜底表
- 项目代号

## 3. 三天内的取舍原则

| 原则 | 具体做法 |
|---|---|
| 先跑通再好看 | 每个模块都带 mock 实现，断网、没 GPU 也能端到端跑完整流程 |
| 数据契约先行 | 第 1 个模块把 Trace / Verdict / Card 三个数据模型定死，所有人对着它开发 |
| 本地优先，云端兜底 | 三个 judge 在 DGX 本地 vLLM；Jev 只做分歧仲裁；Jev 不可达退回本地 Qwen 并打降级标记 |
| 不承诺根因 | 只输出聚类、优先级、证据；卡片上写清楚是建议不是结论 |
| 评委视角 | 每个模块都要能在 Cockpit 上被看见：Agent 在做什么、DGX 在承担什么 |

## 4. 总体架构

```
输入层            Harness 编排层               评测 Skill 库               模型层
─────────         ────────────────             ─────────────────           ──────────────────
τ²-bench 轨迹 ──┐                              S1 clean   数据清洗          DGX 本地 (vLLM)
OTel trace   ──┼─▶ Ingest ─▶ Precheck ─▶ Orchestrator ─▶ S2 evalset 评测集   ├ Judge A Qwen3-30B-A3B
千富脱敏 trace ─┘   (M1)      (M2)          (M7)          S3 score   打分 ───▶├ Judge B Nemotron-3.5 30B-A3B
                                              │           S4 cluster 聚类     ├ Judge C Step 3.7 Flash*
                                              │           S5 report  卡片     └ Embedding Qwen3-Emb
                                              │           S6 regress 回归     云端
                                              │                                ├ Jev (仲裁)
                                              ▼                                └ StepFun API (*兜底)
                                     Agent Cockpit (M8)
                                     时间线 / DGX 面板 / 证据卡 / 回归报告
```

数据流：

```
Trace → PrecheckFlag → Verdict × 3 → Arbitration → BadCase → Cluster → EvidenceCard → RegressionReport
```

运行流程：

```
导入 trace
  → Precheck：环境问题打标，不进评分
  → 生成评测集
  → 三 judge 并行打分，4 个维度
  → 三票一致？是：写入分数；否：升级 Jev 仲裁；Jev 不可达：本地 Judge A 仲裁并标降级
  → 是 badcase？否：结束；是：Embedding + HDBSCAN 聚类
  → Jev Choice 贴 MAST/TRAIL 标签
  → 按频次 × 严重度排优先级
  → 生成证据卡片
  → PM 选择修哪类 → 改 prompt / 工具描述 → 重跑 pass^3 → 回归报告
  → 全程 5% 抽样送强模型审计
```

## 5. 技术栈与模型

### 5.1 技术栈

| 层 | 选择 | 理由 |
|---|---|---|
| 语言 / 包管理 | Python 3.12 + uv | 团队熟悉，DGX 上安装快 |
| 数据模型 | Pydantic v2 | 与 τ²-bench、NeMo Agent Toolkit 同栈，JSON schema 免费得到 |
| 存储 | SQLite 单文件 + JSONL 导出 | 零运维，Cockpit 直接读，演示可拷走 |
| 推理服务 | vLLM，OpenAI 兼容接口 | DGX Spark 官方支持，MoE 模型吞吐好 |
| Judge 客户端 | openai SDK，base_url 可切 | 本地 vLLM、StepFun、mock 三种后端同一套代码 |
| 聚类 | sentence-transformers 或 vLLM embedding + hdbscan | Braintrust Topics 同款做法 |
| 编排 | 自研轻量状态机 + NeMo Agent Toolkit 评估器插件 | 拿 NVIDIA 平台分，又不被框架绑死 |
| 后端 API | FastAPI + SSE | 时间线实时推送 |
| 前端 | 剑乔负责；后端自带静态兜底页 | 前端没赶上也能演示 |
| CLI | Typer | 每个 Skill 是一个子命令，方便 Agent 调用 |
| 测试 | pytest | 每个模块带自测 |

### 5.2 模型清单（DGX Spark 128G 统一内存）

| 角色 | 模型 | 部署 | 显存估算 |
|---|---|---|---|
| Judge A | Qwen3-30B-A3B（FP8 / AWQ） | vLLM 端口 8001 | 约 20G |
| Judge B | Nemotron-3.5-Lightning-30B-A3B-NVFP4（NVIDIA，节点预置） | vLLM 端口 8002 | 约 21G |
| Judge C | Step 3.7 Flash | StepFun API（比赛要求）；显存够则本地 | 0 或约 30G |
| Embedding | Qwen3-Embedding-0.6B | vLLM 端口 8003 或 sentence-transformers | 约 2G |
| 仲裁 | Jev（TypeSafe） | 云 API | 0 |
| 被评 Agent | Qwen3-8B | vLLM 端口 8004 | 约 16G |

被评 Agent 与 Judge A 同族不同权重，README 里要说明。Judge B 原计划 Gemma 4 12B，9-26 登录节点后发现组委会预置了 Nemotron-3.5-Lightning-30B-A3B-NVFP4（NVIDIA 家族、MoE、NVFP4），改用它：省一次下载，平台适配更强。Gemma 4 作为备选。

## 6. 仓库结构

```
sparkjury/
├── pyproject.toml                # uv 管理
├── README.md                     # 按备赛指南结构写
├── docs/
│   ├── ARCHITECTURE.md           # 本文档
│   └── MODULES.md                # 模块验收记录
├── src/sparkjury/
│   ├── models/                   # M1 数据契约
│   │   ├── trace.py              #   Trace / Step / ToolCall / Outcome
│   │   ├── verdict.py            #   Verdict / PanelResult
│   │   ├── arbitration.py        #   Arbitration / TraceDecision
│   │   ├── precheck.py           #   PrecheckFlag / PrecheckResult
│   │   ├── cluster.py            #   BadCase / Cluster
│   │   └── report.py             #   EvidenceCard / CardCluster
│   ├── adapters/                 # M1 输入适配
│   │   ├── tau2.py               #   τ²-bench JSON → Trace
│   │   ├── otel.py               #   OTel span JSON → Trace
│   │   └── nat.py                #   NeMo Agent Toolkit 轨迹 → Trace
│   ├── store/sqlite.py           # M1 存储
│   ├── precheck/rules.py         # M2
│   ├── judges/                   # M3
│   │   ├── client.py             #   OpenAI 兼容客户端 + mock
│   │   ├── rubrics/              #   4 维 rubric
│   │   └── panel.py              #   三裁判面板
│   ├── arbiter/                  # M4  arbiter.py / jev.py
│   ├── cluster/                  # M5  badcase.py / embed.py / group.py / taxonomy.py
│   ├── report/cards.py           # M6
│   ├── regress/passk.py          # M6
│   ├── harness/                  # M7  orchestrator.py / config.py / events.py
│   ├── api/                      # M8
│   │   ├── app.py                #   FastAPI 路由与 token 鉴权
│   │   ├── runs.py               #   run 目录与 manifest 读写
│   │   ├── dgx.py                #   nvidia-smi 采样
│   │   └── static/index.html     #   Cockpit 单页前端
│   └── cli.py                    # 所有 Skill 的命令入口
├── skills/                       # M9 每个 Skill 一个 SKILL.md
├── nat/                          # M9 NeMo Agent Toolkit 配置
├── deploy/dgx/                   # M10 vLLM 启动脚本、env 模板
├── data/samples/                 # 演示与测试样本
└── tests/
```

## 7. 数据契约

### 7.1 Trace（M1，已实现）

| 字段 | 类型 | 说明 |
|---|---|---|
| trace_id | str | 唯一 ID |
| source | tau2 / otel / custom | 来源 |
| domain | str | 如 retail |
| task_id | str | 任务 ID |
| trial | int | 第几次跑 |
| agent_model | str | 被评模型 |
| steps | list[Step] | 有序步骤 |
| outcome | Outcome | success / reward / termination_reason / gold |
| metrics | TraceMetrics | n_steps / n_tool_calls / n_tool_errors / duration_s / tokens / cost |
| raw_ref | str | 原始文件位置 |

Step 字段：idx、role（system / user / assistant / tool）、content、tool_calls[]（call_id / name / arguments）、tool_result（call_id / content / is_error）、latency_ms、tokens_in、tokens_out。

### 7.2 Verdict（M3，已实现）

| 字段 | 说明 |
|---|---|
| trace_id, judge, dimension | 哪条、谁评、哪个维度 |
| score | 0 到 4 |
| label | pass / fail（outcome 维度） |
| evidence_steps | 出问题的 step 序号 |
| rationale | 一段理由 |
| latency_ms, model | 耗时与模型版本 |

### 7.3 Arbitration（M4，已实现）

dimension、final_score、final_label、source（jev / local）、degraded、audit_sampled。

### 7.4 EvidenceCard（M6，已实现）

run_id、总数、环境问题数、真 badcase 数、clusters[]（label / count / share / severity / priority / 代表 trace 与 step 摘录 / 三 judge 意见 / 建议）、pass^1、pass^3、judge 一致率、仲裁次数、降级次数。

## 8. 模块清单

| 模块 | 名称 | 优先级 | 状态 |
|---|---|---|---|
| M1 | 数据契约 + 输入适配 + 存储 | P0 | 已完成，9 个用例 |
| M2 | Precheck 假 badcase 打标 | P0 | 已完成，10 个用例 |
| M3 | 三裁判面板 | P0 | 已完成，15 个用例 |
| M4 | 仲裁与审计 | P0 | 已完成，10 个用例 |
| M5 | badcase 聚类与优先级 | P0 | 已完成，9 个用例 |
| M6 | 证据卡片 + 回归对比 | P0 | 已完成，8 个用例 |
| M7 | Harness 编排器 | P0 | 已完成，9 个用例 |
| M8 | API + Agent Cockpit | 后端 P0 / 前端 P1 | 后端与兜底页已完成，11 个用例 |
| M9 | Agent Skills 打包 + NeMo Agent Toolkit | P1 | 已完成，21 个用例（3 个跳过）|
| M10 | DGX 部署 + τ²-bench 跑数 + 演示数据 | P0 | 脚本与 token 已完成，18 个用例；节点上执行待做 |
| M11 | README / 征文 / 视频脚本 | P0 | 初稿已完成，2 个用例；截图、真实数字、录制待补 |
| M12 | 跨平台与仓库约定守卫 | P1 | 已完成，17 个用例 |

「N 个用例」指该模块测试文件被收集到的用例数（不是通过数），有跳过的在括号里注明。
这张表由 `scripts/certificate.py` 逐行核对，改测试不改表会红。

### M1 数据契约 + 输入适配 + 存储

做什么：

- Trace 统一模型，对齐 OTel GenAI 语义约定
- tau2 适配器：读 τ²-bench Results JSON，支持 messages 与 ticks 两种布局，reward ≥ 1 视为成功
- otel 适配器：按 invoke_agent / chat / execute_tool 三层 span 重建步骤，识别 error.type
- SQLite 存储：traces、runs 两张表，幂等写入，统计任务数、trial 分布、pass^1、pass^k、平均步数、终止原因
- CLI：ingest / stats / show / list / export
- 样本数据：4 任务 × 3 trial，埋了四种坏例（未确认即取消、用错工具、工具后端 503、编造信息）

验收：导入样本后 stats 报 14 条 trace、6 任务、pass^1 64.3%、pass^3 25.0%；pytest 全绿。

### M2 Precheck 假 badcase 打标

规则在 harness 层，不靠模型猜。每条规则输出 PrecheckFlag(kind, evidence_step_idx, note)。

| 规则 | 判定 |
|---|---|
| empty_trace | trace 里没有任何 step，空跑或采集失败 |
| infra_error | termination_reason 是 infrastructure_error / unexpected_error / user_error |
| timeout | termination_reason 是 timeout，或单步延迟超阈值 |
| context_overflow | termination_reason 是 context_window_exceeded |
| tool_unavailable | 同一工具连续 2 次以上 is_error，且错误文本含 unavailable / 5xx / connection |
| permission_denied | 工具错误文本含 permission / unauthorized / 403 |
| user_sim_broken | 模拟用户消息为空或重复 3 次以上 |

被打标的 trace 不进打分，卡片上单列"环境问题 N 条"。

验收：6 条样本各命中一条规则；正常 trace 零误报。

### M3 三裁判面板

| 维度 | 问题 | 输出 |
|---|---|---|
| outcome | 任务目标达成了吗，对照金标与最后状态 | pass / fail + 置信度 |
| tool_use | 工具选对了吗、参数对了吗、有没有该调没调 | 0 到 4 分 + 出错 step |
| efficiency | 多绕了多少步 | 冗余步数 + 0 到 4 分 |
| safety | 有没有未经确认的危险动作 | 0 到 4 分 + 危险 step |

机制：

- 三个 judge 来自三家，被评 Agent 底座不进面板
- 一致性：outcome 看三票 pass/fail 是否相同；分数维度看极差是否 ≤ 1
- JudgeClient(base_url, model, api_key)，超时、重试、并发可配
- MockJudge 用规则给分，离线测试与 demo 兜底

验收：mock 下 20 条 × 4 维 × 3 judge 全部产出 Verdict；接真实 vLLM 后单条延迟小于 10 秒。

### M4 仲裁与审计

- 三票不一致送 Arbiter。首选 Jev：Score 给分数维度，Bool 给 outcome，Choice 给归类
- Jev 超时 5 秒或返回 5xx，退回本地 Judge A，结果打 degraded=true
- 5% 随机抽样送强模型审计，发现小模型系统性漏判

验收：断网时全部走本地且标记降级；有网时 Jev 调用成功率大于 95%。

### M5 badcase 聚类与优先级

- badcase 定义：outcome 为 fail，或任一维度 ≤ 1 分（safety 更严，≤ 2 分即算失败），且未被 Precheck 打标
- 特征文本：失败 step 内容 + 工具调用 + 三份 rationale 摘要
- Embedding 后 HDBSCAN 聚类，min_cluster_size 为 3，噪声点归"其他"
- 每簇取 2 到 3 条代表交 Jev Choice 贴标签：wrong_tool / wrong_args / missing_confirmation / hallucinated_info / premature_stop / policy_violation / loop
- 优先级 = 频次 × 严重度，safety 失败乘 3，outcome 失败乘 2，其它乘 1

验收：50 条 badcase 聚成 3 到 8 簇，每簇有标签、代表样本、优先级分。

### M6 证据卡片 + 回归对比

- EvidenceCard 支持 JSON、Markdown、HTML 三种渲染
- regress --before --after：pass^k 前后对比、每簇数量变化、新增与消失的簇
- pass^k 按 τ-bench 定义：同一任务 k 次全过才算过
- 成对比较交换顺序跑两遍

验收：同一 db 跑两次回归差异为零；人为改坏一条 trace 后能看到变化。

### M7 Harness 编排器

- 状态机：INGEST → PRECHECK → EVALSET → SCORE → ARBITRATE → CLUSTER → REPORT → PM 确认 → REGRESS
- 每步写 runs 表与 run_manifest.json：时间、耗时、模型版本、降级记录
- 单条 trace 失败不阻塞整批；judge 后端不可达自动切 mock 并标记
- 事件总线：每次状态变更发事件，供 Cockpit SSE 消费

验收：sparkjury run --config demo.yaml 一条命令从 τ²-bench 文件跑到卡片，断网也能完成。

### M8 API + Agent Cockpit

| 方法 | 路径 | 用途 |
|---|---|---|
| POST | /runs | 启动一次评测 |
| GET | /runs | 列出所有 run |
| GET | /runs/{run_id} | 单个 run 的 manifest 与摘要 |
| GET | /runs/{run_id}/events | SSE 时间线 |
| GET | /runs/{run_id}/events.json | 同上的 JSON 快照 |
| GET | /runs/{run_id}/card | 证据卡片 JSON |
| GET | /runs/{run_id}/card.html | 证据卡片 HTML |
| GET | /runs/{run_id}/clusters | 聚类与优先级 |
| GET | /runs/{run_id}/traces | trace 列表（支持 cluster= 与 failed= 过滤） |
| GET | /runs/{run_id}/traces/{trace_id} | 单条 trace 与三 judge 意见 |
| GET | /runs/{run_id}/confirm | 读取 PM 的先修选择 |
| POST | /runs/{run_id}/confirm | 写入 PM 的先修选择 |
| GET | /runs/{run_id}/regress | 回归对比 |
| GET | /dgx | nvidia-smi 采样：显存、利用率、常驻模型 |
| GET | /health | 健康检查，唯一免 token 的路由 |
| GET | / | Cockpit 单页 |

这张表由 `scripts/certificate.py` 与 `src/sparkjury/api/app.py` 里注册的路由逐条比对，多写少写都会红。

请求里的路径一律先归位再用，越界报 400：`run_id` 只能是单层目录名（`RunManager.run_dir()` 是所有读写的公共出口），`db` 必须落在 `runs_dir` 之内（否则 `reset_db` 的 `unlink()` 会作用到宿主机上任意一个文件），`config_path` 必须落在服务进程工作目录之内。没配 token 时只服务回环来的请求；`sparkjury serve` 绑公网又不给 token 会直接拒绝启动，`deploy/dgx/start_judges.sh` 在起 tmux 之前就把这种情况拦掉。

Cockpit 三栏：左 USER TASK（本轮配置），中 AGENT TIMELINE（状态机进度与每条 trace 流水），右 DGX SPARK（模型显存、GPU 利用率、本地与云端调用计数）。底部 FINAL ARTIFACT 是证据卡片。

### M9 Agent Skills 打包 + NeMo Agent Toolkit

- 六个 Skill 各一个目录：SKILL.md（名称、触发条件、输入输出、示例命令）+ 调用 CLI 的脚本
- 按 NVIDIA/skills 仓库格式提交注册，万凌负责签名
- nat/configs/sparkjury_eval.yml 把 sparkjury score 注册为自定义 evaluator，judge 指向本地 vLLM
- README 说明：NAT 提供轨迹评估与 profiler，SparkJury 补多 judge 仲裁与归因优先级

### M10 DGX 部署 + τ²-bench 跑数 + 演示数据

- deploy/dgx/start_judges.sh：tmux 里每个 vLLM 端点一个窗口，全部只绑 127.0.0.1（judge_a 8001、judge_b 8002、embed 8003，agent 8004 可选）；对外只暴露 API 端口 9000
- deploy/dgx/env.example：StepFun key、Jev key、各 base_url
- τ²-bench 跑数：tau2 run --domain retail --agent-llm openai/qwen3-8b --num-trials 3，agent 指向本地 vLLM
- 预跑一份结果打包进 data/samples，断网时直接用

### M11 README / 征文 / 视频脚本

- README 结构：Problem / Demo / Why DGX Spark / Architecture / Agent System / Skills / Models / Evaluation / Failure Recovery / Quick Start / Limitations
- 视频：30 秒讲问题，60 到 120 秒出 Wow（三 judge 分歧被 Jev 仲裁、卡片弹出、改完 pass^3 上涨），剩下讲为什么要 DGX
- 征文：十日谈，每天一段

## 9. 关键接口

| 接口 | 输入 | 输出 |
|---|---|---|
| adapters.load(path, source) | 文件路径 | list[Trace] |
| precheck.run(trace) | Trace | list[PrecheckFlag] |
| panel.score(trace, dims) | Trace | list[Verdict]，3 × 维度数 |
| arbiter.decide(trace, verdicts) | Trace + Verdict | Arbitration |
| cluster.group(badcases) | list[BadCase] | list[Cluster] |
| report.build(run) | run_id | EvidenceCard |
| regress.compare(before, after) | 两个 db | RegressionReport |

## 10. DGX 节点与部署约定

| 项目 | 内容 |
|---|---|
| 登录 | ssh -p 6030 asus_gx10@61.172.235.130，密码见登录信息表 |
| 公网端口 | 7030 → 节点 7000，8030 → 节点 8888，9030 → 节点 9000 |
| 对外服务 | 只把 API 绑到 0.0.0.0:9000；vLLM 各实例只监听 127.0.0.1 |
| 长任务 | 一律放 tmux |
| 大文件 | 超过 1G 不走 scp，在节点上直接下载 |
| 预置模型 | 先看 /home/xsuper/models/ 有没有可复用权重 |
| 安全 | 对外端口加 token；不存私密数据；比赛结束节点清盘，代码及时 push |

## 11. 人员对应

| 模块 | 主责 | 协作 |
|---|---|---|
| M1 M2 M7 | 滨辉（Harness） | 骨架已由本方案搭好 |
| M3 M4 M5 M6 M9 | 万凌（Skill 库） | 接口与 mock 由本方案提供 |
| M8 Cockpit | 剑乔 | 后端 API 由本方案提供 |
| M10 部署与跑数 | 滨辉 | 脚本由本方案提供 |
| M11 文档与故事 | 千富、人瑜 | 全员 |

## 12. 排期

| 日期 | 目标 |
|---|---|
| 9-26 | M1 M2 完成并自测；DGX 上起 vLLM；开始 τ²-bench 跑数 |
| 9-27 | M3 M4 M5 接真实模型；M7 一条命令跑通；M8 后端 API |
| 9-28 | M6 卡片与回归；Cockpit 联调；录视频；README |
| 9-29 | 提交：仓库、视频、征文、合影 |

## 13. 风险与兜底

| 风险 | 兜底 |
|---|---|
| Gemma 4 或 Step 本地起不来 | Judge B 换任何非 Qwen 开源模型；Judge C 走 StepFun API |
| Jev 接口与预期不符 | Arbiter 抽象层已隔离；退回本地 Judge A 并标降级 |
| τ²-bench 跑数太慢 | 先跑 30 任务 × 3 trial；演示用预跑数据 |
| 前端没赶上 | 后端自带静态 Cockpit 页 |
| 千富数据拿不到 | 只做公开 benchmark，口头提真实业务 |
| DGX 节点不可用 | 全流程 mock 模式在笔记本上演示 |

## 14. 评分对照

| 评分维度 | 权重 | 我们靠什么 |
|---|---|---|
| 实用性、落地价值、创新性 | 25% | 本地部署 + 开箱即用评测体系，市面无人做；千富真实场景做故事 |
| 智能体与模型优化深度 | 25% | 跨三家模型陪审团 + Jev 仲裁 + 端云成本模型 |
| 项目完整性 | 20% | 从导入到回归的完整闭环，前后端都有，文档规范 |
| 平台适配性 | 15% | NeMo Agent Toolkit 底座、DGX 模型选型、StepFun 模型 |
| 演示效果 | 10% | Cockpit 同时展示 Agent 在做什么和 DGX 在承担什么 |
| 赛事征文 | 5% | CSDN 或知乎十日谈 |

## 15. 提交清单

- 开源仓库链接，README 含 500 字以上项目说明、部署说明、技术栈说明，附 Skill 的 markdown 文件
- 演示视频，上传 B 站后提交链接
- 十日谈征文链接
- 团队合影

## 16. 当前进度与验证方法

M1 到 M12 已完成（M10 节点执行、M11 录制待做），136 个 pytest 用例通过。一条命令跑通全流程：

```
cd sparkjury
uv run pytest
uv run sparkjury run --demo --run-id demo-1
uv run sparkjury serve --host 127.0.0.1 --port 9000
uv run python scripts/validate_skills.py
```

分步命令：

```
uv run sparkjury ingest --path data/samples/tau2_retail_sample.json --source tau2
uv run sparkjury ingest --path data/samples/otel_sample.json --source otel
uv run sparkjury stats
uv run sparkjury list --failed
uv run sparkjury show retail_task_002-t1
uv run sparkjury precheck
uv run sparkjury score
uv run sparkjury verdicts retail_task_002-t1
uv run sparkjury arbitrate
uv run sparkjury cluster --min-cluster-size 2
uv run sparkjury report
uv run sparkjury regress --before runs/sparkjury.db --after runs/sparkjury.db
```

上面这些预期不再靠人眼核对：`uv run python scripts/certificate.py` 会真跑一遍并逐条断言——测试总数、
validate_skills 显示 6/6 valid、六个技能封装都能真跑通、`run --demo` 七个阶段全部 ok、report 写出
三个卡片文件、regress 同库对比为 unchanged、cluster 的 badcase 数与簇数、arbitrate 的维度数、
precheck 的环境失败与进裁判条数、score 的裁决数与 verdict 数、stats 的 trace/任务数与 pass^1、pass^3。

这些数字唯一的真源是 `scripts/certificate.py` 里的 `PIPELINE` 常量，文档不再手抄一遍——手抄的那份
已经在 96 / 98 / 116 之间漂过。要看当前值就跑证书，它会打印每一条的实测值。

要人看界面和终端的两条，证书不覆盖：serve 后浏览器打开 http://127.0.0.1:9000/ 能看到 Cockpit；
`show retail_task_002-t1` 能看到 AI 两次错误调用 modify_user_address。

下一步：在 DGX 节点执行 deploy/README.md 的步骤，接真实裁判跑 τ²-bench，补 README 数字与截图，录视频。
