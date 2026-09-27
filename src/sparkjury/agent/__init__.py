"""Agent harness：让模型自己读技能说明书、自己调工具，把一条流水线跑完。

这一层补的是项目里原来缺的那块：裁判打分是流水线编排的，被评对象跑在 τ²-bench 自己的
框架里，而 SparkJury 自己的六个技能给谁调？给一个会看工具列表、会按需读说明书的模型。

分四层，各管一段，谁也不知道底下是谁：

    cli.py / iface.py   接口：命令行、一行文本、NDJSON 事件流、长驻 RPC
    perms.py            权限：只读放行、写操作审批或记账、红线永远拦
    spawn.py            子 agent：把一件独立的事派给另一个 run 去做
    runtime.py   装配 + 落盘：run 目录、events.jsonl、session.jsonl、usage.jsonl、manifest.json
    ops.py / store.py / compact.py / hooks.py   操作状态机、三个存储、历史压缩、钩子
    loop.py      循环：消息 → 模型 → 工具 → 结果，外加 steering / follow-up / abort
    ai.py        模型：四个本地端点 + 云端，一次调用拿到文本和工具调用
    tools.py     工具：六个技能按需加载与执行 + 三个只读工具 + task
    session.py   会话树：只追加的 JSONL，带 parent 指针，可分支、可回放

参照的是 pi（earendil-works/pi）的 harness 分层，三层都齐了：最小闭环（能跑能看能回放）、
中层（能恢复、能压缩、能挂钩子）、上层（四种接口、权限、子 agent）。刻意没做的写在
`docs/AGENT_HARNESS.md` 末尾：token 级流式、多进程锁、跨机器恢复、工具幂等键。
"""

from sparkjury.agent.ai import (
    NODE_ENDPOINTS,
    OpenAICompatProvider,
    Provider,
    ScriptedProvider,
    ToolCall,
    Turn,
    describe_endpoints,
    resolve_model,
    split_thinking,
    text_turn,
    tool_turn,
)
from sparkjury.agent.loop import (
    STOPPED_ABORTED,
    STOPPED_END_TURN,
    STOPPED_ERROR,
    STOPPED_MAX_TURNS,
    AgentLoop,
    LoopResult,
    default_system_prompt,
)
from sparkjury.agent.compact import (
    CompactionPlan,
    CompactionPolicy,
    Compactor,
    apply_compaction,
    deterministic_summary,
    model_summary,
    plan_compaction,
)
from sparkjury.agent.hooks import Hooks
from sparkjury.agent.iface import (
    RPC_OPS,
    EventStream,
    RpcSession,
    StreamWriter,
    event_payload,
    final_text,
    result_payload,
    run_with_events,
)
from sparkjury.agent.perms import (
    BUILTIN_READONLY,
    RED_LINES,
    Decision,
    PermissionMode,
    PermissionPolicy,
    Verdict,
)
from sparkjury.agent.spawn import CHILDREN_DIR, SubagentRecord, SubagentRunner, task_tool_spec
from sparkjury.agent.ops import (
    TERMINAL_STATUSES,
    Operation,
    OperationKind,
    OperationLog,
    OperationStatus,
)
from sparkjury.agent.runtime import AgentRun, AgentRuntime, load_manifest, new_run_id
from sparkjury.agent.session import Entry, SessionTree
from sparkjury.agent.store import (
    Ledger,
    RunStore,
    StoreError,
    StorePaths,
    Transaction,
    ValuesStore,
    atomic_write_json,
    read_jsonl,
)
from sparkjury.agent.tools import (
    OfflineSkillExecutor,
    SkillInfo,
    SkillRun,
    SubprocessSkillExecutor,
    ToolRegistry,
    ToolSpec,
    load_skill_tools,
    load_skills,
    parse_skill_md,
)

__all__ = [
    "NODE_ENDPOINTS", "OpenAICompatProvider", "Provider", "ScriptedProvider", "ToolCall", "Turn",
    "describe_endpoints", "resolve_model", "split_thinking", "text_turn", "tool_turn",
    "STOPPED_ABORTED", "STOPPED_END_TURN", "STOPPED_ERROR", "STOPPED_MAX_TURNS",
    "AgentLoop", "LoopResult", "default_system_prompt",
    "AgentRun", "AgentRuntime", "new_run_id", "load_manifest",
    "Entry", "SessionTree",
    "CompactionPlan", "CompactionPolicy", "Compactor", "apply_compaction",
    "deterministic_summary", "model_summary", "plan_compaction",
    "Hooks",
    "RPC_OPS", "EventStream", "RpcSession", "StreamWriter", "event_payload", "final_text",
    "result_payload", "run_with_events",
    "BUILTIN_READONLY", "RED_LINES", "Decision", "PermissionMode", "PermissionPolicy", "Verdict",
    "CHILDREN_DIR", "SubagentRecord", "SubagentRunner", "task_tool_spec",
    "TERMINAL_STATUSES", "Operation", "OperationKind", "OperationLog", "OperationStatus",
    "Ledger", "RunStore", "StoreError", "StorePaths", "Transaction", "ValuesStore",
    "atomic_write_json", "read_jsonl",
    "OfflineSkillExecutor", "SkillInfo", "SkillRun", "SubprocessSkillExecutor", "ToolRegistry",
    "ToolSpec", "load_skill_tools", "load_skills", "parse_skill_md",
]
