"""Before/after regression: pass^k, per-task flips, cluster label shifts, optional pairwise judging."""

from __future__ import annotations

from collections import Counter
from pathlib import Path

from sparkjury.judges.pairwise import PairwiseJudge, compare_with_swap
from sparkjury.models.regress import ClusterChange, PairwiseResult, RegressionReport, TaskChange
from sparkjury.models.trace import Trace
from sparkjury.models.verdict import ALL_DIMENSIONS
from sparkjury.store import TraceStore


def _task_outcomes(store: TraceStore) -> dict[str, list[bool | None]]:
    out: dict[str, list[bool | None]] = {}
    for t in store.list():
        out.setdefault(t.task_id, []).append(t.outcome.success)
    return out


def _mean_scores(store: TraceStore) -> dict[str, float | None]:
    acc: dict[str, list[int]] = {d.value: [] for d in ALL_DIMENSIONS}
    for dec in store.list_decisions():
        for a in dec.arbitrations:
            if a.final_score is not None:
                acc[a.dimension.value].append(a.final_score)
    return {k: (sum(v) / len(v) if v else None) for k, v in acc.items()}


def _cluster_counts(store: TraceStore) -> Counter[str]:
    run = store.get_cluster_run()
    c: Counter[str] = Counter()
    if run:
        for cl in run.clusters:
            if cl.cluster_id != -1:
                c[cl.label.value] += cl.size
    return c


def compare(
    before_db: str | Path,
    after_db: str | Path,
    *,
    pairwise: PairwiseJudge | None = None,
    before_label: str | None = None,
    after_label: str | None = None,
) -> RegressionReport:
    with TraceStore(before_db) as b, TraceStore(after_db) as a:
        ob, oa = _task_outcomes(b), _task_outcomes(a)
        common = sorted(set(ob) & set(oa))
        k = max(1, min([len(ob[t]) for t in common] + [len(oa[t]) for t in common])) if common else 1

        def pass_rate(o: dict[str, list[bool | None]]) -> float | None:
            vals = [x for t in common for x in o[t] if x is not None]
            return (sum(vals) / len(vals)) if vals else None

        def pass_k(o: dict[str, list[bool | None]]) -> float | None:
            el = [t for t in common if len(o[t]) >= k and all(x is not None for x in o[t][:k])]
            return (sum(all(o[t][:k]) for t in el) / len(el)) if el else None

        pb, pa, kb, ka = pass_rate(ob), pass_rate(oa), pass_k(ob), pass_k(oa)
        fixed, broken = [], []
        for t in common:
            xb, xa = ob[t][:k], oa[t][:k]
            if any(x is None for x in xb + xa):
                continue
            sb, sa = all(xb), all(xa)
            ch = TaskChange(task_id=t, before="pass" if sb else "fail", after="pass" if sa else "fail",
                            before_pass_trials=sum(xb), after_pass_trials=sum(xa), n_trials=k)
            if not sb and sa:
                fixed.append(ch)
            elif sb and not sa:
                broken.append(ch)

        cb, ca = _cluster_counts(b), _cluster_counts(a)
        cluster_changes = [ClusterChange(label=l, before=cb.get(l, 0), after=ca.get(l, 0), delta=ca.get(l, 0) - cb.get(l, 0))
                           for l in sorted(set(cb) | set(ca))]
        nb = sum(1 for _ in b.list_badcases()) if b.get_cluster_run() else 0
        na = sum(1 for _ in a.list_badcases()) if a.get_cluster_run() else 0

        report = RegressionReport(
            before_label=before_label or str(before_db), after_label=after_label or str(after_db),
            n_tasks_common=len(common), k=k, pass_rate_before=pb, pass_rate_after=pa, pass_k_before=kb, pass_k_after=ka,
            delta_pass_rate=(pa - pb) if (pa is not None and pb is not None) else None,
            delta_pass_k=(ka - kb) if (ka is not None and kb is not None) else None,
            mean_scores_before=_mean_scores(b), mean_scores_after=_mean_scores(a),
            n_badcases_before=nb, n_badcases_after=na, fixed_tasks=fixed, broken_tasks=broken, cluster_changes=cluster_changes,
        )
        if pairwise is not None:
            report.pairwise, report.pairwise_summary = _pairwise(b, a, common, pairwise)
    return report


def _pairwise(b: TraceStore, a: TraceStore, common: list[str], judge: PairwiseJudge) -> tuple[list[PairwiseResult], dict[str, int]]:
    results: list[PairwiseResult] = []
    summary: Counter[str] = Counter()
    for t in common:
        tb = {x.trial: x for x in b.list(task_id=t)}
        ta = {x.trial: x for x in a.list(task_id=t)}
        for trial in sorted(set(tb) & set(ta)):
            winner, consistent, why = compare_with_swap(judge, tb[trial], ta[trial])
            results.append(PairwiseResult(task_id=t, trial=trial, before_trace_id=tb[trial].trace_id, after_trace_id=ta[trial].trace_id,
                                          winner=winner, consistent=consistent, rationale=why))
            summary[winner if consistent else "inconsistent"] += 1
    return results, dict(summary)


def render_markdown(r: RegressionReport) -> str:
    L = [f"# Regression report: {r.verdict}", "", f"Before `{r.before_label}` -> after `{r.after_label}` | {r.n_tasks_common} common tasks | generated {r.generated_at}", "",
         "| Metric | Before | After | Delta |", "|---|---|---|---|",
         f"| pass^1 | {_pct(r.pass_rate_before)} | {_pct(r.pass_rate_after)} | {_delta(r.delta_pass_rate)} |"]
    if r.k > 1:
        L.append(f"| pass^{r.k} (all {r.k} trials pass) | {_pct(r.pass_k_before)} | {_pct(r.pass_k_after)} | {_delta(r.delta_pass_k)} |")
    L += [f"| badcases | {r.n_badcases_before} | {r.n_badcases_after} | {r.n_badcases_after - r.n_badcases_before:+d} |"]
    for d in r.mean_scores_before:
        mb, ma = r.mean_scores_before.get(d), r.mean_scores_after.get(d)
        L.append(f"| mean {d} | {_num(mb)} | {_num(ma)} | {_delta(ma - mb, pct=False) if (mb is not None and ma is not None) else '-'} |")
    L += ["", "## Tasks fixed (fail -> pass)", ""] + ([f"- {t.task_id}: {t.before_pass_trials}/{t.n_trials} -> {t.after_pass_trials}/{t.n_trials} trials passed" for t in r.fixed_tasks] or ["_none_"])
    L += ["", "## Tasks broken (pass -> fail)", ""] + ([f"- {t.task_id}: {t.before_pass_trials}/{t.n_trials} -> {t.after_pass_trials}/{t.n_trials} trials passed" for t in r.broken_tasks] or ["_none_"])
    L += ["", "## Cluster changes", ""]
    if r.cluster_changes:
        L += ["| Label | Before | After | Delta |", "|---|---|---|---|"] + [f"| {c.label} | {c.before} | {c.after} | {c.delta:+d} |" for c in r.cluster_changes]
    else:
        L.append("_no clusters on either side_")
    if r.pairwise:
        L += ["", "## Pairwise (order-swapped, both passes must agree)", "",
              f"after wins {r.pairwise_summary.get('after', 0)}, before wins {r.pairwise_summary.get('before', 0)}, ties {r.pairwise_summary.get('tie', 0)}, inconsistent {r.pairwise_summary.get('inconsistent', 0)}", ""]
        L += [f"- {p.task_id} trial {p.trial}: {p.winner}{'' if p.consistent else ' (inconsistent)'} - {p.rationale}" for p in r.pairwise]
    return "\n".join(L)


def _pct(v: float | None) -> str:
    return "-" if v is None else f"{v * 100:.1f}%"


def _num(v: float | None) -> str:
    return "-" if v is None else f"{v:.2f}"


def _delta(v: float | None, pct: bool = True) -> str:
    if v is None:
        return "-"
    return f"{v * 100:+.1f} pp" if pct else f"{v:+.2f}"
