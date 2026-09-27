#!/usr/bin/env python3
"""_metrics.py — calibrate 的可靠性指标层（六个指标 + 主票筛选 + 量表定义）。

从 compute_profile.py 拆出，原因：单文件超过 600 行违反 .claude/rules/coding.md（必须拆分）。
本模块只算统计量，不做任何判定：指标在这里出生，阈值在 _gates.py 判读，
"算出来"和"判得过"必须是两件事，否则改一条线就会顺手改一个公式。

三件必须记住的设计：
  1. 主票筛选 —— 重复票（judge_repeats>=2）高度相关，并入 agreement 分母会让 n 虚高、
     置信区间看起来比实际窄一倍。它们只进 repeat_agreement。
  2. 方向分解 —— over_rate / under_rate 分开记。漏判（under）会让 badcase 静默消失，
     单看 agreement 均值永远发现不了。
  3. agreement_within_1 —— 0-3 有序量表上相邻分差含义不均匀，只作参考不作主口径。
"""
from __future__ import annotations

from _contracts import mean, percentile, variance

# 与 pack rubrics.dimensions[].id 对齐的四维固定顺序。
# 顺序本身是契约：coverage.dimensions_without_gold 与提案文案都按它生成。
DIMENSIONS = ["outcome", "process", "efficiency", "risk"]


def _primary_votes(votes: list) -> tuple:
    """按 (judge, dimension, task_id) 归组，取 repeat=1（或最小 repeat）为主票。

    重复票（judge_repeats=2）高度相关，并入 agreement 分母会让 n 虚高、
    置信区间看起来比实际窄一倍。它们只进 repeat_agreement。
    返回 (primary, repeats)。
    """
    groups: dict = {}
    for v in votes:
        key = (v.get("judge"), v.get("dimension"), v.get("task_id"))
        groups.setdefault(key, []).append(v)
    primary, repeats = [], []
    for key, group in groups.items():
        ordered = sorted(group, key=lambda x: int(x.get("repeat") or 1))
        primary.append(ordered[0])
        repeats.extend(ordered[1:])
    return primary, repeats


def agreement_with_gold(votes: list, golds: list, *, tolerance: int = 0) -> dict:
    """与金标一致率（含方向分解）。

    公式见 references/reliability-metrics.md §1：
      agreement_exact = mean(1[s == g])            tolerance = 0
      agreement_±1    = mean(1[|s-g| <= 1])        tolerance = 1
      over_rate  = mean(1[s > g])   高估
      under_rate = mean(1[s < g])   低估 / 漏判 —— 单独盯，它让 badcase 静默消失
    返回 {(judge, dimension): cell_stats}；无配准金的 cell 不出现。
    """
    gold_by_key = {(g.get("task_id"), g.get("dimension")): g.get("gold_score") for g in golds}
    cells: dict = {}
    primary, _ = _primary_votes(votes)
    for v in primary:
        key = (v.get("judge"), v.get("dimension"))
        gold = gold_by_key.get((v.get("task_id"), key[1]))
        if gold is None:
            continue  # 该 task 在此维度无金标，由调用方记入 unmatched / no_gold_for_dimension
        c = cells.setdefault(key, {"scores": [], "golds": [], "latencies": []})
        c["scores"].append(float(v.get("score")))
        c["golds"].append(float(gold))
        if v.get("latency_ms") is not None:
            c["latencies"].append(float(v["latency_ms"]))

    out: dict = {}
    for key, c in cells.items():
        s, g = c["scores"], c["golds"]
        n = len(s)
        if tolerance == 0:
            agree = mean([1.0 if a == b else 0.0 for a, b in zip(s, g)])
        else:
            agree = mean([1.0 if abs(a - b) <= tolerance else 0.0 for a, b in zip(s, g)])
        out[key] = {
            "n": n,
            "agreement": round(agree, 6),
            "over_rate": round(mean([1.0 if a > b else 0.0 for a, b in zip(s, g)]), 6),
            "under_rate": round(mean([1.0 if a < b else 0.0 for a, b in zip(s, g)]), 6),
            "mean_signed_bias": round(mean([a - b for a, b in zip(s, g)]), 6),
            "score_variance": round(variance(s), 6),
            "gold_variance": round(variance(g), 6),
            "mean_score": round(mean(s), 6),
            "latency_p50_ms": _round_or_none(percentile(c["latencies"], 0.5)),
            "latency_p95_ms": _round_or_none(percentile(c["latencies"], 0.95)),
        }
    return out


def agreement_within_1(votes: list, golds: list) -> dict:
    """宽松口径（|Δ|≤1）。0-3 有序量表上相邻分差含义不均匀，只作参考不作主口径。"""
    return agreement_with_gold(votes, golds, tolerance=1)


def _round_or_none(x):
    return None if x is None else round(float(x), 3)


def order_swap_conflict_rate(swap_runs: list) -> dict:
    """顺序交换敏感度：成对场景两次结论冲突率。位置偏误的直接观测，无金标维度上唯一可用。"""
    cells: dict = {}
    for r in swap_runs or []:
        key = (r.get("judge"), r.get("dimension"))
        cells.setdefault(key, {"n": 0, "conflicts": 0, "latencies": []})
        c = cells[key]
        c["n"] += 1
        if r.get("first_run") != r.get("second_run"):
            c["conflicts"] += 1
        if r.get("latency_ms") is not None:
            c["latencies"].append(float(r["latency_ms"]))
    return {k: {"order_swap_conflict_rate": round(v["conflicts"] / v["n"], 6) if v["n"] else None,
                "order_swap_n": v["n"],
                "latency_p50_ms": _round_or_none(percentile(v["latencies"], 0.5)),
                "latency_p95_ms": _round_or_none(percentile(v["latencies"], 0.95))}
            for k, v in cells.items()}


def repeat_agreement(votes: list) -> dict:
    """裁判自身稳定性：同一 task 的重复票是否一致。

    区分"judge 判错了"和"judge 自己都没想好"。低 repeat_agreement + 高 agreement
    说明对难题结论不稳但平均而言对；两个都低说明提示词或量表有问题（→ 提案，不自行改）。
    """
    groups: dict = {}
    for v in votes:
        groups.setdefault((v.get("judge"), v.get("dimension"), v.get("task_id")), []).append(v)
    cells: dict = {}
    for (judge, dim, _tid), group in groups.items():
        if len(group) < 2:
            continue
        key = (judge, dim)
        cells.setdefault(key, {"n": 0, "consistent": 0})
        scores = {float(x.get("score")) for x in group}
        cells[key]["n"] += 1
        if len(scores) == 1:
            cells[key]["consistent"] += 1
    return {k: {"repeat_agreement": round(v["consistent"] / v["n"], 6) if v["n"] else None,
                "repeat_n": v["n"]} for k, v in cells.items()}
