"""`sparkjury agent` 子命令：跑一次、看工具清单、回放一次会话。

命令都刻意做得薄——真正的逻辑在 runtime / loop / tools 里，命令行只负责组装参数和
把结果摆成人看得懂的样子。

    sparkjury agent tools          # 注册表里有什么：六个技能 + 三个只读工具 + task
    sparkjury agent policy         # 三种权限模式下每个工具怎么判
    sparkjury agent run --demo     # 离线跑一通（脚本模型 + 离线执行器），两秒完事
    sparkjury agent run -p "……" --model subject
    sparkjury agent run -p "……" --print      # 只把最后那段回答吐出来，方便接到脚本里
    sparkjury agent run -p "……" --events     # 每冒一条事件写一行 JSON（NDJSON）
    sparkjury agent rpc            # 常驻：stdin 一行一条 JSON 命令
    sparkjury agent replay runs/agent-*/session.jsonl
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from sparkjury.agent.ai import ScriptedProvider, describe_endpoints, resolve_model, text_turn, tool_turn
from sparkjury.agent.compact import CompactionPolicy, Compactor, deterministic_summary, plan_compaction
from sparkjury.agent.iface import RpcSession, StreamWriter, final_text, run_with_events
from sparkjury.agent.loop import STOPPED_ABORTED, LoopResult
from sparkjury.agent.perms import RED_LINES, PermissionMode, PermissionPolicy
from sparkjury.agent.runtime import AgentRuntime, load_manifest
from sparkjury.agent.session import SessionTree
from sparkjury.agent.spawn import task_tool_spec
from sparkjury.agent.tools import OfflineSkillExecutor, load_skill_tools

agent_app = typer.Typer(help="Agent harness: 让模型自己读技能、自己调工具。", no_args_is_help=True)
console = Console()


def print_json(payload) -> None:
    """`--json` 的输出必须能被 json.loads 直接吃下去：不折行、不当成 rich 标记。

    技能描述有三百多字符，走 Console.print_json 会被按终端宽度折行，下游解析当场炸。
    """
    console.print(json.dumps(payload, ensure_ascii=False), soft_wrap=True, markup=False, highlight=False)

DEMO_PROMPT = "把这批轨迹过一遍体检，然后告诉我最该先修什么。"
DEMO_DB = "runs/agent-demo/sparkjury.db"

DEMO_REPLIES = {
    "sparkjury-clean": "导入 14 条轨迹，预检挑出 1 条 503（环境问题，不算 Agent 的错）。",
    "sparkjury-score": "三裁判 4 个维度共 52 个判定，一致率 76.9%，3 条分歧已仲裁（本地兜底，降级）。",
    "sparkjury-report": "证据卡片已生成：5 条 badcase 聚成 2 簇，wrong_tool 排第一。",
}


def demo_provider() -> ScriptedProvider:
    """离线脚本：读说明书 → 清洗 → 打分 → 出卡片 → 一句话结论。不联网、不落地命令。"""
    return ScriptedProvider([
        tool_turn(("load_skill", {"name": "sparkjury-clean"}), text="先读一下清洗技能怎么调。"),
        tool_turn(("run_skill", {"name": "sparkjury-clean",
                                 "args": ["--db", DEMO_DB, "--path", "data/samples/tau2_retail_sample.json",
                                          "--source", "tau2"]})),
        tool_turn(("run_skill", {"name": "sparkjury-score", "args": ["--db", DEMO_DB, "--judges", "mock"]})),
        tool_turn(("run_skill", {"name": "sparkjury-report", "args": ["--db", DEMO_DB]})),
        text_turn("轨迹跑完了：14 条里 1 条是环境问题先摘掉；三裁判有 3 条分歧，本地兜底仲裁（降级）。"
                  "最该先修的是工具选择，退款流程里 5 次里错 3 次。"),
    ])


def build_policy(mode: str, *, ask: bool = False) -> PermissionPolicy:
    """默认 `safe`：只读直接放行，会改东西的有人在场就问一句，没人接手就放行并记账。"""
    approver = None
    if ask:
        def approver(_call, question: str) -> bool:  # type: ignore[misc]
            return typer.confirm(question, default=False)

    return PermissionPolicy(mode=mode, approver=approver)


def build_runtime(prompt: str | None, *, model: str | None, max_turns: int, runs_dir: str,
                  workdir: Path, demo: bool, session: Path | None,
                  compact_budget: int = 0, permissions: PermissionPolicy | None = None,
                  subagents: bool = True, subagent_depth: int = 1) -> AgentRuntime:
    executor = OfflineSkillExecutor(DEMO_REPLIES) if demo else None
    provider = demo_provider() if demo else _real_provider(model)
    return AgentRuntime(provider, runs_dir=runs_dir, workdir=workdir, executor=executor,
                        max_turns=max_turns, session_path=session,
                        # 离线 demo 没有真模型可问摘要，用确定性兜底，别白跑一轮
                        compaction=CompactionPolicy(max_prompt_tokens=compact_budget),
                        use_model_summary=not demo, permissions=permissions, subagents=subagents,
                        subagent_max_depth=subagent_depth)


def _real_provider(model: str | None):
    from sparkjury.agent.ai import OpenAICompatProvider

    spec = resolve_model(model)
    return OpenAICompatProvider(spec)


@agent_app.command("tools")
def tools_cmd(
    skills_root: Path | None = typer.Option(None, "--skills-root", help="技能目录，默认仓库里的 skills/"),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """列出注册表：每个工具叫什么、给模型看的一句描述、参数长什么样。"""
    registry, skills = load_skill_tools(skills_root, executor=OfflineSkillExecutor())
    if "task" not in registry.names():
        registry.register(task_tool_spec(lambda _args: "（这里只列清单，不真的跑）"))
    if as_json:
        print_json({"tools": registry.payload(),
                    "skills": [{"name": s.name, "description": s.description, "path": str(s.path)}
                               for s in skills]})
        return
    table = Table(title=f"工具注册表（{len(registry.names())} 个）", show_lines=False)
    table.add_column("工具", style="bold")
    table.add_column("来源")
    table.add_column("给模型看的描述", overflow="fold")
    for tool in registry.specs():
        table.add_row(tool.name, tool.source, tool.description)
    console.print(table)
    console.print(f"[dim]技能目录：{skills_root or '仓库 skills/'}｜"
                  f"system prompt 里只放这些技能的一句话描述，正文由 load_skill 按需取[/dim]")


@agent_app.command("policy")
def policy_cmd(
    skills_root: Path | None = typer.Option(None, "--skills-root", help="技能目录，默认仓库里的 skills/"),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """三种权限模式下每个工具会被怎么判——连同红线，一次看全。"""
    registry, _skills = load_skill_tools(skills_root, executor=OfflineSkillExecutor())
    if "task" not in registry.names():   # 真跑时是 runtime 注册的，这里补上，免得清单看着少一个
        registry.register(task_tool_spec(lambda _args: "（这里只列判定，不真的跑）"))
    names = registry.names()
    modes = [PermissionMode.PLAN, PermissionMode.SAFE, PermissionMode.YOLO]
    policies = {}
    for mode in modes:
        policy = PermissionPolicy(mode=mode)
        policy.bind(registry)
        policies[str(mode)] = dict(policy.preview(names))
    if as_json:
        print_json({"modes": {m: {n: {"allow": d.allow, "verdict": d.kind, "reason": d.reason}
                                  for n, d in policies[m].items()} for m in policies},
                    "default": str(PermissionMode.SAFE),
                    "redlines": [{"needle": needle, "why": why} for needle, why in RED_LINES]})
        return
    table = Table(title="权限判定（默认 safe）", show_lines=False)
    table.add_column("工具", style="bold")
    for mode in modes:
        table.add_column(str(mode))
    for name in names:
        cells = []
        for mode in modes:
            decision = policies[str(mode)][name]
            cells.append("[green]放行[/green]" if decision.allow else f"[yellow]{decision.reason[:14]}…[/yellow]")
        table.add_row(name, *cells)
    console.print(table)
    console.print("[dim]plan 只读不写；safe 有人在场就问一句、没人接手就放行但记账；"
                  "yolo 全放行。红线三种模式都拦，没有开关。[/dim]")
    console.print("红线：" + "、".join(f"{needle}（{why}）" for needle, why in RED_LINES))


@agent_app.command("rpc")
def rpc_cmd(
    model: str = typer.Option("subject", "--model", "-m"),
    max_turns: int = typer.Option(12, "--max-turns"),
    runs_dir: str = typer.Option("runs", "--runs-dir"),
    workdir: Path = typer.Option(Path("."), "--workdir"),
    demo: bool = typer.Option(False, "--demo", help="用脚本模型：喂什么命令都按固定剧本回，不联网"),
    permissions: str = typer.Option("safe", "--permissions"),
    ask: bool = typer.Option(False, "--ask"),
    no_subagents: bool = typer.Option(False, "--no-subagents"),
    no_events: bool = typer.Option(False, "--no-events", help="不推事件流，只回执（默认推）"),
) -> None:
    """常驻接口：stdin 一行一条 JSON 命令，stdout 一行一条 JSON 事件与回执。

    命令：prompt / steer / followup / abort / inspect / policy / ping / shutdown。
    跑 run 的活在别的线程里，所以跑着的时候插话（steer）和喊停（abort）都收得到。
    """
    policy = build_policy(permissions, ask=ask)
    runtime = build_runtime(None, model=model, max_turns=max_turns, runs_dir=runs_dir, workdir=workdir,
                            demo=demo, session=None, permissions=policy, subagents=not no_subagents)
    session = RpcSession(runtime, emit_events=not no_events, max_turns=max_turns)
    raise typer.Exit(code=session.serve())


@agent_app.command("run")
def run_cmd(
    prompt: str | None = typer.Option(None, "--prompt", "-p", help="要它干的事；--demo 时可以不给"),
    model: str = typer.Option("subject", "--model", "-m",
                              help="端点短名（judge-a/judge-b/subject/…）或 http://host:port/v1#模型名"),
    max_turns: int = typer.Option(12, "--max-turns", help="最多几轮 assistant 回合"),
    runs_dir: str = typer.Option("runs", "--runs-dir"),
    workdir: Path = typer.Option(Path("."), "--workdir", help="工作目录，文件工具只能在这里面活动"),
    demo: bool = typer.Option(False, "--demo", help="离线跑一通：脚本模型 + 离线执行器，不联网不落地命令"),
    session: Path | None = typer.Option(None, "--session", help="续用一份已有的 session.jsonl"),
    compact_budget: int = typer.Option(0, "--compact-budget",
                                        help="prompt 超过这么多 token 就把更早的历史压成摘要；0 表示不压"),
    permissions: str = typer.Option("safe", "--permissions",
                                    help="plan（只读）/ safe（默认，写操作有人就问、没人就记账）/ yolo（全放行）"),
    ask: bool = typer.Option(False, "--ask", help="safe 模式下把批准通道接上：每个会改东西的工具都问一句"),
    no_subagents: bool = typer.Option(False, "--no-subagents", help="不给这次 run 装 task（子 agent）工具"),
    subagent_depth: int = typer.Option(1, "--subagent-depth", help="子 agent 最多再往下派几层"),
    print_only: bool = typer.Option(False, "--print", help="只把最后那段回答写到 stdout，别的都不打"),
    events: bool = typer.Option(False, "--events", help="事件流按行输出 JSON（NDJSON），收尾补一行 result"),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """跑一次 agent run。结果落在 runs/<run_id>/，manifest.json 里能看降级项。"""
    if not prompt and not demo:
        console.print("[red]要么给 --prompt，要么加 --demo。[/red]")
        raise typer.Exit(code=2)
    if print_only and events:
        console.print("[red]--print 和 --events 只能选一个：一个只留最后那段回答，一个要全程的事件。[/red]")
        raise typer.Exit(code=2)
    policy = build_policy(permissions, ask=ask)
    runtime = build_runtime(prompt, model=model, max_turns=max_turns, runs_dir=runs_dir,
                            workdir=workdir, demo=demo, session=session,
                            compact_budget=compact_budget, permissions=policy,
                            subagents=not no_subagents, subagent_depth=subagent_depth)
    if not (as_json or print_only or events):
        spec = runtime.provider.spec
        console.print(f"[bold]agent run[/bold] {runtime.run_id}")
        console.print(f"模型 [cyan]{spec.name}[/cyan] / {spec.model} @ {spec.base_url}"
                      f"｜轮数上限 {max_turns}{'｜离线 demo' if demo else ''}")
        console.print(f"权限 [cyan]{policy.mode}[/cyan]"
                      f"｜工具 {', '.join(runtime.registry.names())}")
    try:
        if events:
            # 事件走 stdout（一行一条 JSON），所以下面的结束方式表格一律不打——混在一起就没人能解析了
            result = run_with_events(runtime, prompt or DEMO_PROMPT,
                                     writer=StreamWriter(sys.stdout))
            raise typer.Exit(code=0 if result.ok else 1)
        result = runtime.start(prompt or DEMO_PROMPT)
    except KeyboardInterrupt:  # pragma: no cover - 交互时才发生（工具子进程里按 Ctrl-C）
        console.print("\n[yellow]收到中断：已跑完的记录保留，正在收尾。[/yellow]")
        result = LoopResult(stopped=STOPPED_ABORTED, turns=len(runtime.turns),
                            session_id=runtime.session.session_id)
        runtime.write_manifest(result)
    if print_only:
        console.print(final_text(runtime, result), markup=False, soft_wrap=True, highlight=False)
    elif as_json:
        print_json(json.loads(runtime.run.manifest_path.read_text(encoding="utf-8")))
    else:
        console.print()
        console.print(runtime.session.transcript(), markup=False)
        console.print()
        table = Table(show_header=False, box=None, padding=(0, 1))
        table.add_row("结束方式", result.stopped)
        table.add_row("轮数 / 工具调用", f"{result.turns} / {result.tool_calls}")
        table.add_row("token", str(result.usage or "端点没回"))
        table.add_row("会话", str(runtime.run.session_path))
        table.add_row("事件流", str(runtime.run.events_path))
        table.add_row("manifest", str(runtime.run.manifest_path))
        console.print(table)
    if not result.ok:
        console.print(f"[red]这次 run 没跑完：{result.error or result.stopped}[/red]")
        raise typer.Exit(code=1)


@agent_app.command("replay")
def replay_cmd(
    session: Path = typer.Argument(..., exists=True, readable=True, help="session.jsonl 路径"),
    limit: int | None = typer.Option(None, "--limit", help="只看最后几条"),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """回放一次会话：每条记录一行，带 id 和 parent，能看出分支从哪分出去的。"""
    tree = SessionTree.load(session)
    if as_json:
        print_json([{"id": e.id, "parent": e.parent, "kind": e.kind, "role": e.role, "ts": e.ts, "data": e.data}
                    for e in tree.entries])
        return
    console.print(f"[bold]{session}[/bold]｜{len(tree)} 条记录｜分支 {len(tree.leaves())} 条")
    console.print(tree.transcript(limit), markup=False)


@agent_app.command("ops")
def ops_cmd(
    run_dir: Path = typer.Argument(..., exists=True, file_okay=False, help="runs/agent-* 目录"),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """看一次 run 的操作日志与现场：跑到哪了、有没有没结束的操作、要不要 resume。"""
    runtime = AgentRuntime.reopen(run_dir)
    payload = runtime.inspect()
    if as_json:
        print_json(payload)
        return
    console.print(f"[bold]{payload['run_id']}[/bold]｜会话 {payload['session']['entries']} 条"
                  f"｜叶子 {payload['session']['leaf']}｜末尾 {payload['session']['tail']}")
    table = Table(title="操作日志（只追加，当前状态是折叠出来的）")
    table.add_column("操作", style="bold")
    table.add_column("类型")
    table.add_column("状态")
    table.add_column("记录")
    table.add_column("要点", overflow="fold")
    for op in payload["operations"]:
        detail = " ".join(f"{k}={v}" for k, v in op["detail"].items() if k not in ("session_id", "leaf"))
        table.add_row(op["id"], op["kind"], op["status"], str(op["records"]),
                      (op["error"] or detail) or "-")
    console.print(table)
    console.print(f"用量 {payload['usage'] or '（端点没回 usage）'}")
    if payload["torn_lines"]:
        console.print(f"[yellow]日志有半行（崩溃时写了一半）：{payload['torn_lines']}[/yellow]")
    if payload["pending_tools"]:
        console.print(f"[yellow]有没拿到结果的工具调用：{payload['pending_tools']}[/yellow]")
    console.print("判断：" + ("有没跑完的活，可以 `sparkjury agent resume <目录>` 接着跑"
                             if payload["needs_resume"] else "没有未完成的操作，不需要恢复"))


@agent_app.command("resume")
def resume_cmd(
    run_dir: Path = typer.Argument(..., exists=True, file_okay=False, help="runs/agent-* 目录"),
    model: str = typer.Option("subject", "--model", "-m"),
    max_turns: int = typer.Option(12, "--max-turns"),
    workdir: Path = typer.Option(Path("."), "--workdir"),
    compact_budget: int = typer.Option(0, "--compact-budget"),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """从中断处接着跑：已经拿到结果的工具重放而不重跑，状态未知的交给模型决定。"""
    probe = AgentRuntime.reopen(run_dir).inspect()
    if not probe["needs_resume"]:
        console.print("没有未完成的操作，也没有待执行的工具——这次 run 不需要恢复。")
        raise typer.Exit()
    runtime = AgentRuntime(_real_provider(model), run_dir=run_dir, reopen=True, workdir=workdir,
                           max_turns=max_turns, compaction=CompactionPolicy(max_prompt_tokens=compact_budget))
    result = runtime.resume()
    if as_json:
        print_json(json.loads(runtime.run.manifest_path.read_text(encoding="utf-8")))
    else:
        console.print(f"[bold]resume[/bold] {runtime.run_id}｜{runtime.session.transcript(limit=6).splitlines()[-1]}")
        console.print(f"结束方式 {result.stopped}｜轮数 {result.turns}｜重放的调用 {result.replayed_calls}")
        console.print(f"manifest {runtime.run.manifest_path}")
    if not result.ok:
        raise typer.Exit(code=1)


@agent_app.command("compact")
def compact_cmd(
    session: Path = typer.Argument(..., exists=True, readable=True, help="session.jsonl 路径"),
    keep_tail: int = typer.Option(8, "--keep-tail", help="末尾保留多少条原文"),
    apply: bool = typer.Option(False, "--apply", help="真的写进去；默认只试算"),
    model: str | None = typer.Option(None, "--model", help="给了就用真模型写摘要，否则用确定性兜底摘要"),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """压缩历史：插一条摘要 entry 顶替更早的消息，原文一条不删。默认只试算不落盘。"""
    tree = SessionTree.load(session)
    plan = plan_compaction(tree, keep_tail_entries=keep_tail, reason="手动压缩")
    if plan is None:
        console.print(f"没什么可压的：这条分支只有 {len(tree._compacted_branch())} 条"  # noqa: SLF001
                      f"（末尾保留 {keep_tail} 条 + 至少一条才值得压）。")
        raise typer.Exit()
    if not apply:
        payload = {"plan": {"from": plan.from_id, "to": plan.to_id, "replaced": plan.replaced},
                   "branch_entries": len(tree._compacted_branch()),  # noqa: SLF001
                   "kept": keep_tail, "applied": False}
        print_json(payload) if as_json else console.print(
            f"试算（没落盘）：压 {plan.replaced} 条 → 1 条摘要，末尾保留 {keep_tail} 条。"
            f"\n加 --apply 才会写进 {session}。")
        return
    compactor = Compactor(policy=CompactionPolicy(keep_tail_entries=keep_tail),
                          provider=_real_provider(model) if model else None, use_model=bool(model))
    entry = compactor.compact(tree, reason="手动压缩")
    if entry is None:
        console.print("没压成：条件不满足。")
        raise typer.Exit(code=1)
    summary = str(entry.data.get("summary", ""))
    payload = {"entry": entry.id, "replaced": entry.data.get("replaced"), "source": entry.data.get("source"),
               "summary": summary, "applied": True, "path": str(session)}
    print_json(payload) if as_json else console.print(
        f"已压缩：{entry.data.get('replaced')} 条 → {entry.id}（来源 {entry.data.get('source')}）"
        f"\n—— 原文一条没删，`sparkjury agent replay {session}` 照样看得到全部记录。")


@agent_app.command("endpoints")
def endpoints_cmd(as_json: bool = typer.Option(False, "--json")) -> None:
    """看一眼四个本地端点和两个云端端点的短名。"""
    rows = describe_endpoints()
    if as_json:
        print_json([{"name": n, "model": m, "base_url": u, "note": note} for n, m, u, note in rows])
        return
    table = Table(title="模型端点")
    table.add_column("短名", style="bold")
    table.add_column("模型")
    table.add_column("地址")
    table.add_column("说明", overflow="fold")
    for name, model, url, note in rows:
        table.add_row(name, model, url, note)
    console.print(table)


__all__ = ["agent_app", "demo_provider", "DEMO_PROMPT", "DEMO_REPLIES"]
