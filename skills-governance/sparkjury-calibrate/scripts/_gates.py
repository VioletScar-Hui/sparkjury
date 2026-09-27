#!/usr/bin/env python3
"""_gates.py — calibrate 的门禁层（PASS/DEGRADE/FAIL/UNKNOWN 判定 + 画像渲染 + 提案）。

从 compute_profile.py 拆出，原因：单文件超过 600 行违反 .claude/rules/coding.md（必须拆分）。
本模块把 _metrics.py 算出的 cell 统计渲染成 judge_profile.json 的 payload。

三条不可动摇的判读规则（references/gate-policy.md §2）：
  1. **不自造阈值**：门禁线只从 pack thresholds.calibrate.* 取；整段缺失 -> UNKNOWN + 告警 + 提案，
     绝不给 PASS —— 宁可说没判，不说过了。
  2. **零方差规则阈值无关**：score 无方差而金标有方差 = 裁判无判别力。它排在
     thresholds_undefined 之前，因为这是阈值整段缺失时唯一还能给出的实判。
  3. **FAIL 是强断言**：n < min_n 时只给 UNKNOWN，不出 FAIL。

提案边界：本层只写 proposals[]，绝不改 rubric / prompt / judges.yaml / panel 成员。
"""
from __future__ import annotations

from _metrics import DIMENSIONS

# 工程参考线（非 pack 阈值）：补阈值提案里建议的 min_n_per_cell 下限。
# 出处见 references/reliability-metrics.md §6，pack v0.2 应固化为 thresholds.calibrate.min_n_per_cell。
# 注意：它**不**充当门禁线 —— pack 没定义 min_n 时门禁只给 UNKNOWN（红线：不自造阈值）；
# 零方差规则仍是阈值无关的，与这个参考线无关。
DEFAULT_MIN_N_PER_CELL = 20


def _gate_verdict(stats: dict, thresholds: dict) -> tuple:
    """按 references/gate-policy.md §2 的顺序规则判一个 cell。返回 (verdict, rule, threshold_source)。"""
    cal = (thresholds or {}).get("calibrate") or {}
    min_n = cal.get("min_n_per_cell")
    agreement_min = cal.get("agreement_min")
    over_under_max = cal.get("over_under_max")
    source = "pack thresholds.calibrate.*" if (min_n is not None or agreement_min is not None) else \
        "pack thresholds.calibrate.* 未定义（v0.1）"

    if stats.get("no_gold_for_dimension"):
        return "UNKNOWN", "no_gold_for_dimension", source
    if stats["n"] == 0:
        return "UNKNOWN", "insufficient_n", source

    # 顺序与 references/gate-policy.md §2 的判定规则一致：n < min_n 先于零方差。
    if min_n is not None and stats["n"] < int(min_n):
        return "UNKNOWN", "insufficient_n", source

    # 阈值无关的零方差规则：即使门禁阈值整段缺失（min_n / agreement_min / over_under_max
    # 都没有），退化裁判仍必须被认出来 —— 所以它排在 thresholds_undefined 之前
    # （gate-policy.md §2 设计说明 2：这是 v0.1 唯一能给出的实判）。
    if stats["score_variance"] == 0.0 and (stats.get("gold_variance") or 0.0) > 0.0:
        return "DEGRADE", "zero_variance", "threshold-free rule (gate-policy.md §2)"

    if agreement_min is None or over_under_max is None:
        return "UNKNOWN", "thresholds_undefined", source
    if stats["agreement"] < float(agreement_min):
        return "FAIL", "below_agreement_min", source
    if max(stats["over_rate"], stats["under_rate"]) > float(over_under_max):
        return "DEGRADE", "directional_bias", source
    return "PASS", "pass", source


def render_profile(cells: dict, swap: dict, repeats: dict, dims_with_gold: set,
                   thresholds: dict, coverage: dict) -> dict:
    """把 cell 统计渲染成 judge_profile.json 的 payload：画像 + gates + lead_judges + proposals。"""
    cal = (thresholds or {}).get("calibrate") or {}
    gates, notes, flags = [], [], []
    all_cells = set(cells) | set(swap) | set(repeats)
    flat: dict = {}   # (judge, dimension) -> merged cell stats

    for key in sorted(all_cells, key=lambda k: (str(k[0]), str(k[1]))):
        judge, dim = key
        base = cells.get(key)
        merged = dict(base) if base else {}
        merged.setdefault("n", 0)
        merged.setdefault("agreement", None)
        merged.setdefault("over_rate", None)
        merged.setdefault("under_rate", None)
        merged.setdefault("score_variance", None)
        merged.setdefault("gold_variance", None)
        merged.setdefault("mean_signed_bias", None)
        merged.setdefault("agreement_within_1", None)
        merged.setdefault("min_n_met", None)
        merged["no_gold_for_dimension"] = dim not in dims_with_gold
        if key in swap:
            merged.update({k: v for k, v in swap[key].items() if k not in merged or merged.get(k) is None})
        if key in repeats:
            merged.update({k: v for k, v in repeats[key].items() if merged.get(k) is None})
        if merged["no_gold_for_dimension"]:
            merged["note"] = (merged.get("note") or "") + \
                "该维度无金标（process 维仅在提供 golden_steps_ref 时有）；agreement 类指标不可计算，" \
                "不得用裁判互投当代理。".strip()
        verdict, rule, source = _gate_verdict(merged, thresholds)
        if verdict == "UNKNOWN" and rule in ("insufficient_n",):
            flags.append(f"calibrate_insufficient_n:{judge}:{dim}")
        if verdict == "UNKNOWN" and rule == "thresholds_undefined":
            flags.append("calibrate_thresholds_undefined")
        if verdict == "DEGRADE" and rule == "zero_variance":
            flags.append(f"judge_no_discrimination_{judge}_{dim}")
        if verdict == "DEGRADE" and rule == "directional_bias":
            flags.append(f"judge_degraded_{judge}_{dim}")
        if merged["no_gold_for_dimension"]:
            flags.append(f"calibrate_dim_without_gold:{dim}")

        gates.append({"judge": judge, "dimension": dim, "verdict": verdict, "rule": rule,
                      "reason": _reason(verdict, rule, merged, source), "threshold_source": source})
        flat[(judge, dim)] = merged

    judges = {}
    for (judge, dim), merged in flat.items():
        judges.setdefault(judge, {"by_dimension": {}})["by_dimension"][dim] = merged

    # lead judge：按 agreement_exact 降序排（阈值无关），同分取 latency 更快的。
    lead_judges = {}
    for dim in sorted({d for (_j, d) in all_cells}):
        candidates = []
        for (judge, d), merged in flat.items():
            if d != dim or merged.get("agreement") is None:
                continue
            candidates.append((judge, merged))
        if not candidates:
            lead_judges[dim] = {"judge": None, "agreement": None,
                                "basis": "ranking_not_gate",
                                "tie_break": "no_candidate_with_gold"}
            continue
        # 排序键：agreement 降序 → 方向偏差 max(over,under) 升序（更平衡的优先）→ latency 升序（便宜优先）
        candidates.sort(key=lambda kv: (
            -kv[1]["agreement"],
            max(kv[1].get("over_rate") or 0.0, kv[1].get("under_rate") or 0.0),
            kv[1].get("latency_p50_ms") if kv[1].get("latency_p50_ms") is not None else 1e18,
        ))
        best_judge, best = candidates[0]
        tied = [j for j, c in candidates
                if c["agreement"] == best["agreement"]
                and max(c.get("over_rate") or 0.0, c.get("under_rate") or 0.0)
                == max(best.get("over_rate") or 0.0, best.get("under_rate") or 0.0)]
        lead_judges[dim] = {"judge": best_judge, "agreement": best["agreement"],
                            "basis": "ranking_not_gate",
                            "tie_break": "latency_asc" if len(tied) > 1 else "highest_agreement",
                            "tied_with": tied}

    proposals = _proposals(gates, flat, judges, cal, flags)

    if any(g["verdict"] == "UNKNOWN" and g["rule"] == "thresholds_undefined" for g in gates):
        notes.append(
            "pack v0.1 未定义 thresholds.calibrate.min_n_per_cell / agreement_min / over_under_max，"
            "门禁对这些 cell 只能给 UNKNOWN。这是诚实中间态不是缺陷：宁可说没判，不说过了。"
            "已生成补阈值的 pack diff 提案，需 govern 批准后才会有门禁结论。"
        )
    zero = [g for g in gates if g["rule"] == "zero_variance"]
    if zero:
        notes.append(
            f"{len(zero)} 个 cell 命中阈值无关的零方差退化裁判规则（score 无方差但金标有方差）："
            + "; ".join(f"{g['judge']}/{g['dimension']}" for g in zero)
            + "。这类裁判的 agreement 可能是'全打中间分'刷高的，无判别力。"
        )
    no_gold_dims = sorted({d for (_j, d) in all_cells if d not in dims_with_gold})
    if no_gold_dims:
        notes.append(
            f"维度 {', '.join(no_gold_dims)} 无金标，agreement 类指标不可计算；"
            "这些维度上唯一可用的可靠性信号是顺序交换敏感度（位置偏误的直接观测）。"
        )
    if coverage.get("unmatched_votes"):
        notes.append(
            f"{len(coverage['unmatched_votes'])} 张票配不上金标（无对应 gold cell 或不在校准集），"
            "已如实列入 coverage.unmatched_votes。画像分母不等于投票数，引用任何 agreement 时必须同时引用 n。"
        )

    payload = {
        "judges": judges,
        "gates": gates,
        "lead_judges": lead_judges,
        "proposals": proposals,
        "coverage": dict(coverage),
        "notes": notes,
    }
    payload["degraded_flags"] = sorted(set(flags))
    return payload


def _reason(verdict: str, rule: str, stats: dict, source: str) -> str:
    if rule == "no_gold_for_dimension":
        return "该维度无金标，agreement 类指标不可计算；不得用裁判互投当代理（循环论证）"
    if rule == "insufficient_n":
        return f"主票数 n={stats['n']} 不足，点估计方差过大；FAIL 是强断言，证据不足时不出 FAIL"
    if rule == "thresholds_undefined":
        return (f"阈值未定义（{source}）；agreement={stats.get('agreement')}、n={stats['n']} 已记录，"
                "但缺门禁线无法判定，按保守原则不给 PASS")
    if rule == "zero_variance":
        return (f"score 方差={stats.get('score_variance')} 而金标方差={stats.get('gold_variance')}："
                "裁判无判别力，agreement 可能是全打中间分刷高的（阈值无关规则）")
    if rule == "below_agreement_min":
        return f"agreement={stats.get('agreement')} 低于门禁线（{source}），不得作正式裁判"
    if rule == "directional_bias":
        return (f"agreement={stats.get('agreement')} 达标但单方向偏差过大"
                f"（over={stats.get('over_rate')} / under={stats.get('under_rate')} > 上限，{source}），"
                "需降权并由 lead judge 主导")
    return (f"agreement={stats.get('agreement')}（n={stats['n']}）达标，"
            f"max(over,under)={max(stats.get('over_rate') or 0, stats.get('under_rate') or 0)} 未超上限")


def _proposals(gates: list, cells: dict, judges: dict, cal: dict, flags: list) -> list:
    out = []
    missing = [k for k in ("min_n_per_cell", "agreement_min", "over_under_max") if cal.get(k) is None]
    if missing:
        out.append({
            "target_field": "thresholds.calibrate",
            "current": None,
            "proposed": f"补充 {', '.join(missing)}；min_n_per_cell 建议不低于 "
                      f"{DEFAULT_MIN_N_PER_CELL}（依据 reliability-metrics.md §6 的样本量推导）",
            "evidence": {"judge": "(panel)", "dimension": "(all)", "agreement": None,
                         "n": sum(c["n"] for c in cells.values())},
            "requires": "govern_approval",
        })
    for g in gates:
        judge, dim, stats = g["judge"], g["dimension"], cells.get((g["judge"], g["dimension"]), {})
        ev = {"judge": judge, "dimension": dim, "agreement": stats.get("agreement"),
              "n": stats.get("n") or 0, "over_rate": stats.get("over_rate"),
              "under_rate": stats.get("under_rate"), "score_variance": stats.get("score_variance")}
        if g["rule"] in ("below_agreement_min", "directional_bias", "zero_variance"):
            usable = sorted(d for (j, d), c in cells.items() if j == judge and c.get("agreement") is not None
                            and not (j, d) == (judge, dim)
                            and all(x["rule"] != "zero_variance" and x["verdict"] != "FAIL"
                                    for x in gates if x["judge"] == judge and x["dimension"] == d))
            out.append({
                "target_field": f"judges.{judge}.dimensions",
                "current": ",".join(DIMENSIONS),
                "proposed": (f"收窄为 {','.join(usable) if usable else '(none，建议替换该裁判)'}"
                             f"，移除 {dim}（{g['rule']}）"),
                "evidence": ev, "requires": "govern_approval",
            })
        if g["rule"] == "directional_bias":
            direction = "over（比金标宽松，容易放过问题）" if (stats.get("over_rate") or 0) > (stats.get("under_rate") or 0) \
                else "under（漏判，问题会静默消失）"
            out.append({
                "target_field": f"prompts/{dim}.md",
                "current": "v0.1 现版",
                "proposed": f"人工复核该维度裁判提示词的判定锚点；观察到的偏差方向为 {direction}",
                "evidence": ev, "requires": "govern_approval",
            })
    return out
