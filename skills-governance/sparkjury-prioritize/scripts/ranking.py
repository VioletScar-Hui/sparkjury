#!/usr/bin/env python3
"""ranking.py — 优先级公式与排序（prioritize.py 的拆分模块）。

priority = frequency_norm × severity_weight × fixability_boost，三个系数全部来自
pack thresholds.prioritize，缺一个就拒排并写 reject_reason —— 不夹逼、不猜默认值。

排序键刻意定为 `(-priority, category_id)`：同分时按 category_id 字典序，
保证同输入两次运行结果完全一致（override 幂等性靠这个，_selftest 有 sha256 断言）。

分母口径：含 F99 在内的全部 badcase（与 cluster 侧 share 一致）。此处若退化成
"只累加 clusters 数组的 size"，同一类会在两个 skill 里打印出两个不同的百分比。

模块划分见 pack_binding.py 顶部说明。
"""
from __future__ import annotations

import sys
from pathlib import Path

# YAML 读取统一走项目唯一权威实现 tools/_yaml_lite.py（2026-09-26 收敛，删除了本 skill
# 私有的 _miniyaml.py 副本）。本模块内只出现 YamlLiteError 这一种写法，不再设别名 ——
# 别名会让"这里抛的到底是哪个解析器的错"在一次 grep 里看不出来。
# sparkjury 适配（2026-09-27 移植）：eval-agent 的根工具在 scripts/，sparkjury 在 tools/。
PROJECT_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(PROJECT_ROOT / "tools"))

from _yaml_lite import YamlLiteError, load_yaml_file  # noqa: E402

from overrides import apply_overrides  # noqa: E402

# pack v0.1 的 fixability_boost 是人工先验（pack 注释原文："可修复性先验，v0.1 人工设定，黄金池校准"）。
# 这个来源标签必须出现在每条输出里，防止读者把先验当成校准过的结论。
FIXABILITY_SOURCE = "human_prior_pack_v0.1_uncalibrated"


def load_thresholds(path) -> dict:
    """prioritize 侧的薄包装：读阈值文件。解析权在权威模块，本函数只保留调用点形状。

    签名保持 (path) -> dict 不变：_selftest.py 里的 `impl.load_thresholds(tp)` 与 run()
    都不必改。空文档 / 顶层非 mapping / 文件不存在都按 _yaml_lite 的语义来。
    """
    return load_yaml_file(path)


def compute_priority(cluster: dict, thresholds: dict, total_badcases: int | None = None) -> dict:
    """priority = frequency_norm × severity_weight × fixability_boost（系数全部来自 pack）。

    返回 ok=True 时含 priority/frequency/severity/fixability/rationale；
    ok=False 时含 reject_reason —— 系数缺失一律拒排，不补默认值。
    """
    p = thresholds.get("prioritize")
    if not isinstance(p, dict):
        return {"ok": False, "reject_reason": "pack 缺 thresholds.prioritize 段"}
    cid = str(cluster.get("label"))
    severity = cluster.get("severity_max")
    total = total_badcases if total_badcases is not None else cluster.get("total_badcases")
    count = cluster.get("size")

    base = {
        "category_id": cid,
        "category_name": cluster.get("category_name"),
        "size": count,
        "severity": severity,
        "trace_count": len(cluster.get("trace_ids") or []),
    }

    if not isinstance(total, int) or total <= 0:
        return {**base, "ok": False,
                "reject_reason": f"总 badcase 数不可用（{total!r}），frequency_norm 无分母"}
    if not isinstance(count, int) or count <= 0:
        return {**base, "ok": False, "reject_reason": f"类内 badcase 数不可用（{count!r}）"}

    sev_weights = p.get("severity_weights") or {}
    skey = str(severity)
    if skey not in sev_weights:
        return {**base, "ok": False,
                "reject_reason": f"severity_weight 未在 pack 定义（severity_max={severity}，"
                                 f"pack 仅定义 {sorted(sev_weights)}）；不夹逼、不猜"}

    boosts = p.get("fixability_boost") or {}
    if cid not in boosts:
        return {**base, "ok": False,
                "reject_reason": f"fixability_boost 未在 pack 定义 category_id={cid}；"
                                 "缺系数的类不可排序，须先补 pack（govern 提案）"}

    frequency_norm = count / total
    severity_weight = float(sev_weights[skey])
    fixability = float(boosts[cid])
    priority = frequency_norm * severity_weight * fixability

    rationale = (
        f"frequency_norm={frequency_norm:.4f} ({count}/{total}) "
        f"× severity_weight={severity_weight} (severity_max={severity}) "
        f"× fixability_boost={fixability} ({cid}) "
        f"= {priority:.4f}"
    )
    return {
        **base,
        "ok": True,
        "priority": round(priority, 6),
        "frequency": round(frequency_norm, 6),
        "frequency_count": count,
        "fixability": fixability,
        "fixability_source": FIXABILITY_SOURCE,
        "severity_weight": severity_weight,
        "rationale": rationale,
    }


def rank(clusters: list, thresholds: dict, overrides: list | None = None,
         total_badcases: int | None = None) -> dict:
    """纯函数：clusters + pack (+ override 历史) → 排序结果。"""
    # 分母与 cluster 侧 share 口径一致：含 F99 在内的全部 badcase。
    # 调用方显式传 total_badcases（如 cluster_report.total_badcases）时优先用调用方的。
    total = total_badcases if total_badcases is not None else sum(
        c.get("size") or 0 for c in clusters)

    p = thresholds.get("prioritize") or {}
    top_n = p.get("top_recommendation_count", 1)
    if not isinstance(top_n, int) or top_n < 1:
        raise YamlLiteError(f"prioritize.top_recommendation_count 非法: {top_n!r}")

    scored, rejected = [], []
    for c in sorted(clusters, key=lambda x: str(x.get("label"))):
        r = compute_priority(c, thresholds, total)
        if r["ok"]:
            scored.append(r)
        else:
            rejected.append({"category_id": r["category_id"],
                             "category_name": r.get("category_name"),
                             "reason": r["reject_reason"]})

    # 排序键：priority 降序 → category_id 升序（保证幂等）
    scored.sort(key=lambda r: (-r["priority"], r["category_id"]))
    for i, r in enumerate(scored, 1):
        r["rank"] = i

    applied, unmatched = [], []
    if overrides:
        scored, applied, unmatched = apply_overrides(scored, overrides)
        for i, r in enumerate(scored, 1):
            r["rank"] = i
        for rec in applied:
            item = next(r for r in scored if r["category_id"] == rec["category_id"])
            item["rationale"] += (
                f"；按历史人工决策修正（ledger event {rec['event_id']}，"
                f"rank {rec['from_rank']}→{rec['to_rank']}，actor={rec['actor']}）"
            )
            item["override_applied"] = rec

    top = scored[:top_n]
    top_rec = None
    if top:
        t = top[0]
        top_rec = {
            "category_id": t["category_id"],
            "one_line_reason": (
                f"{t['category_name'] or t['category_id']}：{t['size']} 条 badcase"
                f"（占 {t['frequency'] * 100:.1f}%），severity_max={t['severity']}，"
                f"pack 标记可修复性 {t['fixability']} → priority={t['priority']:.4f}"
            ),
            "priority": t["priority"],
        }
    return {"ranked": scored, "top_recommendation": top_rec,
            "rejected_candidates": rejected, "override_applied": applied,
            "override_unmatched": unmatched,
            "total_badcases": total, "top_recommendation_count": top_n}
