"""SparkJury command line. Every evaluation skill is a subcommand."""

from __future__ import annotations

import json
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Column, Table

from sparkjury import __version__
from sparkjury.agent.cli import agent_app
from sparkjury.adapters import load as load_traces
from sparkjury.models.trace import TraceSource
from sparkjury.store import TraceStore

app = typer.Typer(help="SparkJury: agent evaluation harness for DGX Spark.", no_args_is_help=True)

# Agent harness 子命令（sparkjury agent ...）。逻辑在 sparkjury/agent/ 里，这里只是挂上去。
app.add_typer(agent_app, name="agent")
console = Console()
# 降级提示走 stderr：`--json` 的 stdout 要能直接喂给 json.loads，混一行警告进去就解析不了。
err_console = Console(stderr=True)


@app.callback()
def _root(version: bool = typer.Option(False, "--version", help="Print version and exit.")) -> None:
    if version:
        console.print(f"sparkjury {__version__}")
        raise typer.Exit()


@app.command()
def ingest(
    path: Path = typer.Option(..., "--path", "-p", exists=True, readable=True, help="Trace file."),
    source: TraceSource = typer.Option(TraceSource.TAU2, "--source", "-s", help="tau2 | otel"),
    db: Path = typer.Option(Path("runs/sparkjury.db"), "--db", help="SQLite database path."),
) -> None:
    """Load a trace file into the store (M1)."""
    traces = load_traces(path, source)
    with TraceStore(db) as store:
        n = store.upsert_traces(traces)
        total = store.count()
    console.print(f"[green]ingested[/] {n} traces from {path} (source={source.value}) -> {db} (total {total})")


@app.command()
def stats(db: Path = typer.Option(Path("runs/sparkjury.db"), "--db"), as_json: bool = typer.Option(False, "--json")) -> None:
    """Summarise what is in the store: tasks, trials, pass rate, pass^k, step counts."""
    with TraceStore(db) as store:
        st = store.stats()
    if as_json:
        console.print_json(json.dumps(st.as_dict(), ensure_ascii=False))
        return
    t = Table(title=f"SparkJury store - {db}", show_header=False)
    t.add_row("traces", str(st.n_traces))
    t.add_row("tasks", str(st.n_tasks))
    t.add_row("trials per task", ", ".join(f"{k} trials × {n} tasks" for k, n in sorted(st.trials_per_task.items())) or "-")
    t.add_row("traces with gold", str(st.n_with_gold))
    t.add_row("pass rate (pass^1)", _pct(st.pass_rate))
    for k, v in sorted(st.pass_k.items()):
        if k > 1:
            t.add_row(f"pass^{k}", _pct(v))
    t.add_row("avg steps", _num(st.avg_steps))
    t.add_row("avg tool calls", _num(st.avg_tool_calls))
    t.add_row("avg tool errors", _num(st.avg_tool_errors))
    t.add_row("avg duration (s)", _num(st.avg_duration_s))
    t.add_row("termination", ", ".join(f"{k}={v}" for k, v in sorted(st.termination.items())) or "-")
    t.add_row("sources", ", ".join(f"{k}={v}" for k, v in st.sources.items()) or "-")
    t.add_row("agent models", ", ".join(f"{k}={v}" for k, v in st.agent_models.items()) or "-")
    console.print(t)


@app.command()
def show(
    trace_id: str = typer.Argument(..., help="trace_id to print"),
    db: Path = typer.Option(Path("runs/sparkjury.db"), "--db"),
    width: int = typer.Option(160, "--width"),
) -> None:
    """Print one trace as a readable transcript."""
    with TraceStore(db) as store:
        tr = store.get(trace_id)
    if tr is None:
        console.print(f"[red]not found:[/] {trace_id}")
        raise typer.Exit(code=1)
    o = tr.outcome
    console.rule(f"{tr.trace_id}  task={tr.task_id} trial={tr.trial} model={tr.agent_model or '-'}")
    console.print(
        f"success={o.success} reward={o.reward} termination={o.termination_reason} "
        f"steps={tr.metrics.n_steps} tool_calls={tr.metrics.n_tool_calls} tool_errors={tr.metrics.n_tool_errors}"
    )
    console.print(tr.transcript(width=width), markup=False, highlight=False)


@app.command("list")
def list_cmd(
    db: Path = typer.Option(Path("runs/sparkjury.db"), "--db"),
    task_id: str | None = typer.Option(None, "--task"),
    failed: bool = typer.Option(False, "--failed", help="only traces whose gold outcome is fail"),
    limit: int = typer.Option(50, "--limit"),
) -> None:
    """List traces in the store."""
    with TraceStore(db) as store:
        rows = store.list(task_id=task_id, success=False if failed else None, limit=limit)
    t = Table(Column("trace_id", overflow="fold"), "task", "trial", "success", "reward", "steps", "tools", "errs", "termination")
    for tr in rows:
        t.add_row(
            tr.trace_id, tr.task_id, str(tr.trial), str(tr.outcome.success), _num(tr.outcome.reward),
            str(tr.metrics.n_steps), str(tr.metrics.n_tool_calls), str(tr.metrics.n_tool_errors),
            tr.outcome.termination_reason or "-",
        )
    console.print(t)


@app.command()
def precheck(
    db: Path = typer.Option(Path("runs/sparkjury.db"), "--db"),
    step_latency_ms: float | None = typer.Option(120_000, "--step-latency-ms", help="single step slower than this is a timeout; 0 disables"),
    max_duration_s: float | None = typer.Option(None, "--max-duration-s", help="whole run longer than this is a timeout"),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Label environment-caused failures (M2). Flagged traces are excluded from judging."""
    from sparkjury.precheck import PrecheckConfig, run_many

    cfg = PrecheckConfig(step_latency_ms=step_latency_ms or None, max_duration_s=max_duration_s)
    with TraceStore(db) as store:
        traces = store.list()
        results = run_many(traces, cfg)
        store.put_precheck(results)
        summary = store.precheck_summary()
    flagged = [r for r in results if r.is_env_failure]
    if as_json:
        console.print_json(json.dumps({"summary": summary, "flagged": [r.model_dump(mode="json") for r in flagged]}, ensure_ascii=False))
        return
    console.print(
        f"[green]prechecked[/] {summary['n_checked']} traces: "
        f"{summary['n_env_failures']} environment failures, {summary['n_scorable']} go to judges"
    )
    if summary["kinds"]:
        console.print("  by kind: " + ", ".join(f"{k}={v}" for k, v in sorted(summary["kinds"].items())))
    if flagged:
        t = Table(Column("trace_id", overflow="fold"), "kind", "step", Column("note", overflow="fold"))
        for r in flagged:
            for f in r.flags:
                t.add_row(r.trace_id, f.kind.value, "-" if f.evidence_step_idx is None else str(f.evidence_step_idx), f.note)
        console.print(t)


@app.command()
def score(
    db: Path = typer.Option(Path("runs/sparkjury.db"), "--db"),
    judges: str = typer.Option("mock", "--judges", help="'mock' or path to a panel TOML (see deploy/judges.example.toml)"),
    dims: str | None = typer.Option(None, "--dims", help="comma-separated subset of outcome,tool_use,efficiency,safety"),
    limit: int | None = typer.Option(None, "--limit"),
    workers: int | None = typer.Option(None, "--workers"),
    trace_id: str | None = typer.Option(None, "--trace", help="score a single trace"),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Score prechecked traces with the judge panel (M3). Disagreements are flagged for arbitration."""
    from sparkjury.judges import Panel, PanelConfig
    from sparkjury.models.verdict import Dimension

    cfg = PanelConfig.mock() if judges == "mock" else PanelConfig.from_toml(judges)
    if dims:
        cfg.dimensions = [Dimension(d.strip()) for d in dims.split(",") if d.strip()]
    if workers:
        cfg.workers = workers
    panel = Panel.from_config(cfg)

    with TraceStore(db) as store:
        if trace_id:
            t = store.get(trace_id)
            if t is None:
                console.print(f"[red]not found:[/] {trace_id}")
                raise typer.Exit(code=1)
            traces = [t]
        else:
            traces = store.scorable_traces()
            if limit:
                traces = traces[:limit]
        results = panel.score_many(traces)
        store.put_panel_results(results)
        summary = store.verdict_summary()

    if as_json:
        console.print_json(json.dumps({"summary": summary, "results": [r.model_dump(mode="json") for r in results]}, ensure_ascii=False))
        return

    names = [j.name for j in panel.judges]
    t = Table(Column("trace_id", overflow="fold"), "dimension", *names, "agree", Column("why", overflow="fold"))
    for r in results:
        for a in r.agreement:
            cells = []
            for n in names:
                s = a.scores.get(n)
                lab = a.labels.get(n)
                cells.append(("-" if s is None else str(s)) + (f" {lab}" if lab else ""))
            t.add_row(r.trace_id, a.dimension.value, *cells, "yes" if a.agreed else "NO", a.reason)
    console.print(t)
    console.print(
        f"[green]scored[/] {summary['n_traces_scored']} traces, {summary['n_verdicts']} verdicts; "
        f"{summary['n_needing_arbitration']} traces need arbitration "
        f"(agreement rate {_pct(summary['agreement_rate'])})"
    )
    if summary["disagreements_by_dimension"]:
        console.print("  disagreements by dimension: " + ", ".join(f"{k}={v}" for k, v in sorted(summary["disagreements_by_dimension"].items())))
    for name, j in summary["judges"].items():
        console.print(f"  {name}: {j['n_verdicts']} verdicts, {j['n_errors']} errors, mean score {_num(j['mean_score'])}, mean latency {_num(j['mean_latency_ms'])} ms")


@app.command()
def verdicts(
    trace_id: str = typer.Argument(...),
    db: Path = typer.Option(Path("runs/sparkjury.db"), "--db"),
) -> None:
    """Show the three judges' verdicts and rationale for one trace."""
    with TraceStore(db) as store:
        pr = store.get_panel_result(trace_id)
    if pr is None:
        console.print(f"[red]no verdicts for[/] {trace_id}")
        raise typer.Exit(code=1)
    for a in pr.agreement:
        console.rule(f"{trace_id} | {a.dimension.value} | {'agreed' if a.agreed else 'DISAGREE'} ({a.reason})")
        for v in pr.verdicts_for(a.dimension):
            head = f"{v.judge} [{v.model}] score={v.score}" + (f" label={v.label}" if v.label else "")
            if v.error:
                console.print(f"  {head}  ERROR: {v.error}", markup=False)
                continue
            ev = ",".join(map(str, v.evidence_steps)) or "-"
            console.print(f"  {head} conf={_num(v.confidence)} steps=[{ev}]", markup=False)
            console.print(f"      {v.rationale}", markup=False, highlight=False)


@app.command()
def arbitrate(
    db: Path = typer.Option(Path("runs/sparkjury.db"), "--db"),
    jev: str = typer.Option("auto", "--jev", help="auto (use TYPESAFE_API_KEY if set) | off"),
    jev_timeout_s: float = typer.Option(5.0, "--jev-timeout-s"),
    judges: str = typer.Option("mock", "--judges", help="panel config used for the local fallback and audit judges"),
    audit_rate: float = typer.Option(0.05, "--audit-rate", help="fraction of traces re-scored by the audit judge"),
    trace_id: str | None = typer.Option(None, "--trace"),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Resolve judge disagreements (M4): Jev in the cloud, local judge when Jev is unreachable, 5% audit."""
    from sparkjury.arbiter import Arbiter, JevClient
    from sparkjury.judges import Panel, PanelConfig

    cfg = PanelConfig.mock() if judges == "mock" else PanelConfig.from_toml(judges)
    panel = Panel.from_config(cfg)
    local = panel.judges[0]                       # Judge A doubles as the local arbiter
    audit = panel.judges[-1] if len(panel.judges) > 1 else None
    jev_client = None if jev == "off" else JevClient(timeout_s=jev_timeout_s)
    arbiter = Arbiter(jev=jev_client, local_judge=local, audit_judge=audit, audit_rate=audit_rate)

    with TraceStore(db) as store:
        panels = [store.get_panel_result(trace_id)] if trace_id else store.list_panel_results()
        panels = [p for p in panels if p is not None]
        if not panels:
            console.print("[red]no panel results in store; run `sparkjury score` first[/]")
            raise typer.Exit(code=1)
        pairs = [(store.get(p.trace_id), p) for p in panels]
        decisions = arbiter.decide_many([(t, p) for t, p in pairs if t is not None])
        store.put_decisions(decisions)
        summary = store.arbitration_summary()

    if as_json:
        console.print_json(json.dumps({"summary": summary, "decisions": [d.model_dump(mode="json") for d in decisions]}, ensure_ascii=False))
        return

    t = Table(Column("trace_id", overflow="fold"), "dimension", "panel", "final", "source", "degraded", "audit")
    for d in decisions:
        for a in d.arbitrations:
            if a.source.value == "panel" and not a.audit_sampled:
                continue  # only show the interesting rows
            panel_txt = " / ".join("-" if s is None else str(s) for s in a.panel_scores.values())
            final_txt = ("-" if a.final_score is None else str(a.final_score)) + (f" {a.final_label}" if a.final_label else "")
            audit_txt = "-" if not a.audit_sampled else (f"{a.audit_score} {'DIFF' if a.audit_disagrees else 'ok'}" if a.audit_score is not None else "err")
            t.add_row(a.trace_id, a.dimension.value, panel_txt, final_txt, a.source.value, "yes" if a.degraded else "", audit_txt)
    console.print(t)
    jev_state = "off" if jev_client is None else ("configured" if jev_client.configured else "not configured (TYPESAFE_API_KEY unset)")
    console.print(
        f"[green]decided[/] {summary['n_traces']} traces x {summary['n_dimensions']} dimensions; "
        f"sources: " + ", ".join(f"{k}={v}" for k, v in sorted(summary['by_source'].items())) +
        f"; degraded={summary['n_degraded']}; audited={summary['n_audited_dimensions']} "
        f"(disagreements {summary['n_audit_disagreements']}); jev: {jev_state}"
    )


@app.command()
def cluster(
    db: Path = typer.Option(Path("runs/sparkjury.db"), "--db"),
    embedder: str = typer.Option("hash", "--embedder", help="hash (offline) | openai (vLLM /v1/embeddings)"),
    embed_base_url: str = typer.Option("http://127.0.0.1:8003/v1", "--embed-base-url"),
    embed_model: str = typer.Option("Qwen/Qwen3-Embedding-0.6B", "--embed-model"),
    method: str = typer.Option("auto", "--method", help="auto | hdbscan | threshold"),
    min_cluster_size: int = typer.Option(3, "--min-cluster-size"),
    jev: str = typer.Option("auto", "--jev", help="auto (label clusters with Jev if TYPESAFE_API_KEY set) | off"),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Select badcases, cluster them, label each cluster, rank by frequency x severity (M5)."""
    from sparkjury.arbiter import JevClient
    from sparkjury.cluster import HashingEmbedder, OpenAIEmbedder, build_badcase, cluster_badcases, label_clusters
    from sparkjury.models.cluster import ClusterRun

    emb = HashingEmbedder() if embedder == "hash" else OpenAIEmbedder(embed_base_url, embed_model)
    with TraceStore(db) as store:
        decisions = store.list_decisions()
        if not decisions:
            console.print("[red]no decisions in store; run `sparkjury arbitrate` first[/]")
            raise typer.Exit(code=1)
        badcases = []
        for d in decisions:
            t = store.get(d.trace_id)
            if t is None:
                continue
            b = build_badcase(t, d, store.get_panel_result(d.trace_id))
            if b:
                badcases.append(b)
        try:
            clusters, used = cluster_badcases(badcases, emb, min_cluster_size=min_cluster_size, method=method)
            emb_name = emb.name
        except Exception as e:  # noqa: BLE001 - embedding server down -> offline embedder
            err_console.print(f"[yellow]embedder {emb.name} failed ({type(e).__name__}); falling back to hashing[/]")
            emb = HashingEmbedder()
            clusters, used = cluster_badcases(badcases, emb, min_cluster_size=min_cluster_size, method=method)
            emb_name = emb.name + " (fallback)"
        jev_client = None if jev == "off" else JevClient()
        if jev_client is not None and not jev_client.configured:
            err_console.print("[yellow]TYPESAFE_API_KEY not set; cluster labels fall back to heuristic[/]")
        label_counts = label_clusters(clusters, badcases, jev_client)
        if label_counts["n_jev_failed"]:
            err_console.print(f"[yellow]Jev failed on {label_counts['n_jev_failed']} cluster(s); "
                              "those labels fall back to heuristic[/]")
        run = ClusterRun(
            n_badcases=len(badcases), n_clusters=sum(1 for c in clusters if c.cluster_id != -1),
            n_noise=sum(c.size for c in clusters if c.cluster_id == -1), embedder=emb_name, method=used,
            clusters=clusters, badcases=badcases,
        )
        store.put_cluster_run(run)

    if as_json:
        console.print_json(run.model_dump_json())
        return
    t = Table("rank", "label", "size", "share", "severity", "priority", "dims", Column("representatives", overflow="fold"), Column("source"))
    for c in run.clusters:
        t.add_row(str(c.rank), c.label.value, str(c.size), f"{c.share:.0%}", _num(c.severity), _num(c.priority),
                  " ".join(f"{k}x{v}" for k, v in c.failed_dimension_counts.items()),
                  ", ".join(r.trace_id for r in c.representatives), c.label_source)
    console.print(t)
    console.print(
        f"[green]clustered[/] {run.n_badcases} badcases into {run.n_clusters} cluster(s) + {run.n_noise} unclustered; "
        f"embedder={run.embedder}, method={run.method}"
    )
    for c in run.clusters:
        if c.cluster_id != -1:
            console.print(f"  #{c.rank} {c.label.value}: {c.summary} Suggestion: {c.suggestion}", markup=False, highlight=False)


@app.command()
def report(
    db: Path = typer.Option(Path("runs/sparkjury.db"), "--db"),
    out: Path = typer.Option(Path("runs/card"), "--out", help="output directory"),
    fmt: str = typer.Option("all", "--format", help="all | json | md | html"),
    title: str | None = typer.Option(None, "--title"),
    run_id: str = typer.Option("latest", "--run-id"),
) -> None:
    """Build the evidence card (M6): totals, quality, ranked clusters with evidence, one recommendation."""
    from sparkjury.report import build_card, write_card

    with TraceStore(db) as store:
        card = build_card(store, run_id=run_id, title=title)
    formats = ("json", "md", "html") if fmt == "all" else tuple(f.strip() for f in fmt.split(","))
    files = write_card(card, out, formats=formats)
    t, q = card.totals, card.quality
    console.print(f"[green]card[/] {t.n_traces} traces, {t.n_env_failures} env failures, {t.n_badcases} badcases in {t.n_clusters} cluster(s); "
                  f"pass^1 {_pct(q.pass_rate)}" + "".join(f", pass^{k} {_pct(v)}" for k, v in sorted(q.pass_k.items()) if k > 1) +
                  f"; judge agreement {_pct(q.judge_agreement_rate)}; degraded {q.n_degraded}")
    console.print(f"  recommendation: {card.recommendation}", markup=False, highlight=False)
    for f in files:
        console.print(f"  wrote {f}")


@app.command()
def regress(
    before: Path = typer.Option(..., "--before", exists=True),
    after: Path = typer.Option(..., "--after", exists=True),
    out: Path | None = typer.Option(None, "--out", help="write the markdown report here"),
    pairwise: str | None = typer.Option(None, "--pairwise", help="'mock' or a panel TOML; judges before/after pairs with order swap"),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Compare two evaluation stores (M6): pass^k before/after, tasks fixed or broken, cluster shifts."""
    from sparkjury.regress import compare, render_markdown

    judge = None
    if pairwise == "mock":
        from sparkjury.judges.pairwise import MockPairwiseJudge

        judge = MockPairwiseJudge()
    elif pairwise:
        import os

        from sparkjury.judges import PanelConfig
        from sparkjury.judges.pairwise import OpenAIPairwiseJudge

        spec = PanelConfig.from_toml(pairwise).judges[0]
        judge = OpenAIPairwiseJudge(spec.name, spec.model, spec.base_url or "", os.environ.get(spec.api_key_env) if spec.api_key_env else None, timeout_s=spec.timeout_s)
    r = compare(before, after, pairwise=judge, before_label=before.name, after_label=after.name)
    if as_json:
        console.print_json(r.model_dump_json())
        return
    md = render_markdown(r)
    console.print(f"[green]regression[/] {r.verdict}: pass^{r.k} {_pct(r.pass_k_before)} -> {_pct(r.pass_k_after)} "
                  f"({'-' if r.delta_pass_k is None else f'{r.delta_pass_k * 100:+.1f} pp'}); "
                  f"fixed {len(r.fixed_tasks)}, broken {len(r.broken_tasks)}; badcases {r.n_badcases_before} -> {r.n_badcases_after}")
    if r.pairwise_summary:
        console.print("  pairwise: " + ", ".join(f"{k}={v}" for k, v in sorted(r.pairwise_summary.items())))
    if out:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(md, encoding="utf-8")
        console.print(f"  wrote {out}")
    else:
        console.print(md, markup=False, highlight=False)


@app.command()
def run(
    config: Path | None = typer.Option(None, "--config", "-c", exists=True, help="run TOML (see deploy/run.example.toml)"),
    demo: bool = typer.Option(False, "--demo", help="offline demo on the bundled samples with mock judges"),
    run_id: str | None = typer.Option(None, "--run-id"),
    db: Path | None = typer.Option(None, "--db"),
    stages: str | None = typer.Option(None, "--stages", help="comma-separated subset, e.g. SCORE,ARBITRATE,CLUSTER,REPORT"),
    quiet: bool = typer.Option(False, "--quiet", help="only print stage lines, no per-trace progress"),
) -> None:
    """Run the whole pipeline as one state machine (M7): INGEST -> PRECHECK -> EVALSET -> SCORE -> ARBITRATE -> CLUSTER -> REPORT."""
    from sparkjury.harness import EventKind, RunConfig, Stage, run_config

    if demo:
        cfg = RunConfig.demo(run_id=run_id, db=str(db) if db else None)
    elif config:
        cfg = RunConfig.from_toml(config)
        if run_id:
            cfg.run_id = run_id
        if db:
            cfg.db = str(db)
    else:
        console.print("[red]give --config FILE or --demo[/]")
        raise typer.Exit(code=2)
    if stages:
        cfg.stages = [Stage(s.strip().upper()) for s in stages.split(",") if s.strip()]

    colors = {EventKind.STAGE_START: "cyan", EventKind.STAGE_END: "green", EventKind.DEGRADED: "yellow",
              EventKind.WARNING: "yellow", EventKind.ERROR: "red", EventKind.RUN_START: "bold", EventKind.RUN_END: "bold"}

    def on_event(ev):
        if ev.kind == EventKind.PROGRESS and quiet:
            return
        c = colors.get(ev.kind, "white")
        extra = ""
        if ev.kind == EventKind.STAGE_END:
            keys = [k for k in ("n_ingested", "n_env_failures", "n_traces", "n_needing_arbitration", "n_degraded", "n_badcases", "n_clusters") if k in ev.data]
            extra = "  " + " ".join(f"{k}={ev.data[k]}" for k in keys) + f"  ({ev.data.get('duration_s', 0):.2f}s)"
        prefix = "  " if ev.kind == EventKind.PROGRESS else ""
        console.print(f"{prefix}[{c}]{ev.kind.value:12}[/] {ev.message}{extra}", highlight=False)

    manifest = run_config(cfg, on_event=on_event)
    console.print(f"run dir: {cfg.run_dir}  status: {manifest['status']}  degradations: {len(manifest['degradations'])}")
    rep = manifest["stages"].get("REPORT", {})
    if rep.get("recommendation"):
        console.print(f"recommendation: {rep['recommendation']}", markup=False, highlight=False)
    if manifest["status"] != "ok":
        console.print(f"[red]{manifest.get('error')}[/]")
        raise typer.Exit(code=1)


@app.command()
def runs(
    runs_dir: Path = typer.Option(Path("runs"), "--runs-dir"),
) -> None:
    """List runs (from runs/<run_id>/manifest.json)."""
    rows = []
    for p in sorted(runs_dir.glob("*/manifest.json")):
        try:
            m = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        rep = m.get("stages", {}).get("REPORT", {})
        rows.append((m.get("run_id", p.parent.name), m.get("status", "?"), f"{m.get('duration_s', 0):.1f}s",
                     str(len(m.get("degradations", []))), _pct(rep.get("pass_rate")), str(rep.get("n_badcases", "-"))))
    t = Table("run_id", "status", "duration", "degraded", "pass^1", "badcases")
    for r in rows:
        t.add_row(*r)
    console.print(t)


@app.command()
def events(
    run_id: str = typer.Argument(...),
    runs_dir: Path = typer.Option(Path("runs"), "--runs-dir"),
    kind: str | None = typer.Option(None, "--kind", help="filter: stage_end, degraded, progress, ..."),
) -> None:
    """Print the event log of a run (runs/<run_id>/events.jsonl)."""
    from sparkjury.harness import EventBus

    evs = EventBus.read_log(runs_dir / run_id / "events.jsonl")
    if not evs:
        console.print(f"[red]no events for run[/] {run_id}")
        raise typer.Exit(code=1)
    for ev in evs:
        if kind and ev.kind.value != kind:
            continue
        console.print(f"{ev.ts} {ev.seq:4d} {ev.kind.value:12} {ev.stage or '-':9} {ev.message}", highlight=False, markup=False)


@app.command()
def serve(
    host: str = typer.Option("0.0.0.0", "--host"),
    port: int = typer.Option(9000, "--port", help="on the DGX node use 9000 (mapped to public 9030)"),
    runs_dir: Path = typer.Option(Path("runs"), "--runs-dir"),
    no_probe: bool = typer.Option(False, "--no-probe", help="skip probing model endpoints in /dgx"),
    token: str | None = typer.Option(None, "--token", help="API token (default: env SPARKJURY_API_TOKEN); required when binding a public interface, otherwise serve refuses to start"),
) -> None:
    """Start the API + Cockpit (M8). Open http://<host>:<port>/ in a browser.

    绑公网（非回环）地址时必须给 token，否则这里直接拒绝启动：日志里滚过去的一句警告拦不住任何人，
    而「8888 和 9000 上对外提供的服务必须有鉴权」是节点手册的红线。本机自用 `--host 127.0.0.1` 不需要 token。
    """
    import os

    import uvicorn

    from sparkjury.api import create_app

    tok = token if token is not None else os.environ.get("SPARKJURY_API_TOKEN") or None
    if host not in ("127.0.0.1", "localhost", "::1") and not tok:
        err_console.print(
            f"[red]refusing to serve on {host} without a token[/]：8888/9000 上对外提供的服务必须有鉴权（节点手册红线）。"
            "设 SPARKJURY_API_TOKEN（节点上是 deploy/dgx/.env 里那一项）或加 --token；只想本机用就 --host 127.0.0.1。"
        )
        raise typer.Exit(code=2)
    console.print(f"[green]SparkJury cockpit[/] http://{host}:{port}/" + ("?token=<token>" if tok else "") + f"   runs dir: {runs_dir}   auth: {'on' if tok else 'off'}")
    # access_log off: request lines would otherwise print ?token= query strings into the log
    uvicorn.run(create_app(runs_dir, probe_endpoints=not no_probe, token=tok), host=host, port=port, log_level="info", access_log=False)


@app.command()
def export(
    out: Path = typer.Option(Path("runs/traces.jsonl"), "--out"),
    db: Path = typer.Option(Path("runs/sparkjury.db"), "--db"),
) -> None:
    """Export all traces as JSONL (one canonical Trace per line)."""
    with TraceStore(db) as store:
        n = store.export_jsonl(out)
    console.print(f"[green]exported[/] {n} traces -> {out}")


def _pct(v: float | None) -> str:
    return "-" if v is None else f"{v * 100:.1f}%"


def _num(v: float | None) -> str:
    return "-" if v is None else f"{v:.2f}"


if __name__ == "__main__":
    app()
