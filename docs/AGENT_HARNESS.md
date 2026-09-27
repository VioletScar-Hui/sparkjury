# Agent harness：让模型自己把 SparkJury 用起来

这份文档讲 M13 那一层。它解决的问题很具体：SparkJury 有六个技能、一条七段流水线、四个本地端点，
但在此之前，**模型只会被当成「一问一答的打分器」用**——裁判客户端 `judges/client.py` 发一条消息、
收一段文本，没有工具、没有多轮、没有循环。技能是给人用的命令，被评 Agent 跑在 τ²-bench 自己的框架里。
也就是说：SparkJury 自己是个 Agent 系统，但它没有让自己家的模型动过手。

M13 补的就是这一层：一个能挂工具、能多轮、能中断、能回放的 agent harness，模型在哪一层用都一样——
四个本地端点、云端 StepFun、以后换成别的模型，只换一个 `--model` 参数。

参照的是 pi（earendil-works/pi）的 harness 分层，分三步交付：先做**最小闭环**（能跑、能看、能回放），
再把**中层**补上（能恢复、能压缩、能挂钩子），最后是**上层**（三种接口、权限与留痕、子 agent）。

## 一句话

模型读工具清单 → 自己决定调哪个技能 → 看结果再决定下一步 → 干完为止；全过程一行一行写进会话树，
看得见、能中断、能回放。

## 分层

```
   sparkjury agent run -p "……"          ← 人只说一句话，不说用哪个技能
        │
   ┌────▼────────────────────────────────────────────────────────────────────────┐
   │ 上层：接口、权限、子 agent                                                    │
   │   iface.py   三种接口：--print（只吐最后那段）/ --events（NDJSON）/ agent rpc │
   │   perms.py   plan 只读 / safe 审批或记账 / yolo 全放行；红线三种模式都拦      │
   │   spawn.py   task 工具：把一件独立的事派给另一个 run，默认只深一层            │
   ├──────────────────────────────────────────────────────────────────────────────┤
   │ cli.py   run / tools / policy / rpc / replay / endpoints / ops / resume / compact │
   ├──────────────────────────────────────────────────────────────────────────────┤
   │ 中层（可恢复）                                                                │
   │   runtime.py  装配与落盘：run 目录、事件流、用量、操作日志、manifest          │
   │   ops.py      操作状态机：run / compaction / navigation                       │
   │   store.py    三个存储 + 原子事务：会话树 / values / 用量账本                 │
   │   compact.py  历史压缩：摘要 entry 顶替更老的消息                              │
   ├──────────────────────────────────────────────────────────────────────────────┤
   │ loop.py       循环：消息 → 模型 → 工具 → 结果                                 │
   │               steering / follow-up / abort；hooks 四个挂点                     │
   ├──────────────────────────────┬───────────────────────────────────────────────┤
   │ ai.py 统一模型入口            │ tools.py 工具注册表                           │
   │ 8001/8002/8003/8004 + 云端    │ 六个技能 + 三个只读工具 + task                │
   ├──────────────────────────────┴───────────────────────────────────────────────┤
   │ session.py    会话树：只追加的 JSONL，每条带 parent 指针                       │
   └──────────────────────────────────────────────────────────────────────────────┘
```

画成一张图是五层，但真正决定行为的是两条规矩：

- **技能正文按需加载**。system prompt 里只有技能的名字和一句话描述，模型要看细节得自己调
  `load_skill`。六个技能的 SKILL.md 全塞进提示词要两千多字符，而一次 run 通常只用其中两三个。
- **工具失败是消息，不是崩溃**。技能名写错、文件不存在、子进程超时，都变成模型能看到的一句话，
  它自己决定纠正还是换路；同时记一笔失败，收尾时出现在 `manifest.json` 的 `degradations` 里。

## 一次 run 里发生了什么

以「把这批轨迹过一遍体检」为例：

1. harness 写 system prompt：技能清单（只有描述）、工具清单、干活规矩，然后开一条 `system` 记录。
2. 模型这一轮回的是工具调用而不是文本 → harness 记一条 `tool_call`，执行 `load_skill`，把说明书
   记成 `tool_result` 回灌给模型。
3. 模型接着调 `run_skill` 跑清洗、打分、出卡片，每个技能的 stdout 尾部和退出码都回灌。
4. 某一轮模型不再要求调工具，只回一段话 → 这就是这次 run 的结论，run 正常结束（`end_turn`）。

每一步都同时写进两处：会话树（给回放）和事件流 `events.jsonl`（给看板）。事件用的是流水线那套
`EventBus`，`stage` 固定写 `AGENT`，所以 M8 的看板不需要改就能看到 agent 在干什么。

## 三种打断

| 做法 | 什么时候进 | 用途 |
|---|---|---|
| `steer(text)` | 当前 assistant 回合之后、下一个请求之前 | 工具跑着的时候插话（「先别打分，数据源换了」） |
| `followup(text)` | 模型这一轮说完之后，当成新的一轮 | 追加要求（「把结论写进证据卡片」） |
| `abort()` | 立刻 | Ctrl-C 或看板上的停止按钮；已写下的记录一条不删 |

`abort` 之后 run 以 `aborted` 收尾，manifest 里记一条降级，会话里留一条 note 说明中断位置——
不是把这段历史抹掉当作没发生。

## 会话树

`session.jsonl` 一行一条，只追加：

```json
{"id":"e0003","parent":"e0002","kind":"tool_call","role":null,"ts":"…","data":{"calls":[…]}}
```

`kind` 六种：`message`（system/user/assistant 的文本）、`tool_call`、`tool_result`、`note`、
`branch`、`compaction`（压缩摘要）。当前停在哪条 entry 决定活动分支；`branch(from_id)` 把活动分支
切到更早那条，后面接在这条后面——原来的记录一条不动，也不需要复制文件。翻译成模型吃的消息是
`messages()` 的活：一轮里的多个工具调用合成一条 assistant 消息，工具结果一条对一个 `tool_call_id`。

崩溃时最后一行可能只写了一半：读的时候跳过它，但把行号记进 `torn_lines`——「悄悄丢掉」和
「本来就没有」是两回事。

回放：

```bash
uv run sparkjury agent replay runs/agent-20260927-101530-1a2b/session.jsonl
uv run sparkjury agent replay runs/agent-…/session.jsonl --limit 5
```

## 命令

```bash
uv run sparkjury agent endpoints                    # 六个端点的短名
uv run sparkjury agent tools                        # 注册表：模型能调什么
uv run sparkjury agent run --demo                   # 离线跑一通，两秒，不联网
uv run sparkjury agent run -p "跑一遍体检" --model subject
uv run sparkjury agent run -p "…" --model http://127.0.0.1:8001/v1#Qwen/Qwen3-30B-A3B-Instruct-2507-FP8
uv run sparkjury agent replay <session.jsonl>
uv run sparkjury agent ops runs/agent-20260927-101530-1a2b      # 操作日志与现场，要不要恢复
uv run sparkjury agent resume runs/agent-20260927-101530-1a2b   # 从中断处接着跑
uv run sparkjury agent compact runs/x/session.jsonl --dry-run   # 压缩试算（默认就不落盘）
uv run sparkjury agent policy                                   # 三种权限模式下每个工具怎么判
uv run sparkjury agent run -p "跑一遍体检" --print               # 只吐最后那段回答，给脚本用
uv run sparkjury agent run -p "跑一遍体检" --events              # 每行一条 JSON 事件（NDJSON）
uv run sparkjury agent rpc                                      # 常驻：stdin 一行一条 JSON 命令
```

跑完落在 `runs/<run_id>/`：`session.jsonl`（会话树）、`events.jsonl`（事件流）、`usage.jsonl`
（每轮的 token 与延迟）、`manifest.json`（收尾账：`status` / `stopped` / `turns` / `tool_calls` /
`usage` / `skills` / `degradations`，口径跟流水线的 manifest 对齐）。

## 中层：可恢复

最小闭环的循环只会往前跑，进程一没就说不清上次跑到哪。中层补的是这一件事，拆成五块：

**三个存储。** 只追加的会话树（`session.jsonl`）、可替换的 values（`values.json`）、只追加的用量账本
（`usage.jsonl`）。values 是快照，会话树是真源：崩溃后 values 最多落后一步，恢复时以会话树为准。
整份替换走「写临时文件 → fsync → `os.replace`」，所以读的人要么看到旧的一整份、要么看到新的一整份，
不存在写了一半的 JSON。一次提交里要写多个文件时用 `store.transaction()`：体里只暂存，退出时先追加、
后替换，中途抛异常就一条都不写。

**操作状态机。** 一次 run 是一条操作，`ops.jsonl` 里一行一次状态变化（accept / running / completed /
failed / aborted），当前状态是折叠出来的，历史一行不删。这样「上次到底是跑完了还是被 kill 了」
有据可查，而不是靠猜。

**中断恢复。** `resume` 从断点接着跑，判断全部来自会话树：

| 断在哪 | 恢复时怎么做 |
|---|---|
| 一批工具里有结果、有没跑的 | 有结果的重放（不重跑，只记一条「重放」标记），没跑的执行 |
| 只有「开始执行」标记、没有结果 | **不自动重跑**：重跑可能造成第二次副作用，记一条「完成情况未知」交给模型决定 |
| 停在一个工具结果之后 | 继续问模型（它还欠一个回答） |
| 停在 assistant 的回答之后 | 不再问模型，直接以 `end_turn` 收尾 |

重放的判断只认「开跑那一刻」的日志快照。工具调用 id 不保证跨轮唯一（有的端点每轮都从 `call_1`
开始编），边跑边看会把本轮刚写下的结果当成上一轮的重放——这个坑在 demo 上真的踩到过一次。

**历史压缩。** 触发看上一轮端点回的 `prompt_tokens`（真实值），不是估的字符数；端点上不回 usage
时才退回字符估算。压缩时插一条 `compaction` entry，说清「我替掉了到哪一条为止」，切分在读的时候做——
entry 是只追加的，谁也回不去改历史。摘要优先让模型自己写，叫不动就回落确定性摘要（把每条压成一行），
回落会记一笔降级。

**钩子。** 四个挂点：请求前（可改消息）、工具执行前（返回 `False` 或一句理由就拦下这次调用）、
工具执行后、每轮结束。钩子里抛异常不吞——那是调用方自己的代码，出错就该看见。

四个原语对应 pi 的说法：`accept`（建操作）、`drive`（推进）、`request_abort`（中断）、
`inspect`（看现场，`sparkjury agent ops` 打的就是它）。

## 上层：接口、权限、子 agent

中层跑通之后，剩下三件「一个能给人用的 harness 该有、但这个仓库还没有」的事。

### 三种接口

一次 run 的产物是给人和给机器看的两种东西，所以接口分成三条：

| 接口 | 命令 | 吐什么 |
|---|---|---|
| print | `agent run -p "…" --print` | 只有最后那段回答，一行不多。`$(… --print)` 能直接接进脚本 |
| 事件流 | `agent run -p "…" --events` | 每冒一条事件写一行 JSON（NDJSON），结束补一行 `result` |
| RPC | `agent rpc` | 常驻进程：stdin 一行一条命令，stdout 一行一条事件与回执 |

RPC 认识的命令是 `prompt` / `steer` / `followup` / `abort` / `inspect` / `policy` / `ping` /
`shutdown`。跟 `--print` 的区别只有一个但很关键：**跑 run 的活放在工作线程里，所以跑着的时候
命令还读得到**——插话（steer）和喊停（abort）都是在别人干活的时候才有意义的东西。

写 stdout 的地方都过一把锁：RPC 里边事件来自工作线程、回执来自读命令的线程，两个线程同时写一行会写岔。

两条如实说的语义：

- **abort 是协作式的**。它把标志立起来，但拦不住已经发出去、正在等回包的那次模型调用。
  所以 `shutdown` 的收工顺序是「先等这一轮跑完（默认 30 秒），等不到再喊停」；喊停之后操作日志里
  会立刻有一条 `aborted`，但那个线程要等模型回包才真的停下来。
- **跑着的时候来的 prompt 当插话处理**，不会另起一轮。同一个会话文件被两条线同时写，坏起来很难查。

### 权限与留痕

pi 的 harness 里没有权限系统，它的理由很直接：agent 跑在容器里，越界由沙箱兜。这里不一样——
SparkJury 的 agent 跑在队里大家共用的那台 DGX Spark 上，同一个 ssh 账号、同一棵工作树。

| 模式 | 只读工具 | 会改东西的工具 |
|---|---|---|
| `plan` | 放行 | 一律拒绝，理由回给模型，让它把打算写成步骤 |
| `safe`（默认） | 放行 | 有人在场就问一句；没人接手就放行，但记成「无人值守放行」 |
| `yolo` | 放行 | 放行，仍然逐条记账 |

三种模式都拦节点手册里的红线（reboot / shutdown / poweroff / 探测内网 / mkfs / dd / 删根目录），
红线没有开关：那是手册定的规则，不是这次 run 的偏好。红线检查会把参数摊平了找，所以藏在列表和
字典里的也拦得住。`agent policy` 把三种模式下每个工具的处置和全部红线一次列出来。

**谁算只读不靠猜**：工具自己声明（`ToolSpec.readonly`），权限层从注册表读。没声明的一律按
「会改东西」处理——宁可多问一句，也不要自作主张。

`safe` 在没有批准通道时选择放行而不是拒绝，是因为这个 harness 的正常用法就是无人值守地跑
（节点上那些 `run_*.sh` 就是）。拒绝会把无人值守整个堵死；而放行时每一次都记下来，收尾在
`manifest.json` 的 `permissions` 里能数清楚：检查了几次、几次没人点头就做了、几次被拦。

### 子 agent

`task` 是挂在同一张工具表里的普通工具，模型看到的调用语法跟 `run_skill` 一模一样：
`{"prompt": "……"}`，可选一个 `model` 换端点。派出去的活落在父 run 目录的 `children/<子 run>/` 下，
会话树、事件流、用量账本、manifest 各一套——子 agent 干了什么，翻它自己的目录就能看清。

四条规矩：

- **默认只深一层**。子 agent 的工具表里没有 `task`，免得两个 agent 互相派活派到天荒地老。
  `--subagent-depth 2`（或 `subagent_max_depth`）才给更深一层，用的是「剩余深度」，父子口径一致。
- **权限沿用父的**。子 agent 不能借「我是个新 run」把拒绝名单甩掉：同一个 `PermissionPolicy`
  实例绑给两边，拒签记录落在同一本账上。
- **失败不美化**。子 agent 没正常结束（报错、被中断、轮数用尽）时，父 agent 拿到的是 `ToolError`，
  会进父 manifest 的降级项；模型看到的是「它没干完，原因是这个」，不是一段假装成功的总结。
- **子 run 的降级项抄进父 manifest**。一次派活里的失败不该因为「发生在另一个 run 里」就看不见。

一个已知的坑写在这里，免得下次有人踩：脚本替身（`--demo` 与测试用的 `ScriptedProvider`）只有一条
固定时间线，父子共用会把父的下一轮吃掉。所以这种 provider 下派活会直接报错，要求显式给
`subagent_factory`——宁可报错，也不要悄悄串味。

还有一个是节点上真跑才发现的，写下来当教训：`task` 的 `model` 参数第一版只写了句「短名见
`agent endpoints`」，结果 Qwen3-8B 照着仓库里的 `.claude`、`.codex` 这些目录猜名字，编出
`claude`、`codex`、`default`、`sparkjury` 五个不存在的端点，一个个撞回 404，把 12 轮全烧在
重试上。现在这个参数是 enum（只有真存在的短名），名单外的名字在起子 run 之前就拒掉，并且明确
告诉模型「不要换名字重试」。**提示词里的「见某处」，模型是不会去见的。**

### 顺手修的两处循环语义

写这一层的时候发现循环里有两个「看起来跑完了、其实没跑完」的地方，一起修了：

- **abort 在模型回答那一轮里到达**：以前会记成 `end_turn`，看起来像自然跑完。现在如实记成
  `aborted`，回答本身保留（已经落盘的不删），加一条说明中断位置的 note。
- **steering 在最后那一轮回答期间到达**：以前会随 `end_turn` 一起丢掉。现在循环会再跑一轮，
  把它冲成一条 user 消息——「插话在当前回合之后生效」这句话才算真的成立。

### 模型把工具调用写成了正文怎么办

同一个 harness，换一个模型就有可能哑火——这件事在节点上是真发生的。三个本地端点的实测：

| 端点 | 模型 | 说要调工具时发生了什么 |
|---|---|---|
| 8004 `subject` | Qwen3-8B | 正常返回结构化的 tool_calls，`load_skill` 真的跑起来了 |
| 8001 `judge-a` | Qwen3-30B-A3B-Instruct-2507-FP8 | 同上 |
| 8002 `judge-b` | Nemotron-3.5-Lightning-30B-A3B-NVFP4 | 把调用写成了正文里的 `<function=load_skill><parameter=name>…`，端点没解析出来，harness 这边看到的是「自言自语说要去调工具，然后就没有然后了」 |

根子在服务端：一个 vLLM 端点只能配一种 `--tool-call-parser`（`start_judges.sh` 里是 `hermes`），
配不上的模型格式就落回 content。以前这一轮会以一句空回答收尾，看着像模型不行，其实是没人接它的话。

所以 `ai.py` 加了一层兜底 `salvage_tool_calls()`：服务端没给出结构化调用、而正文里出现了
写全了的调用块时，把它捞出来当成真的调用执行。三条自我约束，宁可漏捞也不硬捞——

- 形状必须成对写全（`<tool_call>…</tool_call>` 里套 `<function=名字>` 加 `<parameter=键>值</parameter>`，
  或者 Hermes 那种 JSON），半截标签当没看见；
- **名字必须在本次真的声明过的工具表里**，否则原样留在正文里。文档和说明书里到处是
  `<function=…>` 的例子，不设这道门，抄一段文档就能变成一次真执行；
- 捞出来的块从正文里去掉，同一句话不会在会话里出现两遍。

留痕照旧：会话里加一条 `note{phase:tool_salvage}`、事件流发一条 warning、manifest 多一栏
`salvaged_tool_calls`，并在 `degradations` 里写明「N 个工具调用是从正文里捞回来的（端点没配对应的
tool-call parser）」。这不是悄悄修好——读 manifest 的人应该能看到这个端点的工具调用没走正常通道。

## 跟 pi 的对照

| pi | 这里 | 为什么这么办 |
|---|---|---|
| `pi-ai` 多 provider 统一层 | `ai.py` | 四个本地端点 + 云端共用一套 `complete()`，换模型只换参数 |
| `pi-agent-core` 的 agent loop | `loop.py` | 同样的一圈，外加 steering / follow-up / abort 三个打断口 |
| JSONL 会话文件 + parent 指针 | `session.py` | 中断后能接着看，分支不用复制文件 |
| 技能描述进提示词、正文按需加载 | `tools.py` 的 `load_skill` | 六个技能全塞提示词太贵，一次 run 用不到那么多 |
| `pi-durable` 的三个存储 + 原子事务 | `store.py` | 会话树 / values / 用量账本，整份替换原子，一次提交要么全在要么全不在 |
| `pi-durable` 的操作状态机与 durable restart | `ops.py` `runtime.py` | 一次 run 一条操作，`resume` 接着跑；已有结果的工具重放而不重跑 |
| pi 的 compaction | `compact.py` | 摘要 entry 顶替更老的消息，原文一条不删 |
| pi 的 hooks 与被动事件 | `hooks.py` | 四个挂点；被动事件仍走 M7 的 `EventBus` |
| 扩展注册工具（`registerTool`） | `ToolRegistry.register` | 加工具就是加一条 `ToolSpec`；`task` 就是这么挂上去的 |
| 五种接口（interactive / print / JSON / RPC / SDK） | `iface.py` | 对上三种：`--print`、`--events`（NDJSON）、`agent rpc`；SDK 那种在进程内调的形状对这个仓库没用 |
| 子 agent（`task` 一类工具） | `spawn.py` | 子 run 有独立目录与会话，默认只深一层，权限沿用父的 |
| 权限靠容器和沙箱，harness 里没有权限系统 | `perms.py` | 节点是共享账号、手册有硬红线，所以这里补了处置与留痕：不是沙箱，是「谁点的头、谁被拦、几次没人点头」都有账 |

## 刻意没做的

- **token 级流式**：vLLM 的增量要处理工具调用分片，这一版按回合返回；事件流是回合级的，
  看板上「第 N 轮、调了哪个工具」照样实时。
- **多进程锁**：同一个 run 目录被两个人同时 `resume`，没有租约或锁来互斥。单机一个人接着跑
  上一个人的 run 够用，并发操作同一份日志不管。
- **跨机器恢复**：状态都在本机文件里，没有远端操作队列。
- **工具调用的幂等键**：重放靠日志里的结果，不靠工具自己声明幂等。不可重复的工具（真的下单、
  真的扣费）不该挂进这个 harness，或者得自己加一道确认钩子。
- **RPC 的鉴权**：`agent rpc` 走本机 stdin/stdout，等于把「谁能连上这台机器」当成边界，
  没有额外的 token。要给别的机器连，得自己套一层带鉴权的壳（像 API 那层那样）。
- **权限的强制力**：`perms.py` 拦的是模型走工具表的那条路。模型一旦拿到能执行任意命令的工具
  （比如自己接一个 shell 工具），这层就只是个记账本——真正的边界仍然是不给它这种工具。

## 验证

```bash
uv run pytest tests/test_m13_agent.py tests/test_m13_durable.py \
  tests/test_m13_toplayer.py tests/test_m13_salvage.py -q
uv run sparkjury agent run --demo            # 端到端：读说明书 → 清洗 → 打分 → 出卡片
uv run sparkjury agent ops <run_dir>         # 操作日志与现场
uv run sparkjury agent policy                # 三种模式下每个工具怎么判
```

中层那 36 个用例盯的是「悄悄错」：日志写坏一半当成没写、崩溃后把工具又跑了一遍、压缩之后模型
再也看不到早期结论、被 hook 拦下的调用当成执行成功。断言全部落在可观察的事实上——文件里剩什么、
下次启动时会不会重跑、模型实际收到哪些消息。

测试盯的都是可观察的结果——发给模型的请求、会话里的记录、事件流、manifest 里的降级项——
不盯内部实现：技能 frontmatter 解析、注册表给的 schema 是不是合法工具定义、提示词里有没有
混进技能正文、工具结果有没有回灌、steering 有没有在下一个请求里出现、中断之后还会不会继续问模型、
manifest 有没有把失败写成成功。

节点上跑真模型（Qwen3-8B @ 8004）：

```bash
uv run --group ops python scripts/node.py check      # 先看节点在忙什么
uv run --group ops python scripts/node.py sync
ssh -p 6030 asus_gx10@61.172.235.130 'cd ~/sparkjury && \
  ~/.local/bin/uv run sparkjury agent run -p "把这批轨迹过一遍体检" --model subject'
```
