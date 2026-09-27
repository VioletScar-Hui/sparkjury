"""Build the evidence card from a store and render it as JSON, Markdown or HTML."""

from __future__ import annotations

import html
import json
from pathlib import Path

from sparkjury.models.cluster import FailureLabel
from sparkjury.models.report import (
    CardCluster,
    CardQuality,
    CardRepresentative,
    CardTotals,
    EvidenceCard,
    JudgeOpinion,
)
from sparkjury.models.verdict import ALL_DIMENSIONS, Dimension
from sparkjury.store import TraceStore


# ---- build --------------------------------------------------------------------


def build_card(store: TraceStore, run_id: str = "latest", title: str | None = None) -> EvidenceCard:
    st = store.stats()
    pre = store.precheck_summary()
    vs = store.verdict_summary()
    ar = store.arbitration_summary()
    crun = store.get_cluster_run()

    totals = CardTotals(
        n_traces=st.n_traces, n_tasks=st.n_tasks,
        n_env_failures=pre["n_env_failures"], env_kinds=pre["kinds"], n_scorable=pre["n_scorable"] if pre["n_checked"] else st.n_traces,
        n_scored=vs["n_traces_scored"],
        n_badcases=crun.n_badcases if crun else 0, n_clusters=crun.n_clusters if crun else 0,
        n_unclustered=crun.n_noise if crun else 0,
    )
    quality = CardQuality(
        pass_rate=st.pass_rate, pass_k=st.pass_k, pass_k_comb=st.pass_k_comb, pass_at_k=st.pass_at_k,
        agent_model=max(st.agent_models, key=st.agent_models.get) if st.agent_models else None,
        judge_agreement_rate=vs["agreement_rate"], n_needing_arbitration=vs["n_needing_arbitration"],
        decisions_by_source=ar["by_source"], n_degraded=ar["n_degraded"],
        n_audited=ar["n_audited_dimensions"], n_audit_disagreements=ar["n_audit_disagreements"],
        n_outcome_fail=ar["n_outcome_fail"], mean_scores=_mean_scores(store), judges=vs["judges"],
    )
    clusters = [_card_cluster(store, c, crun) for c in crun.clusters] if crun else []
    card = EvidenceCard(
        run_id=run_id, title=title or "SparkJury evidence card", totals=totals, quality=quality, clusters=clusters,
        embedder=crun.embedder if crun else None, cluster_method=crun.method if crun else None,
    )
    card.recommendation = _recommendation(card)
    return card


def _mean_scores(store: TraceStore) -> dict[str, float | None]:
    sums: dict[str, list[int]] = {d.value: [] for d in ALL_DIMENSIONS}
    for dec in store.list_decisions():
        for a in dec.arbitrations:
            if a.final_score is not None:
                sums[a.dimension.value].append(a.final_score)
    return {k: (sum(v) / len(v) if v else None) for k, v in sums.items()}


def _card_cluster(store: TraceStore, c, crun) -> CardCluster:
    by_id = {b.trace_id: b for b in crun.badcases}
    reps: list[CardRepresentative] = []
    for r in c.representatives:
        b = by_id.get(r.trace_id)
        dec = store.get_decision(r.trace_id)
        panel = store.get_panel_result(r.trace_id)
        failed = [d.value for d in b.failed_dimensions] if b else []
        opinions: list[JudgeOpinion] = []
        if panel:
            for d in failed:
                for v in panel.verdicts_for(Dimension(d)):
                    opinions.append(JudgeOpinion(judge=v.judge, model=v.model, dimension=d, score=v.score,
                                                 label=v.label, rationale=v.rationale[:240]))
        reps.append(CardRepresentative(
            trace_id=r.trace_id, task_id=r.task_id, failed_dimensions=failed,
            final_scores=dec.scores if dec else {}, excerpt=r.excerpt, opinions=opinions,
            decision_sources={a.dimension.value: a.source.value + (" (degraded)" if a.degraded else "") for a in dec.arbitrations} if dec else {},
        ))
    return CardCluster(
        rank=c.rank, cluster_id=c.cluster_id, label=c.label, label_source=c.label_source,
        label_confidence=c.label_confidence, size=c.size, share=c.share, severity=c.severity, priority=c.priority,
        failed_dimension_counts=c.failed_dimension_counts, summary=c.summary, suggestion=c.suggestion,
        member_trace_ids=c.member_trace_ids, representatives=reps,
    )


def _recommendation(card: EvidenceCard) -> str:
    real = [c for c in card.clusters if c.cluster_id != -1]
    if not real:
        if card.totals.n_badcases == 0:
            return "No badcases found in this run. Nothing to fix; consider adding harder tasks."
        return "Badcases did not form clusters; review them individually."
    top = real[0]
    others = ", ".join(f"#{c.rank} {c.label.value} ({c.size})" for c in real[1:3])
    txt = (f"Fix cluster #1 first: {top.label.value}, {top.size} trace(s), {top.share:.0%} of badcases, "
           f"priority {top.priority:.1f}. {top.suggestion}")
    if others:
        txt += f" Then: {others}."
    txt += " After the change, re-run the same tasks and compare pass^3 with `sparkjury regress`."
    return txt


# ---- render -------------------------------------------------------------------


def render_json(card: EvidenceCard) -> str:
    return json.dumps(card.model_dump(mode="json"), ensure_ascii=False, indent=2)


def render_markdown(card: EvidenceCard) -> str:
    t, q = card.totals, card.quality
    L: list[str] = [f"# {card.title}", "", f"Run `{card.run_id}` | generated {card.generated_at}", ""]
    L += ["## Summary", "",
          "| | |", "|---|---|",
          f"| Traces | {t.n_traces} across {t.n_tasks} tasks |",
          f"| Environment failures (excluded) | {t.n_env_failures}" + (f" ({', '.join(f'{k}={v}' for k, v in t.env_kinds.items())})" if t.env_kinds else "") + " |",
          f"| Scored by the panel | {t.n_scored} |",
          f"| Badcases | {t.n_badcases} in {t.n_clusters} cluster(s) + {t.n_unclustered} unclustered |",
          f"| pass^1 | {_pct(q.pass_rate)} |"]
    for k, v in sorted(q.pass_k.items()):
        if k > 1:
            L.append(f"| pass^{k} | {_pct(v)} |")
    L += [f"| Judge agreement | {_pct(q.judge_agreement_rate)} ({q.n_needing_arbitration} traces arbitrated) |",
          f"| Decisions by source | {', '.join(f'{k}={v}' for k, v in sorted(q.decisions_by_source.items())) or '-'} |",
          f"| Degraded decisions | {q.n_degraded} |",
          f"| Audit | {q.n_audited} dimension(s) audited, {q.n_audit_disagreements} disagreement(s) |",
          f"| Mean final scores | {', '.join(f'{k} {_num(v)}' for k, v in q.mean_scores.items())} |",
          "", "## Recommendation", "", card.recommendation, "", f"> {card.disclaimer}", ""]
    L += ["## Clusters", ""]
    if not card.clusters:
        L.append("_No badcases._")
    for c in card.clusters:
        name = "Unclustered" if c.cluster_id == -1 else c.label.value
        L += [f"### #{c.rank} {name}", "",
              f"- Size {c.size} ({c.share:.0%} of badcases), severity {c.severity:.1f}, priority {c.priority:.1f}",
              f"- Failed dimensions: {', '.join(f'{k}x{v}' for k, v in c.failed_dimension_counts.items()) or '-'}",
              f"- Label source: {c.label_source}" + (f" (confidence {c.label_confidence:.2f})" if c.label_confidence is not None else ""),
              f"- Suggestion: {c.suggestion}", ""]
        for r in c.representatives:
            L += [f"**{r.trace_id}** (task {r.task_id}; failed {', '.join(r.failed_dimensions) or '-'}; "
                  f"scores {', '.join(f'{k}={v}' for k, v in r.final_scores.items())})", "", "```"]
            L += r.excerpt.splitlines() or ["(no excerpt)"]
            L += ["```"]
            for o in r.opinions:
                L.append(f"- {o.dimension} / {o.judge} [{o.model}]: {o.score}" + (f" {o.label}" if o.label else "") + f" - {o.rationale}")
            L.append("")
    L += ["---", f"Embedder {card.embedder or '-'}, clustering {card.cluster_method or '-'}."]
    return "\n".join(L)


_HTML_CSS = """
:root{--bg:#F6F7F3;--card:#fff;--ink:#1C2421;--ink2:#4C5751;--muted:#7C877F;--line:#DDE2DB;--acc:#2F7A3E;--acc2:#E3F1E4;--warn:#B8741A;--warn2:#FBF0DD;--code:#EEF1EC}
@media(prefers-color-scheme:dark){:root{--bg:#131816;--card:#1B221E;--ink:#E9EDE8;--ink2:#B9C2BB;--muted:#8A958D;--line:#2C3630;--acc:#6DBF78;--acc2:#1F3324;--warn:#E0A54A;--warn2:#3A2E18;--code:#10150F}}
body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.65 "Noto Sans SC","PingFang SC","Microsoft YaHei",system-ui,sans-serif}
.wrap{max-width:900px;margin:0 auto;padding:32px 20px 80px}h1{font-size:26px;margin:0 0 4px}.meta{color:var(--muted);font-size:13px;margin-bottom:20px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;margin:14px 0}
.tile{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:12px 14px}.tile b{display:block;font-size:22px;color:var(--acc)}.tile span{font-size:12.5px;color:var(--muted)}
.rec{background:var(--acc2);border-left:4px solid var(--acc);padding:14px 18px;border-radius:4px;margin:18px 0}.disc{background:var(--warn2);border-left:4px solid var(--warn);padding:10px 16px;border-radius:4px;color:var(--ink2);font-size:13.5px}
h2{font-size:20px;margin:30px 0 8px}.cl{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:16px 20px;margin-top:14px}
.cl h3{margin:0 0 6px;font-size:17px}.pill{display:inline-block;font-size:11.5px;padding:1px 8px;border-radius:10px;background:var(--acc2);color:var(--acc);margin-left:8px}
.kv{color:var(--ink2);font-size:14px;margin:2px 0}.rep{margin-top:12px;padding-top:10px;border-top:1px dashed var(--line)}
pre{background:var(--code);border:1px solid var(--line);border-radius:6px;padding:10px 12px;overflow-x:auto;font:12.5px/1.5 Consolas,monospace;white-space:pre-wrap}
ul{margin:6px 0;padding-left:18px}li{font-size:13.5px;color:var(--ink2)}table{border-collapse:collapse;width:100%;font-size:14px}td,th{text-align:left;padding:6px 8px;border-bottom:1px solid var(--line)}th{color:var(--muted);font-size:12.5px}
"""


def render_html(card: EvidenceCard) -> str:
    e = html.escape
    t, q = card.totals, card.quality
    tiles = [
        (t.n_traces, "traces"), (t.n_env_failures, "environment failures"), (t.n_badcases, "badcases"),
        (t.n_clusters, "clusters"), (_pct(q.pass_rate), "pass^1"),
    ]
    for k, v in sorted(q.pass_k.items()):
        if k > 1:
            tiles.append((_pct(v), f"pass^{k}"))
    tiles += [(_pct(q.judge_agreement_rate), "judge agreement"), (q.n_degraded, "degraded decisions")]
    H = [f"<!DOCTYPE html><html lang='zh-CN'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>",
         f"<title>{e(card.title)}</title><style>{_HTML_CSS}</style></head><body><div class='wrap'>",
         f"<h1>{e(card.title)}</h1><div class='meta'>run {e(card.run_id)} · {e(card.generated_at)} · agent {e(q.agent_model or '-')}</div>",
         "<div class='grid'>" + "".join(f"<div class='tile'><b>{e(str(a))}</b><span>{e(b)}</span></div>" for a, b in tiles) + "</div>",
         f"<div class='rec'><b>Recommendation</b><br>{e(card.recommendation)}</div>",
         f"<div class='disc'>{e(card.disclaimer)}</div>",
         "<h2>Quality</h2><table><tr><th>Mean final score</th>" + "".join(f"<td>{e(k)} {e(_num(v))}</td>" for k, v in q.mean_scores.items()) + "</tr>",
         f"<tr><th>Decisions by source</th><td colspan='4'>{e(', '.join(f'{k}={v}' for k, v in sorted(q.decisions_by_source.items())) or '-')}</td></tr>",
         f"<tr><th>Arbitrated traces</th><td colspan='4'>{q.n_needing_arbitration}</td></tr>",
         f"<tr><th>Audit</th><td colspan='4'>{q.n_audited} dimension(s), {q.n_audit_disagreements} disagreement(s)</td></tr>",
         f"<tr><th>Environment failures</th><td colspan='4'>{e(', '.join(f'{k}={v}' for k, v in t.env_kinds.items()) or '-')}</td></tr></table>",
         "<h2>Clusters</h2>"]
    if not card.clusters:
        H.append("<p>No badcases.</p>")
    for c in card.clusters:
        name = "Unclustered" if c.cluster_id == -1 else c.label.value
        H.append(f"<div class='cl'><h3>#{c.rank} {e(name)}<span class='pill'>{c.size} · {c.share:.0%}</span><span class='pill'>priority {c.priority:.1f}</span></h3>")
        H.append(f"<div class='kv'>Failed dimensions: {e(', '.join(f'{k}x{v}' for k, v in c.failed_dimension_counts.items()) or '-')} · severity {c.severity:.1f} · label from {e(c.label_source)}</div>")
        H.append(f"<div class='kv'><b>Suggestion:</b> {e(c.suggestion)}</div>")
        for r in c.representatives:
            H.append(f"<div class='rep'><b>{e(r.trace_id)}</b> <span class='kv'>task {e(r.task_id)} · failed {e(', '.join(r.failed_dimensions) or '-')} · scores {e(', '.join(f'{k}={v}' for k, v in r.final_scores.items()))}</span>")
            H.append(f"<pre>{e(r.excerpt or '(no excerpt)')}</pre>")
            if r.opinions:
                H.append("<ul>" + "".join(f"<li>{e(o.dimension)} · {e(o.judge)} [{e(o.model)}]: {o.score}{' ' + e(o.label) if o.label else ''} — {e(o.rationale)}</li>" for o in r.opinions) + "</ul>")
            H.append("</div>")
        H.append("</div>")
    H.append(f"<p class='meta' style='margin-top:30px'>Embedder {e(card.embedder or '-')}, clustering {e(card.cluster_method or '-')}.</p></div></body></html>")
    return "\n".join(H)


def write_card(card: EvidenceCard, out_dir: str | Path, basename: str = "card", formats: tuple[str, ...] = ("json", "md", "html")) -> list[Path]:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    if "json" in formats:
        p = out / f"{basename}.json"; p.write_text(render_json(card), encoding="utf-8"); written.append(p)
    if "md" in formats:
        p = out / f"{basename}.md"; p.write_text(render_markdown(card), encoding="utf-8"); written.append(p)
    if "html" in formats:
        p = out / f"{basename}.html"; p.write_text(render_html(card), encoding="utf-8"); written.append(p)
    return written


def _pct(v: float | None) -> str:
    return "-" if v is None else f"{v * 100:.1f}%"


def _num(v: float | None) -> str:
    return "-" if v is None else f"{v:.2f}"
