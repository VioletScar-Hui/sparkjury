# Agent harness：让模型自己把 SparkJury 用起来

这份文档讲 M13 那一层。它解决的问题很具体：SparkJury 有六个技能、一条七段流水线、四个本地端点，
但在此之前，**模型只会被当成「一问一答的打分器」用**——裁判客户端 `judges/client.py` 发一条消息、
收一段文本，没有工具、没有多轮、没有循环。技能是给人用的命令，被评 Agent 跑在 τ²-bench 自己的框架里。
也就是说：SparkJury 自己是个 Agent 系统，但它没有让自己家的模型动过手。

M13 补的就是这一层：一个能挂工具、能多轮、能中断、能回放的 agent harness，模型在哪一层用都一样——
四个本地端点、云端 StepFun、以后换成别的模型，只换一个 `--model` 参数。

参照的是 pi（earendil-works/pi）的 harness 分层，分两步交付：先做**最小闭环**（能跑、能看、能回放），
再把**中层**补上（能恢复、能压缩、能挂钩子）。

## 一句话

模型读工具清单 → 自己决定调哪个技能 → 看结果再决定下一步 → 干完为止；全过程一行一行写进会话树，
看得见、能中断、能回放。

## 分层

```
   sparkjury agent run -p "……"          ← 人只说一句话，不说用哪个技能
        │
   ┌────▼───────────────────────────────────────────────────────────────┐
   │ cli.py     run / tools / replay / endpoints / ops / resume / compact│
   ├────────────────────────────────────────────────────────────────────┤
   │ 中层（可恢复）                                                      │
   │   runtime.py  装配与落盘：run 目录、事件流、用量、操作日志、manifest │
   │   ops.py      操作状态机：run / compaction / navigation              │
   │   store.py    三个存储 + 原子事务：会话树 / values / 用量账本        │
   │   compact.py  历史压缩：摘要 entry 顶替更老的消息                    │
   ├────────────────────────────────────────────────────────────────────┤
   │ loop.py       循环：消息 → 模型 → 工具 → 结果                       │
   │               steering / follow-up / abort；hooks 四个挂点           │
   ├──────────────────────────────┬─────────────────────────────────────┤
   │ ai.py 统一模型入口            │ tools.py 工具注册表                  │
   │ 8001/8002/8003/8004 + 云端    │ 六个技能 + 两个只读文件工具           │
   ├──────────────────────────────┴─────────────────────────────────────┤
   │ session.py    会话树：只追加的 JSONL，每条带 parent 指针             │
   └────────────────────────────────────────────────────────────────────┘
```

画成一张图是四层，但真正决定行为的是两条规矩：

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
| 扩展注册工具（`registerTool`） | `ToolRegistry.register` | 加工具就是加一条 `ToolSpec` |
| 权限靠容器和沙箱，harness 里没有权限系统 | 同上 | 节点上本来就是共享账号，红线靠 AGENTS.md 和只读文件工具兜 |

## 刻意没做的

- **token 级流式**：vLLM 的增量要处理工具调用分片，这一版按回合返回；事件流是回合级的，
  看板上「第 N 轮、调了哪个工具」照样实时。
- **多进程锁**：同一个 run 目录被两个人同时 `resume`，没有租约或锁来互斥。单机一个人接着跑
  上一个人的 run 够用，并发操作同一份日志不管。
- **跨机器恢复**：状态都在本机文件里，没有远端操作队列。
- **工具调用的幂等键**：重放靠日志里的结果，不靠工具自己声明幂等。不可重复的工具（真的下单、
  真的扣费）不该挂进这个 harness，或者得自己加一道确认钩子。

## 验证

```bash
uv run pytest tests/test_m13_agent.py tests/test_m13_durable.py -q   # 65 个用例，全离线
uv run sparkjury agent run --demo            # 端到端：读说明书 → 清洗 → 打分 → 出卡片
uv run sparkjury agent ops <run_dir>         # 操作日志与现场
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
