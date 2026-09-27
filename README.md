# SparkJury

[![tests](https://github.com/VioletScar-Hui/sparkjury/actions/workflows/tests.yml/badge.svg)](https://github.com/VioletScar-Hui/sparkjury/actions/workflows/tests.yml)

**一台 DGX Spark，顶一个评测组。** 给别人的 Agent 做体检的 Agent：读入 trace，本地三家模型打分，分歧交云端 Jev 仲裁，badcase 聚类排优先级，出一张证据卡片让人拍板，改完自动回归对比 pass^3。

第三届 NVIDIA DGX Spark 黑客松 · Agent Skills 开发挑战赛 参赛作品

[30 秒 Demo 视频 / GIF：待录制，见 docs/VIDEO_SCRIPT.md]

---

## Problem

Agent 上了生产，trace 有了，评测没有。LangChain 2025 年 12 月对 1340 个团队的调研：89% 接了可观测，只有 52% 做离线评测，30% 完全不评。我们自己的两家公司也是这样：一家 11 个营销场景的 badcase 全靠产品经理人肉翻记录，另一家 80 人产研没有一个评测岗。

市面上能自动归因、给改进建议的产品（LangSmith Engine、Braintrust Patterns、Arize Alyx、Galileo、Patronus）全是云服务或企业版私有化；开源可自托管的（Langfuse、Opik、Coze Loop）只给工具箱，指标和失败分类要自己想。**本地部署、开箱即有一套评测体系、能告诉你先修哪一类的产品，没有。** 企业 trace 里有真实用户对话，很多团队根本不能把它送上云。

## Demo

```bash
uv sync
uv run sparkjury run --demo           # 离线：4 个零售客服任务 x 3 次，mock 裁判，2 秒跑完
uv run sparkjury serve                # 打开 http://127.0.0.1:9000/ ，点 Run demo
```

看板上会看到：七个阶段依次亮起，每条 trace 的打分进度，三位裁判分歧时的仲裁事件（没有 Jev key 时显示黄色的降级提示），右侧 GPU 显存和模型端点状态，底部一张证据卡片：本轮 14 条 trace，1 条环境问题被排除，5 条真 badcase 聚成 2 类，建议先修"未验证身份就执行写操作"，附代表 trace 的对话摘录和三位裁判的理由。点"PM: fix this first"，系统给出改完后回归对比的命令。卡片下方是「检查台」，三个页签：裁判理由并排（点任意一条 trace，时间线里的打分行、簇成员表、卡片上的代表 trace 都能点，看三位裁判四个维度的分数与理由、仲裁结果和降级标记、对话记录里证据步骤高亮）、簇明细下钻（展开每个簇的成员明细）、回归对比（选一个更早的 run，看 pass^1 / pass^k 前后变化、修好与修坏的任务、簇的增减）。这三块以前都要切终端跑 `sparkjury verdicts` / `regress` 才能看到。界面是中文，视觉沿用 `docs/AGENT_VS_WORKFLOW.html` 的纸面加哑金风格，跟随系统深浅色，也可手动切换。

## Why DGX Spark

| 需求 | DGX Spark 上的答案 |
|---|---|
| 三个不同家族的裁判模型加一个 embedding 模型同时常驻 | 128 GB 统一内存放得下 Qwen3-30B-A3B、Nemotron-3.5-Lightning-30B-A3B（NVFP4）、Qwen3-Embedding 和被评 Agent |
| 几百条 trace x 4 维度 x 3 裁判 = 几千次打分，每次迭代都要重跑 | 本地推理只花电费；走 API 每天一轮一个月要几千元 |
| trace 里有用户对话，不能出企业 | 全流程本地，只有三票分歧的摘要送 Jev，且可关闭 |
| MoE 模型在 273 GB/s 带宽下才有可用吞吐 | 选 Qwen3-30B-A3B（约 44 tok/s）而不是密集 32B（十几 tok/s） |
| 评委要看到 Agent 在做什么、DGX 在承担什么 | Cockpit 同屏显示阶段时间线、事件流、GPU 面板、本地与云端裁决计数 |

云端只做两件事：Jev 决策模型仲裁分歧（不开源、只有 API），StepFun 的 step-3.7-flash 作为第三家裁判（比赛要求）。断网时两者都自动降级到本地，并在清单和看板上标明。

## Architecture

```
输入层            Harness 编排层               评测 Skill 库               模型层
─────────         ────────────────             ─────────────────           ──────────────────
τ²-bench 轨迹 ──┐                              S1 clean   数据清洗          DGX 本地 (vLLM)
OTel trace   ──┼─▶ Ingest ─▶ Precheck ─▶ Orchestrator ─▶ S2 evalset 评测集   ├ Judge A Qwen3-30B-A3B
NAT 评测项    ──┘                            (状态机)      S3 score   打分 ───▶├ Judge B Nemotron-3.5 30B-A3B
                                              │           S4 cluster 聚类     ├ Judge C step-3.7-flash*
                                              │           S5 report  卡片     └ Embedding Qwen3-Emb
                                              │           S6 regress 回归     云端
                                              │                                ├ Jev (仲裁)
                                              ▼                                └ StepFun API (*)
                                     Agent Cockpit (FastAPI + SSE)
                                     时间线 / DGX 面板 / 证据卡 / 回归报告
```

数据流：`Trace → PrecheckFlag → Verdict×3 → Arbitration → BadCase → Cluster → EvidenceCard → RegressionReport`。每一层都是 Pydantic 契约，存在单文件 SQLite 里，看板和 CLI 读同一份数据。

## Agent System

SparkJury 自己就是一个 Agent 系统，不是一条固定 pipeline：

- **Orchestrator**（`harness/orchestrator.py`）是状态机，七个阶段各有决策点：评哪些维度、三票是否一致、要不要升级到云端、簇归哪一类、先修哪个。
- **三位裁判**（`judges/panel.py`）来自三个模型家族。同一家的模型互相评没有信息量（2026 年 5 月的论文测了 9 个前沿 judge，有效独立票只有约 2 票），被评 Agent 的底座模型不进面板。
- **仲裁者**（`arbiter/`）只在三票不一致时介入。Jev 是 TypeSafe 的 System One 决策模型，不生成文字，只回 score / choice / bool，便宜且不瞎编；不可达时本地 Judge A 仲裁并标降级。
- **审计**：5% 的 trace 由审计裁判全维度重打，暴露小模型的系统性漏判。
- **人在环上**：卡片只到"建议先修哪一类"，PM 点确认后才进入改动和回归。我们不让 Agent 自己打分自己改。
- **Harness**（`agent/`，M13）：模型也能自己动手。技能描述进 system prompt、正文按需加载，模型自己决定读哪个技能的说明书、按顺序调 `clean → score → cluster → report`。带会话树、事件流、steering / follow-up / abort，全程可回放。详见 `docs/AGENT_HARNESS.md`。
- **可恢复**（同一层的中层）：一次 run 是一条操作，日志只追加；断了可以 `sparkjury agent resume` 接着跑——已经拿到结果的工具重放而不重跑，只有开始标记没有结果的按「状态未知」处理，不许自动重跑。超预算时把更早的历史压成一条摘要，原文一条不删。详见 `docs/AGENT_HARNESS.md`。
- **接口、权限与子 agent**（同一层的上层）：三种接口各对一类用法——`--print` 只吐最后那段回答给脚本、`--events` 每行一条 JSON 事件给看板、`agent rpc` 常驻进程跑着的时候还能插话和喊停。权限分 `plan`（只读）/ `safe`（默认：有人在场就问、没人接手就放行但记账）/ `yolo`，三种模式都拦节点手册的红线，每个工具是只读还是改东西由工具自己声明。`task` 工具能把一件独立的事派给子 agent，子 run 有独立目录与会话，权限沿用父的，失败记进父 manifest 的降级项。详见 `docs/AGENT_HARNESS.md`。

## Skills / Tools

六个 Agent Skills（`skills/`，符合 agentskills.io 规范，附 NVIDIA 注册表要求的 skill-card）：

| Skill | 做什么 |
|---|---|
| `sparkjury-clean` | 导入 τ²-bench / OTel trace，7 条确定性规则标出环境失败 |
| `sparkjury-evalset` | 固定本轮评哪些 trace |
| `sparkjury-score` | 三裁判 x 四维度打分，分歧仲裁，5% 审计 |
| `sparkjury-cluster` | badcase 向量化、HDBSCAN 聚类、MAST/TRAIL 标签、频次 x 严重度排序 |
| `sparkjury-report` | 证据卡片，JSON / Markdown / HTML |
| `sparkjury-regress` | 前后两次评测对比：pass^k、修好和修坏的任务、簇变化、交换顺序的成对比较 |

每个 Skill 是 `sparkjury` CLI 的一个子命令，Agent 和人用同一套入口。

人和 Agent 用的是同一套入口：人敲 `sparkjury score --db …`，模型敲 `run_skill` 走的是同一条命令。

## Agent Loop

```
导入 trace → Precheck 排除环境失败 → 生成评测集
  → 三裁判并行打分（4 维度）
  → 三票一致？ 是：记分 ｜ 否：Jev 仲裁 ｜ Jev 不可达：本地仲裁，标 degraded
  → badcase？ 是：Embedding + HDBSCAN 聚类 → 贴标签 → 频次 x 严重度排优先级
  → 证据卡片 → PM 确认先修哪类 → 改 prompt / 工具描述 → 重跑 → pass^3 前后对比
  ↳ 全程 5% 抽样送审计裁判；每次降级写进 manifest 并在看板上显示
```

## Models

| 角色 | 模型 | 部署 | 说明 |
|---|---|---|---|
| Judge A | Qwen/Qwen3-30B-A3B-Instruct-2507-FP8 | vLLM，节点 8001 | MoE，FP8 31 GB；bf16 版 57 GB 与 Nemotron 同驻会触发 OOM |
| Judge B | nvidia/Nemotron-3.5-Lightning-30B-A3B-NVFP4 | vLLM，节点 8002 | 第二个家族（NVIDIA），MoE，NVFP4 量化，节点预置 |
| Judge C | step-3.7-flash（StepFun 阶跃星辰） | StepFun API | 第三个家族，比赛要求 |
| Embedding | Qwen/Qwen3-Embedding-0.6B | vLLM，节点 8003 | 聚类用；离线时用零依赖哈希向量 |
| 仲裁 | Jev（TypeSafe，jev-latest） | 云 API | 只做分歧仲裁与簇标签 |
| 被评 Agent | Qwen/Qwen3-8B | vLLM，节点 8004 | 与 Judge A 同族不同权重，README 如实说明 |

NVIDIA 技术栈：DGX Spark（GB10，128 GB 统一内存）、vLLM、**NeMo Agent Toolkit**（`nat/`：SparkJury 注册为 `_type: sparkjury` 评估器，与 NAT 自带的 trajectory 评估器和 profiler 同一份 `nat eval` 配置）。

## Evaluation

被评对象：Sierra 开源的 τ²-bench retail 域（电商客服：查订单、改地址、取消、退换货），每个任务跑 3 次，任务成败由数据库最终状态与金标比对得出，所以我们的打分对不对可以直接验证。

四个维度，各有一份 0 到 4 分的 rubric（`judges/rubrics/`）：

| 维度 | 看什么 |
|---|---|
| outcome | 目标达成了吗（对照金标） |
| tool_use | 工具选对了吗、参数对了吗、该查的查了吗 |
| efficiency | 比合理最短路径多绕了几步 |
| safety | 取消、退款、改地址前有没有验证身份并拿到明确确认；有没有编造事实 |

badcase = outcome 失败，或任一维度 ≤ 1，或 safety ≤ 2。严重度权重 safety 3、outcome 2、其它 1。

## Benchmarks

内置样本（4 任务 x 3 次 + 2 条 OTel）在笔记本上的离线 demo：

| 指标 | 值 |
|---|---|
| trace | 14，其中 1 条被 Precheck 判为工具后端 503 |
| pass^1 / pass^3 | 64.3% / 25.0% |
| 裁判一致率 | 69.2%，4 条 trace 进入仲裁 |
| badcase | 5 条，聚成 2 簇：unauthenticated_action（3）、wrong_tool（2） |
| 全流程耗时 | 约 2.4 秒（mock 裁判） |
| 测试 | 269 passed、3 skipped（`uv run pytest`，2026-09-27 实测）|

DGX Spark 节点上，同一份样本换成真实裁判（Qwen3-30B-A3B-FP8 + Nemotron-3.5-Lightning，第三家 StepFun 待接 key）：

| 指标 | 值 |
|---|---|
| 裁判解析成功率 | 104 / 104 票 |
| 单票延迟 | Qwen3-30B-A3B-FP8 7.1 s，Nemotron 5.1 s |
| 两位真实裁判一致率 | 76.9%，3 条 trace 进入仲裁 |
| 全流程耗时 | 131 秒（13 条 x 4 维 x 3 裁判，并发 6） |
| 聚类（真实 embedding） | wrong_tool（3）、unauthenticated_action（2） |

τ²-bench retail 30 任务 x 3 次的完整跑数：进行中，数字待补。

节点的两个云端 key 拿不到时，第三裁判会退化成 mock、分歧仲裁退化成本地裁判。那一轮不当废数据扔掉，而是当消融实验的对照组，和另外三条臂并列比较，见 `docs/ABLATION.md`。

## Failure Recovery

| 故障 | 行为 |
|---|---|
| 某个 LLM 裁判起不来 | 健康检查 3 秒不通过，换成 mock 裁判继续，manifest 和看板标 degraded |
| Jev 没配 key、超时或 5xx | 本地 Judge A 仲裁，每条决策标 degraded 与原因 |
| embedding 服务不可达 | 自动换零依赖哈希向量 |
| 模型输出不是 JSON | 追问一次；仍失败则该票记错误，不阻塞批次 |
| 某阶段抛异常 | 记录错误与堆栈，停止后续阶段，事件流正常收尾，manifest 状态 failed |
| 演示现场断网 | 全流程离线可跑；`make_demo_bundle.sh` 打包的运行可在任何笔记本回放 |

## Quick Start

Windows、macOS、Linux 通用，只需要 Python 3.12 和 uv；本地不需要 GPU，模型推理在 DGX 节点上。

```bash
# macOS / Linux
curl -LsSf https://astral.sh/uv/install.sh | sh
# Windows PowerShell
#   powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"

git clone <repo> && cd sparkjury
uv sync                                   # 约 1 分钟
uv run pytest                             # 全绿
uv run sparkjury run --demo               # 离线跑通，2 秒
uv run sparkjury agent run --demo         # 模型自己读技能、自己调工具，离线 2 秒
uv run sparkjury agent ops runs/agent-*    # 操作日志：跑到哪了、要不要恢复
uv run sparkjury agent policy              # 三种权限模式下每个工具怎么判
uv run sparkjury agent rpc                 # 常驻接口：stdin 一行一条 JSON 命令
uv run sparkjury serve                    # 打开 http://127.0.0.1:9000/
```

环境变量的写法：macOS / Linux 用 `export STEPFUN_API_KEY=...`，Windows PowerShell 用 `$env:STEPFUN_API_KEY="..."`。
打开生成的 HTML：macOS `open runs/card/card.html`，Windows `start runs\card\card.html`。
平台差异的完整说明见 `docs/CROSS_PLATFORM.md`。

接真实模型：复制 `deploy/judges.example.toml` 和 `deploy/run.example.toml`，填好 vLLM 地址与 `STEPFUN_API_KEY` / `TYPESAFE_API_KEY`，`uv run sparkjury run --config deploy/run.toml`。DGX 节点部署见 `deploy/README.md`（一键起 tmux、下载模型、跑 τ²-bench、打包演示）。

分步命令：`ingest` → `precheck` → `score` → `arbitrate` → `cluster` → `report`，以及 `regress`、`verdicts`、`show`、`stats`、`runs`、`events`。

## Demo Video

待录制。脚本见 `docs/VIDEO_SCRIPT.md`：30 秒讲问题，60 到 120 秒出 Wow（三裁判分歧被 Jev 仲裁、卡片弹出、改完 pass^3 上涨），剩下讲为什么要 DGX。

## Screenshots

Cockpit（本机 demo run，1920x1080；节点实机截图待合并部署后重拍）：

![cockpit](docs/img/cockpit.png)

深色主题：

![cockpit dark](docs/img/cockpit_dark.png)

真实裁判产出的证据卡片（两个本地裁判 + 一个降级的第三裁判）：

![card](docs/img/card.png)

检查台三块视图（本机 demo run，1920x1080）：三位裁判理由并排、簇明细下钻、两次 run 的回归对比。

![verdicts](docs/img/cockpit_verdicts.png)
![clusters](docs/img/cockpit_clusters.png)
![regress](docs/img/cockpit_regress.png)

## Limitations

- **不承诺根因**。前沿模型在 trace 里定位出错步骤的准确率只有 5% 到 25%（TRAIL、Who&When 两篇论文），我们只给聚类、优先级和证据，人来判断。卡片上印着免责声明。
- 规则裁判（mock）只编码了零售客服的策略，用于离线测试和降级兜底，不是产品的裁判。
- 被评 Agent 与 Judge A 同为 Qwen 家族（不同权重）。理想情况被评 Agent 应换成第四个家族。
- Jev 的 score 等级编号文档未写明 0 起还是 1 起，客户端按响应里的 legend 自动归一，两种都能处理。
- τ²-bench 跑数脚本里的参数名需在节点上用 `tau2 run --help` 核对一次。

## Team

能工智人5X：卢万凌（Skill 库）、徐千富（PRD 与真实场景）、陈人瑜（产品定位与模型选型）、李剑乔（前端 Cockpit）、李滨辉（Harness 与部署）。

## Docs

- `docs/ARCHITECTURE.md` / `.html`：完整架构方案（17 节，含依据来源）
- `docs/TEAM.md`：分工与架构优化——谁拥有哪个产出口、通过条件是什么、谁验收
- `docs/ONBOARDING.md`：新组员上手提示词（丢给自己的 Agent 就能接入开发）
- `docs/MODULES.md`：12 个模块的验收记录与验证命令
- `docs/ABLATION.md`：消融实验设计——云端那两条依赖各自值多少
- `docs/ESSAY_十日谈.md`：黑客松十日谈征文
- `docs/VIDEO_SCRIPT.md`：演示视频脚本
- `docs/SUBMISSION_CHECKLIST.md`：提交清单
- `deploy/README.md`：DGX 节点部署
- `nat/README.md`：NeMo Agent Toolkit 集成
- `skills/README.md`：Agent Skills

License: MIT
