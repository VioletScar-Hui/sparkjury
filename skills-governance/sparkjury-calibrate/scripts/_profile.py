#!/usr/bin/env python3
"""_profile.py — calibrate 的画像构造层（输入校验 + 同族红线 + build_profile）。

从 compute_profile.py 拆出，原因：单文件超过 600 行违反 .claude/rules/coding.md（必须拆分）。
本模块是"纯函数入口"的落点：build_profile(votes, golds, pack) -> payload，
同输入两次运行 payload hash 必须一致（calibrate `--selftest` 直接断言这一点）。

顺序即红线：先 _validate 再过同族红线，最后才算指标。
画像若建立在不合法的样本上，后面每一个数字都是错的 —— 同族自评掉点是
JudgeBench（arXiv 2410.12784）的实测观察，不是理论顾虑。
"""
from __future__ import annotations

from _gates import render_profile
from _metrics import (DIMENSIONS, _primary_votes, agreement_with_gold,
                      agreement_within_1, order_swap_conflict_rate,
                      repeat_agreement)

from _contracts import PreflightError, UsageError  # noqa: E402


def _validate(votes: list, golds: list, calibration_set: list) -> None:
    if not votes:
        raise UsageError("votes 不能为空：没有投票就没有画像可算")
    for i, v in enumerate(votes):
        miss = [k for k in ("judge", "dimension", "task_id", "score") if k not in v]
        if miss:
            raise PreflightError(f"votes[{i}] 缺字段 {miss}（schemas/io.schema.json#vote）")
        if v["dimension"] not in DIMENSIONS:
            raise PreflightError(f"votes[{i}] dimension={v['dimension']} 不在四维 {DIMENSIONS} 内")
        score = v.get("score")
        # score=None/字符串/bool 会在指标计算深处炸成 TypeError；输入校验阶段明确失败
        if isinstance(score, bool) or not isinstance(score, (int, float)):
            raise PreflightError(
                f"votes[{i}].score={score!r} 不是 0-3 有序量表数字（schemas/io.schema.json#vote）；"
                "拒绝用 0/默认值补造"
            )
    for i, g in enumerate(golds):
        miss = [k for k in ("task_id", "dimension", "gold_score") if k not in g]
        if miss:
            raise PreflightError(f"golds[{i}] 缺字段 {miss}")
        src = str(g.get("gold_source") or "")
        if "llm_judge" in src or "judge_vote" in src:
            raise PreflightError(
                f"golds[{i}] 的 gold_source={src!r}：不接受 LLM 票当金标（循环论证，"
                "画像会系统性偏乐观）。只接受人注册的 checker 与 tau2 自带终态判定。"
            )
    for i, e in enumerate(calibration_set or []):
        if not e.get("has_golden_outcome"):
            raise PreflightError(
                f"calibration_set[{i}]（{e.get('task_id')}）缺金标：画像失去外部基准。"
                "这是输入不合法，应回 evalset 修，不在 calibrate 侧降级。"
            )


def check_same_family(judged_agent: dict | None, judges_panel: list, forbidden: list) -> None:
    """同族红线：被评 agent 不得与任一裁判同族。

    JudgeBench（arXiv 2410.12784）的同族自评掉点观察是本红线的依据。
    在计算任何指标之前做：画像若建立在不合法的样本上，后面全错。
    """
    if not judged_agent:
        return
    family = (judged_agent.get("family") or "").strip().lower()
    model = str(judged_agent.get("model") or "")
    panel_families = {str(j.get("family") or "").strip().lower() for j in judges_panel or []}
    hit = [f for f in (family, *[f for f in panel_families if f and f in model.lower()]) if f in forbidden]
    if hit:
        raise PreflightError(
            f"同族红线触发：被评 agent family={family or model!r} 与 judges.yaml#"
            f"judged_agent_forbidden_families {forbidden} 有交集（命中 {hit}）。"
            "同族自评不可信，不出画像。请更换被评 agent 选型或调整裁判 panel。"
        )


def build_profile(payload_input: dict) -> dict:
    votes = payload_input["votes"]
    golds = payload_input["golds"]
    calibration_set = payload_input.get("calibration_set") or []
    swap_runs = payload_input.get("swap_runs") or []
    thresholds = payload_input.get("thresholds") or {}
    _validate(votes, golds, calibration_set)

    dims_with_gold = {g.get("dimension") for g in golds}
    gold_keys = {(g.get("task_id"), g.get("dimension")) for g in golds}
    cal_ids = {e.get("task_id") for e in calibration_set}
    primary, repeats = _primary_votes(votes)

    unmatched = []
    for v in primary:
        if (v.get("task_id"), v.get("dimension")) not in gold_keys:
            unmatched.append({"judge": v.get("judge"), "dimension": v.get("dimension"),
                              "task_id": v.get("task_id"),
                              "reason": "no_gold_for_dimension" if v.get("dimension") not in dims_with_gold
                              else ("task_not_in_calibration_set" if cal_ids and v.get("task_id") not in cal_ids
                                    else "no_gold_for_this_task")})

    cells = agreement_with_gold(votes, golds, tolerance=0)
    within1 = agreement_within_1(votes, golds)
    for key, stats in cells.items():
        stats["agreement_within_1"] = (within1.get(key) or {}).get("agreement")
    swap = order_swap_conflict_rate(swap_runs)
    repeats_stats = repeat_agreement(votes)

    min_n = (thresholds.get("calibrate") or {}).get("min_n_per_cell")
    for key, stats in cells.items():
        stats["min_n_met"] = None if min_n is None else (stats["n"] >= int(min_n))

    coverage = {
        "cells_total": 0,
        "cells_with_gold": len(cells),
        "votes_total": len(votes),
        "votes_matched_to_gold": sum(c["n"] for c in cells.values()),
        "repeat_votes_excluded_from_denominator": len(repeats),
        "unmatched_votes": unmatched,
        "judges_evaluated": sorted({str(k[0]) for k in cells}),
        "dimensions_without_gold": sorted(d for d in DIMENSIONS if d not in dims_with_gold),
    }
    profile = render_profile(cells, swap, repeats_stats, dims_with_gold, thresholds, coverage)
    profile["coverage"]["cells_total"] = len(profile["gates"])
    profile["coverage"]["judges_evaluated"] = sorted(
        {str(k[0]) for k in cells} | {str(k[0]) for k in swap} | {str(k[0]) for k in repeats_stats})
    # provenance：画像绑版本（gate-policy.md §4）。pack_hash 缺省 = 本次未绑 pack，记 null 不造假。
    profile["provenance"] = {
        "pack_hash": payload_input.get("pack_hash"),
        "judge_versions": {str(j.get("id")): str(j.get("model"))
                           for j in payload_input.get("judges_panel") or [] if j.get("id")},
        "calibration_task_count": len(calibration_set),
        "gate_threshold_source": ("pack thresholds.calibrate.*"
                                  if (thresholds.get("calibrate") or {})
                                  else "pack thresholds.calibrate.* 未定义（v0.1）"),
        "agreement_tolerance": 0,
        "primary_vote_rule": "repeat=1 为主票；repeat>=2 只进 repeat_agreement，不进 agreement 分母",
    }
    return profile
