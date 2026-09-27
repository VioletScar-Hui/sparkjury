#!/usr/bin/env python3
"""pipeline.py — prioritize 的 run() 编排与 IO 助手（prioritize.py 的拆分模块）。

职责：读输入 → 排优先级 → 落 priority.json + run-manifest.json → 追加 ledger 事件。
ledger 事件顺序固定（started → checkpoint → completed → 可选 degraded），
`override_unmatched` 恒存在于 meta（空数组 = 没有未生效的人工决策）——
缺这个键则下游无法区分「没有」和「没记」。

模块划分见 pack_binding.py 顶部说明。
"""
from __future__ import annotations

import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

# 保证 `python3 /任意/路径/pipeline.py` 也能 import 同目录模块
sys.path.insert(0, str(Path(__file__).resolve().parent))

from overrides import load_overrides  # noqa: E402
from pack_binding import resolve_pack_binding  # noqa: E402
from ranking import load_thresholds, rank  # noqa: E402

SKILL_NAME = "prioritize"
SKILL_VERSION = "0.1.0"


def sha256_obj(obj) -> str:
    return hashlib.sha256(
        json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def write_json(path: Path, obj) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(obj, ensure_ascii=False, indent=2) + "\n"
    path.write_text(payload, encoding="utf-8")
    return sha256_obj(json.loads(payload))


def emit(ledger_path, events: list) -> None:
    if not ledger_path:
        return
    p = Path(ledger_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as fh:
        for e in events:
            fh.write(json.dumps(e, ensure_ascii=False) + "\n")


def resolve_total_badcases(clusters_path: Path, clusters: list, explicit: int | None) -> tuple:
    """确定 frequency_norm 的分母，返回 (总数, 来源)。

    必须与 cluster 侧 share 口径一致（cluster 的 share = size / total_badcases，
    分母含 F99）。若此处退化成"只累加 clusters 数组的 size"，同一类在两个 skill 里
    会打印出两个不同的百分比 —— 这是会被人当成 bug 报上来的不一致。
    优先级：显式入参 > 同目录 cluster_report.json > 累加 clusters。
    """
    if isinstance(explicit, int) and explicit > 0:
        return explicit, "explicit_arg"
    report = clusters_path.parent / "cluster_report.json"
    if report.is_file():
        try:
            doc = json.loads(report.read_text(encoding="utf-8"))
            n = doc.get("total_badcases")
            if isinstance(n, int) and n > 0:
                return n, f"cluster_report.json ({report.name})"
        except (json.JSONDecodeError, OSError):
            pass
    return sum(c.get("size") or 0 for c in clusters), "sum_of_cluster_sizes"


def run(clusters_path: Path, thresholds_path: Path, out_dir: Path, run_id: str,
        pack_hash: str | None, ledger: str | None, overrides_path: Path | None,
        total_badcases: int | None = None, pack_dir: Path | None = None,
        pack_id: str | None = None, pack_version: str | None = None) -> int:
    thresholds = load_thresholds(thresholds_path)
    doc = json.loads(clusters_path.read_text(encoding="utf-8"))
    clusters = doc.get("clusters", doc) if isinstance(doc, dict) else doc
    if not isinstance(clusters, list):
        raise ValueError(f"{clusters_path}: 期望 clusters 数组或含 clusters 键的对象")
    overrides = load_overrides(overrides_path) if overrides_path else []
    run_id = run_id or f"run-{sha256_obj([c.get('cluster_id') for c in clusters])[:12]}"

    total, total_src = resolve_total_badcases(clusters_path, clusters, total_badcases)

    emit(ledger, [{"event_id": f"{run_id}-prioritize-started", "ts": now_iso(), "run_id": run_id,
                   "skill": SKILL_NAME, "event_type": "started",
                   "payload": {"input": str(clusters_path), "clusters": len(clusters),
                               "override_events": len(overrides),
                               "total_badcases": total, "total_source": total_src}}])

    result = rank(clusters, thresholds, overrides, total)

    out = {
        "ranked": result["ranked"],
        "top_recommendation": result["top_recommendation"],
        "rejected_candidates": result["rejected_candidates"],
        "meta": {
            "formula": (thresholds.get("prioritize") or {}).get("formula"),
            "total_badcases": result["total_badcases"],
            "total_badcases_source": total_src,
            "top_recommendation_count": result["top_recommendation_count"],
            "override_applied": result["override_applied"],
            # 恒存在（无 override 时为 []）：空数组 = 没有未生效的人工决策，
            # 缺这个键则下游无法区分"没有"和"没记"。
            "override_unmatched": result["override_unmatched"],
            "disclaimer": (
                "fixability_boost 为 pack v0.1 人工先验（未经黄金池校准）；"
                "优先级只表达『先修哪一类』的相对顺序，不承诺修复成本与收益的绝对值。"
            ),
        },
    }
    h = write_json(out_dir / "priority.json", out)
    binding = resolve_pack_binding(pack_dir, pack_hash, pack_id, pack_version)
    manifest = {
        "run_id": run_id,
        "trace_version": "trace.v1",
        "pack": binding["pack"],
        "skill_versions": {SKILL_NAME: SKILL_VERSION},
        "degraded_flags": sorted(set(binding["flags"])),
        "outputs": [{"skill": SKILL_NAME, "artifact": "priority.json", "hash": h,
                     "records": len(out["ranked"])}],
    }
    write_json(out_dir / "run-manifest.json", manifest)

    emit(ledger, [
        {"event_id": f"{run_id}-prioritize-checkpoint", "ts": now_iso(), "run_id": run_id,
         "skill": SKILL_NAME, "event_type": "checkpoint",
         "payload": {"artifact": "priority.json", "hash": h, "resumable_from": True}},
        {"event_id": f"{run_id}-prioritize-completed", "ts": now_iso(), "run_id": run_id,
         "skill": SKILL_NAME, "event_type": "completed",
         "payload": {"ranked": len(out["ranked"]), "rejected": len(out["rejected_candidates"]),
                     "top": (out["top_recommendation"] or {}).get("category_id"),
                     "override_applied": len(result["override_applied"]),
                     "override_unmatched": len(result["override_unmatched"])}},
    ])
    if manifest["degraded_flags"]:
        emit(ledger, [{"event_id": f"{run_id}-prioritize-degraded", "ts": now_iso(),
                       "run_id": run_id, "skill": SKILL_NAME, "event_type": "degraded",
                       "payload": {"flags": manifest["degraded_flags"],
                                   "reason": "pack 身份未与真实 pack.manifest.json 绑定或非 FROZEN态"}}])

    tr = out["top_recommendation"] or {}
    print(f"[prioritize] ranked={len(out['ranked'])} rejected={len(out['rejected_candidates'])} "
          f"total_badcases={result['total_badcases']}")
    print(f"[prioritize] pack={binding['pack']['pack_id']} v{binding['pack']['version']} "
          f"state_at_run={binding['pack']['state_at_run']} bound={binding['pack']['pack_bound']}")
    print(f"[prioritize] top_recommendation={tr.get('category_id')} :: {tr.get('one_line_reason')}")
    if result["override_applied"]:
        print(f"[prioritize] override applied: {[a['category_id'] for a in result['override_applied']]}")
    for u in result["override_unmatched"]:
        # debug 级：不阻断、不静默。缺 taxonomy_id 或类不在本轮，都要能被人看见
        print(f"[prioritize][{u['level']}] override 未生效 {u['event_id']}: "
              f"{u['reason']} — {u['detail']}", file=sys.stderr)
    print(f"[prioritize] wrote {out_dir}/priority.json run-manifest.json")
    return 0
