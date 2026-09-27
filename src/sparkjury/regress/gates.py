"""回归门禁（M6）：pack 身份、提升阈值、新严重簇，三条一起给判定。

以前这三条只活在 `tools/verify.sh` 的人工检查里——`sparkjury regress` 自己不校验 pack hash、没有
提升阈值、新冒出来的严重簇也不阻断，「铁律」全靠带外人工卡。这里把它们变成 regress 自己会算的东西，
阈值读 pack（`standards/scenario-pack/thresholds.yaml` 的 `regress:` 段），不硬编码。

三种结果：

- `REFUSED`（退出码 2）：两侧 store 记录的 pack hash 不同。这不是回归对比，是拿两套标准评了两轮，
  继续出报告只会得到一张好看的、没有意义的表。
- `FAIL`（退出码 1）：能对比，但门禁没过（主指标提升不够 / 出现新的高严重簇）。
- `PASS`（退出码 0）：门禁全过。被跳过的项不算通过，「跳过」会在 reasons 里写清为什么。

pack hash 从哪来：run 的 `manifest.json` 里新加的 `pack_hash` 字段（`Orchestrator` 落盘时写）。
它就在 `<db 的父目录>/manifest.json`，因为 store 的规矩是「一个 run 一个目录」；老 store 没有这个
文件，或者 manifest 里还没有这个字段时，判定身份这件事降级成 skip，而不是拒绝——否则所有历史库都
变成不可对比的了。注意不去读 SQLite 的 runs 表：那里面除了真 run，还有 `cluster:latest` 这类伪 run。
"""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel, Field

from sparkjury import pack as pack_mod
from sparkjury.models.cluster import ClusterRun
from sparkjury.models.regress import RegressionReport
from sparkjury.store import TraceStore

REFUSED_EXIT = 2
FAIL_EXIT = 1
PASS_EXIT = 0

#: pack 的 `regress.gates.primary_metric` 名字 -> RegressionReport 上的字段
_METRIC_FIELDS = {"task_pass_rate": "delta_pass_k", "pass_rate": "delta_pass_rate"}
_EPS = 1e-9


class GateItem(BaseModel):
    """一条门禁。`source` 说明阈值是哪来的：pack / default / cli。"""

    name: str
    status: str          # pass | fail | skip
    detail: str
    source: str = "pack"


class GateReport(BaseModel):
    verdict: str         # PASS | FAIL | REFUSED
    exit_code: int
    pack_hash_before: str | None = None
    pack_hash_after: str | None = None
    same_pack: bool | None = None
    items: list[GateItem] = Field(default_factory=list)

    @property
    def failures(self) -> list[GateItem]:
        return [i for i in self.items if i.status == "fail"]

    def reasons(self) -> list[str]:
        return [f"{i.name}: {i.detail}" for i in self.items]


def manifest_pack_hash(db_path: str | Path) -> str | None:
    """`<db 的父目录>/manifest.json` 里的 pack_hash；没有就 None。"""
    p = Path(db_path)
    m = p.parent / "manifest.json"
    if not m.is_file():
        return None
    try:
        data = json.loads(m.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    h = data.get("pack_hash")
    return str(h) if h else None


def load_cluster_run(db_path: str | Path) -> ClusterRun | None:
    try:
        with TraceStore(db_path) as store:
            return store.get_cluster_run()
    except Exception:  # noqa: BLE001 - 没有库/没有簇都按「没有」算，门禁会 skip
        return None


def new_severe_clusters(before: ClusterRun | None, after: ClusterRun | None, severe_min: float) -> list[tuple[str, float, int]]:
    """after 里新冒出来、且 severity 达到阈值的簇。返回 [(label, severity, size)]。

    「新」= 这个标签在 before 的簇里一条都没有（标签整体没出现过）；标签内部条数变多不算新类目，
    那是同一类问题变多，归主指标和簇变化表去讲。
    """
    if after is None:
        return []
    had = {c.label.value for c in (before.clusters if before else []) if c.cluster_id != -1 and c.size > 0}
    out: list[tuple[str, float, int]] = []
    for c in after.clusters:
        if c.cluster_id == -1 or c.size <= 0 or c.label.value in had:
            continue
        if c.severity + _EPS >= severe_min:
            out.append((c.label.value, round(float(c.severity), 3), c.size))
    return sorted(out, key=lambda x: (-x[1], x[0]))


def _primary_delta(report: RegressionReport, metric: str) -> float | None:
    field = _METRIC_FIELDS.get(metric)
    if field is None:
        return None
    v = getattr(report, field, None)
    return None if v is None else float(v)


def evaluate_gates(report: RegressionReport | None, *, before_db: str | Path, after_db: str | Path,
                   pack: str | Path | None = None, severe_min: float | None = None,
                   delta_min: float | None = None, pack_hash_before: str | None = None,
                   pack_hash_after: str | None = None) -> GateReport:
    """算三关门禁。`report` 为 None 时只判 pack 身份（`regress` 在对比之前就要知道能不能比）。"""
    policy = pack_mod.regress_policy(pack)
    gates_cfg = policy.get("gates") or {}
    items: list[GateItem] = []

    hb = pack_hash_before if pack_hash_before is not None else manifest_pack_hash(before_db)
    ha = pack_hash_after if pack_hash_after is not None else manifest_pack_hash(after_db)
    same: bool | None = None
    if hb and ha:
        same = hb == ha
    require_same = bool(policy.get("same_pack_hash_required", True))

    if same is False:
        if require_same:
            items.append(GateItem(
                name="pack_identity", status="fail", source="pack",
                detail=f"pack hash differs ({hb[:16]} vs {ha[:16]}): a different pack is a new evaluation round, not a regression - refusing to compare",
            ))
            return GateReport(verdict="REFUSED", exit_code=REFUSED_EXIT, pack_hash_before=hb, pack_hash_after=ha,
                              same_pack=False, items=items)
        items.append(GateItem(
            name="pack_identity", status="skip", source="pack",
            detail=f"pack hash differs ({hb[:16]} vs {ha[:16]}) but thresholds.regress.same_pack_hash_required=false allows cross-pack comparison",
        ))
    elif same is True:
        items.append(GateItem(name="pack_identity", status="pass", source="pack",
                              detail=f"both sides ran the same pack: {hb[:16]}"))
    else:
        missing = "before" if not hb else "after"
        items.append(GateItem(
            name="pack_identity", status="skip", source="pack",
            detail=f"no pack_hash in the {missing} side's manifest.json (old run, or a db outside a run dir): cannot confirm both ran the same pack",
        ))

    if report is None:
        verdict = "FAIL" if any(i.status == "fail" for i in items) else "PASS"
        return GateReport(verdict=verdict, exit_code=(FAIL_EXIT if verdict == "FAIL" else PASS_EXIT),
                          pack_hash_before=hb, pack_hash_after=ha, same_pack=same, items=items)

    metric = str(gates_cfg.get("primary_metric") or "task_pass_rate")
    delta = _primary_delta(report, metric)
    if delta_min is not None:
        dm, dm_src = float(delta_min), "cli"
    elif gates_cfg.get("delta_min") is not None:
        dm, dm_src = float(gates_cfg["delta_min"]), "pack"
    else:
        dm, dm_src = pack_mod.DEFAULT_DELTA_MIN, "default"
    if delta is None:
        items.append(GateItem(name="primary_metric", status="skip", source=dm_src,
                              detail=f"{metric}=None (no comparable gold outcomes in this round): delta_min={dm} cannot be judged"))
    elif delta + _EPS < dm:
        items.append(GateItem(name="primary_metric", status="fail", source=dm_src,
                              detail=f"{metric} improved {delta * 100:+.1f} pp < delta_min {dm} ({dm * 100:.0f} pp): "
                                     f"not enough improvement to call it a regression fix - FAIL"))
    else:
        items.append(GateItem(name="primary_metric", status="pass", source=dm_src,
                              detail=f"{metric} improved {delta * 100:+.1f} pp >= delta_min {dm} ({dm * 100:.0f} pp); verdict={report.verdict}"))

    if not gates_cfg.get("new_severe_cluster_blocks", True):
        items.append(GateItem(name="new_severe_cluster", status="skip", source="pack",
                              detail="thresholds.regress.gates.new_severe_cluster_blocks=false"))
    else:
        if severe_min is not None:
            smin, smin_src = float(severe_min), "cli"
        elif gates_cfg.get("new_severe_severity") is not None:
            smin, smin_src = float(gates_cfg["new_severe_severity"]), "pack"
        else:
            smin, smin_src = pack_mod.DEFAULT_SEVERE_SEVERITY, "default"
        new_sev = new_severe_clusters(load_cluster_run(before_db), load_cluster_run(after_db), smin)
        if new_sev:
            detail = "、".join(f"{lab} (severity {sev}, {size} 条)" for lab, sev, size in new_sev)
            items.append(GateItem(name="new_severe_cluster", status="fail", source=smin_src,
                                  detail=f"new high-severity cluster(s) appeared (severity >= {smin}): {detail}"))
        else:
            items.append(GateItem(name="new_severe_cluster", status="pass", source=smin_src,
                                  detail=f"no new cluster at severity >= {smin}"))

    verdict = "FAIL" if any(i.status == "fail" for i in items) else "PASS"
    return GateReport(verdict=verdict, exit_code=(FAIL_EXIT if verdict == "FAIL" else PASS_EXIT),
                      pack_hash_before=hb, pack_hash_after=ha, same_pack=same, items=items)


def render_gates(g: GateReport, *, prefix: str = "  ") -> str:
    """给人看的多行门禁输出：每行一条，末尾写清跳过原因。"""
    mark = {"pass": "PASS", "fail": "FAIL", "skip": "SKIP"}
    lines = [f"gates {g.verdict}"]
    for i in g.items:
        lines.append(f"{prefix}[{mark.get(i.status, i.status)}] {i.name} ({i.source}) {i.detail}")
    return "\n".join(lines)
