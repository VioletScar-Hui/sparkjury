#!/usr/bin/env python3
"""_selftest.py — compute_profile.py 的内嵌自检 fixture（`--selftest` 触发）。

从 compute_profile.py 拆出，原因：单文件 600 行上限，且自检需要 import 被测函数形成环。

核心 fixture：**6 票对 4 金标**，覆盖主票筛选、双口径 agreement、over/under 方向分解、
零方差退化裁判、n=1 边界、顺序交换敏感度、重复票稳定性。
门禁分两轮跑：先无阈值（v0.1 常态 → UNKNOWN + 告警），再注入阈值（→ 真实 PASS/DEGRADE/FAIL）。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _contracts import PreflightError, UsageError, canonical_hash  # noqa: E402
import compute_profile as cp  # noqa: E402

# 4 个金标 cell
GOLDS = [
    {"task_id": "t1", "dimension": "outcome", "gold_score": 3, "gold_source": "tau2_db_final_state"},
    {"task_id": "t2", "dimension": "outcome", "gold_score": 0, "gold_source": "tau2_db_final_state"},
    {"task_id": "t3", "dimension": "process", "gold_score": 2, "gold_source": "golden_trajectory:retail_0003"},
    {"task_id": "t4", "dimension": "process", "gold_score": 1, "gold_source": "golden_trajectory:retail_0004"},
]

# 6 张主票。judge_a 4 张（2 outcome + 2 process），judge_b 2 张（1 outcome + 1 process）
VOTES = [
    {"vote_id": "v1", "judge": "judge_a", "dimension": "outcome", "task_id": "t1", "repeat": 1,
     "score": 3, "confidence": 0.9, "latency_ms": 1200, "evidence_span_ids": ["s1"]},
    {"vote_id": "v2", "judge": "judge_a", "dimension": "outcome", "task_id": "t2", "repeat": 1,
     "score": 0, "confidence": 0.95, "latency_ms": 1100, "evidence_span_ids": ["s2"]},
    {"vote_id": "v3", "judge": "judge_a", "dimension": "process", "task_id": "t3", "repeat": 1,
     "score": 2, "confidence": 0.7, "latency_ms": 1300, "evidence_span_ids": ["s3"]},
    {"vote_id": "v4", "judge": "judge_a", "dimension": "process", "task_id": "t4", "repeat": 1,
     "score": 2, "confidence": 0.6, "latency_ms": 1250, "evidence_span_ids": ["s4"]},
    {"vote_id": "v5", "judge": "judge_b", "dimension": "outcome", "task_id": "t1", "repeat": 1,
     "score": 3, "confidence": 0.85, "latency_ms": 900, "evidence_span_ids": ["s1"]},
    {"vote_id": "v6", "judge": "judge_b", "dimension": "process", "task_id": "t3", "repeat": 1,
     "score": 1, "confidence": 0.5, "latency_ms": 950, "evidence_span_ids": ["s3"]},
]

SWAP_RUNS = [
    {"judge": "judge_a", "dimension": "process", "pair_id": "p1",
     "first_run": "a_gt_b", "second_run": "b_gt_a", "latency_ms": 1400},
    {"judge": "judge_a", "dimension": "process", "pair_id": "p2",
     "first_run": "a_gt_b", "second_run": "a_gt_b", "latency_ms": 1350},
    {"judge": "judge_b", "dimension": "process", "pair_id": "p1",
     "first_run": "tie", "second_run": "tie", "latency_ms": 1000},
]

CALIBRATION_SET = [
    {"task_id": t, "expected_outcome_ref": "tau2_db_final_state", "has_golden_outcome": True}
    for t in ("t1", "t2", "t3", "t4")
]

THRESHOLDS_V01 = {"cluster": {"representatives_per_cluster": 3}}
THRESHOLDS_WITH_GATE = {
    "calibrate": {"min_n_per_cell": 2, "agreement_min": 0.75, "over_under_max": 0.25},
    "cluster": {"representatives_per_cluster": 3},
}

PANEL = [
    {"id": "judge_a", "family": "qwen", "model": "Qwen3-30B-A3B", "runtime": "local_vllm"},
    {"id": "judge_b", "family": "gemma", "model": "Gemma-4-12B", "runtime": "local_vllm"},
    {"id": "judge_c", "family": "step", "model": "Step-3.7-Flash", "runtime": "local_vllm"},
]


def _approx(a, b, eps=1e-6):
    if a is None or b is None:
        return a is None and b is None
    return abs(float(a) - float(b)) < eps


def run() -> int:
    failures: list = []

    def check(cond, msg):
        if not cond:
            failures.append(msg)

    # ---------- 1. 主票筛选：重复票不进 agreement 分母 ----------
    with_repeats = VOTES + [
        {"vote_id": "v1r", "judge": "judge_a", "dimension": "outcome", "task_id": "t1", "repeat": 2,
         "score": 1, "confidence": 0.4, "latency_ms": 800},
    ]
    primary, repeats = cp._primary_votes(with_repeats)
    check(len(primary) == 6, f"主票应为 6 张，实得 {len(primary)}")
    check(len(repeats) == 1, f"重复票应为 1 张，实得 {len(repeats)}")

    cells = cp.agreement_with_gold(VOTES, GOLDS)
    check(len(cells) == 4, f"应产出 4 个 (judge,dimension) cell，实得 {len(cells)}")

    # ---------- 2. judge_a/outcome：n=2, agreement 1.0, 无偏差 ----------
    a_out = cells[("judge_a", "outcome")]
    check(a_out["n"] == 2, f"judge_a/outcome n 应为 2，实得 {a_out['n']}")
    check(_approx(a_out["agreement"], 1.0), f"agreement 应为 1.0，实得 {a_out['agreement']}")
    check(_approx(a_out["over_rate"], 0.0) and _approx(a_out["under_rate"], 0.0),
          "judge_a/outcome 应无方向偏差")
    check(_approx(a_out["mean_signed_bias"], 0.0), f"signed bias 应为 0，实得 {a_out['mean_signed_bias']}")
    check(_approx(a_out["score_variance"], 4.5), f"score 方差应为 4.5，实得 {a_out['score_variance']}")
    check(_approx(a_out["gold_variance"], 4.5), f"金标方差应为 4.5，实得 {a_out['gold_variance']}")
    check(_approx(a_out["latency_p50_ms"], 1150.0), f"latency p50 应为 1150，实得 {a_out['latency_p50_ms']}")

    # ---------- 3. judge_a/process：n=2, agreement 0.5, over 0.5, 零方差 ----------
    a_prc = cells[("judge_a", "process")]
    check(a_prc["n"] == 2, "judge_a/process n 应为 2")
    check(_approx(a_prc["agreement"], 0.5), f"agreement 应为 0.5，实得 {a_prc['agreement']}")
    check(_approx(a_prc["over_rate"], 0.5) and _approx(a_prc["under_rate"], 0.0),
          "judge_a/process 应 over=0.5 / under=0.0（高估方向）")
    check(_approx(a_prc["score_variance"], 0.0), f"零方差应为 0.0，实得 {a_prc['score_variance']}")
    check(a_prc["gold_variance"] > 0.0, "金标应有方差 —— 否则零方差规则不成立")

    # ---------- 4. judge_b 两个 cell：n=1 边界 ----------
    b_out = cells[("judge_b", "outcome")]
    b_prc = cells[("judge_b", "process")]
    check(b_out["n"] == 1 and _approx(b_out["agreement"], 1.0), "judge_b/outcome 应为 n=1 且一致")
    check(b_prc["n"] == 1 and _approx(b_prc["agreement"], 0.0), "judge_b/process 应为 n=1 且不一致")
    check(_approx(b_prc["under_rate"], 1.0), "judge_b/process 应是 under=1.0（漏判方向）")
    check(_approx(b_prc["score_variance"], 0.0), "单点方差应为 0.0")

    # ---------- 5. 双口径 agreement ----------
    within1 = cp.agreement_within_1(VOTES, GOLDS)
    check(_approx(within1[("judge_a", "process")]["agreement"], 1.0),
          "±1 口径下 judge_a/process 应全部一致（2 vs 1 / 2 都是差 1）")
    check(_approx(within1[("judge_a", "outcome")]["agreement"], 1.0), "±1 口径下应仍为 1.0")

    # ---------- 6. 顺序交换敏感度与重复票稳定性 ----------
    swap = cp.order_swap_conflict_rate(SWAP_RUNS)
    check(_approx(swap[("judge_a", "process")]["order_swap_conflict_rate"], 0.5),
          f"judge_a/process 冲突率应为 0.5，实得 {swap[('judge_a','process')]['order_swap_conflict_rate']}")
    check(swap[("judge_a", "process")]["order_swap_n"] == 2, "judge_a/process 应有 2 对")
    check(_approx(swap[("judge_b", "process")]["order_swap_conflict_rate"], 0.0),
          "judge_b/process 应无冲突")
    rep = cp.repeat_agreement(with_repeats)
    check(_approx(rep[("judge_a", "outcome")]["repeat_agreement"], 0.0),
          "judge_a/outcome 的重复票（3 vs 1）应判为不一致")
    check(rep[("judge_a", "outcome")]["repeat_n"] == 1, "只有 1 个 task 有重复票")

    # ---------- 7. v0.1（无阈值）：门禁大面积 UNKNOWN + 告警 + 提案 ----------
    p01 = cp.build_profile({"votes": VOTES, "golds": GOLDS, "calibration_set": CALIBRATION_SET,
                            "swap_runs": SWAP_RUNS, "thresholds": THRESHOLDS_V01})
    verdicts = {(g["judge"], g["dimension"]): g["verdict"] for g in p01["gates"]}
    check(verdicts[("judge_a", "outcome")] == "UNKNOWN",
          f"无阈值时 judge_a/outcome 应 UNKNOWN，实得 {verdicts[('judge_a','outcome')]}")
    check(verdicts[("judge_a", "process")] == "DEGRADE",
          f"零方差规则在无阈值时仍须生效，judge_a/process 应 DEGRADE，实得 {verdicts[('judge_a','process')]}")
    check("calibrate_thresholds_undefined" in p01["degraded_flags"], "缺阈值应打告警 flag")
    check("judge_no_discrimination_judge_a_process" in p01["degraded_flags"],
          "退化裁判应打专用 flag")
    check(any(pr["target_field"] == "thresholds.calibrate" for pr in p01["proposals"]),
          "应生成补阈值的 pack diff 提案")
    check(all(pr["requires"] == "govern_approval" for pr in p01["proposals"]),
          "所有提案都必须标 requires=govern_approval")
    check(p01["lead_judges"]["outcome"]["basis"] == "ranking_not_gate",
          "lead judge 必须标注这是排序不是门禁")
    # outcome 维 judge_a 与 judge_b 的 agreement 都是 1.0，属同分：胜负由偏差平衡度再按时延决定，
    # 因此只能断言"winner 在同分集合里"，不能断言某一个具体裁判。
    check(p01["lead_judges"]["outcome"]["judge"] in p01["lead_judges"]["outcome"]["tied_with"],
          f"lead judge 必须落在同分集合内，实得 {p01['lead_judges']['outcome']}")
    check(p01["lead_judges"]["outcome"]["tie_break"] == "latency_asc",
          "同分时应标注 tie_break=latency_asc")
    check(p01["coverage"]["votes_matched_to_gold"] == 6, "6 张票应全部配到金标")
    check(p01["coverage"]["cells_total"] == 4, "cell 总数应为 4")
    check(canonical_hash(p01) == canonical_hash(cp.build_profile(
        {"votes": VOTES, "golds": GOLDS, "calibration_set": CALIBRATION_SET,
         "swap_runs": SWAP_RUNS, "thresholds": THRESHOLDS_V01})), "build_profile 必须幂等")

    # ---------- 8. 注入阈值后：真实 PASS/DEGRADE/FAIL ----------
    p2 = cp.build_profile({"votes": VOTES, "golds": GOLDS, "calibration_set": CALIBRATION_SET,
                           "swap_runs": SWAP_RUNS, "thresholds": THRESHOLDS_WITH_GATE})
    v2 = {(g["judge"], g["dimension"]): g["verdict"] for g in p2["gates"]}
    gates_p2 = {g["judge"] + "/" + g["dimension"]: g for g in p2["gates"]}
    # judge_a/outcome: agreement=1.0 >= 0.75, max(over,under)=0 <= 0.25, n=2>=2 → PASS
    check(v2[("judge_a", "outcome")] == "PASS",
          f"judge_a/outcome 应 PASS，实得 {v2[('judge_a','outcome')]}")
    check(gates_p2["judge_a/outcome"]["rule"] == "pass", "judge_a/outcome 的 rule 应为 pass")
    # judge_a/process: agreement=0.5 < 0.75，但零方差规则优先 → DEGRADE
    check(v2[("judge_a", "process")] == "DEGRADE",
          f"零方差优先于 agreement 判定，judge_a/process 应 DEGRADE，实得 {v2[('judge_a','process')]}")
    # judge_b/process: n=1，min_n=2 → insufficient_n 优先于任何阈值判定。
    # 注意：这里 score_variance=0 但 gold_variance 也是 0（只有 1 个金标点），
    # 退化裁判规则无从评估 —— 单点金标下"零方差"说明不了任何事，这正是必须报 n 的理由。
    check(v2[("judge_b", "process")] == "UNKNOWN",
          f"judge_b/process n=1 < min_n=2，应 UNKNOWN，实得 {v2[('judge_b','process')]}")
    check(gates_p2["judge_b/process"]["rule"] == "insufficient_n",
          f"rule 应为 insufficient_n，实得 {gates_p2['judge_b/process']['rule']}")
    # judge_b/outcome: agreement=1.0, 但 n=1 < min_n=2 → UNKNOWN
    check(v2[("judge_b", "outcome")] == "UNKNOWN",
          f"n=1 < min_n=2，judge_b/outcome 应 UNKNOWN，实得 {v2[('judge_b','outcome')]}")
    check(gates_p2["judge_b/outcome"]["rule"] == "insufficient_n", "judge_b/outcome 的 rule 应为 insufficient_n")
    check(gates_p2["judge_a/process"]["rule"] == "zero_variance", "judge_a/process 的 rule 应为 zero_variance")
    check("calibrate_insufficient_n:judge_b:outcome" in p2["degraded_flags"], "样本不足应打 flag")

    # ---------- 9. FAIL 路径：agreement 低且有方差 ----------
    fail_votes = [
        {"judge": "judge_c", "dimension": "risk", "task_id": t, "repeat": 1, "score": s}
        for t, s in (("r1", 3), ("r2", 0), ("r3", 2), ("r4", 1))
    ]
    fail_golds = [{"task_id": t, "dimension": "risk", "gold_score": g,
                   "gold_source": "rule:hard_rule"} for t, g in (("r1", 0), ("r2", 3), ("r3", 0), ("r4", 3))]
    pf = cp.build_profile({"votes": fail_votes, "golds": fail_golds, "calibration_set": [],
                           "swap_runs": [], "thresholds": THRESHOLDS_WITH_GATE})
    gf = {(g["judge"], g["dimension"]): g for g in pf["gates"]}[("judge_c", "risk")]
    check(gf["verdict"] == "FAIL", f"agreement=0 且有方差的 cell 应 FAIL，实得 {gf['verdict']}")
    check(gf["rule"] == "below_agreement_min", f"rule 应为 below_agreement_min，实得 {gf['rule']}")
    check(any(pr["target_field"] == "judges.judge_c.dimensions" for pr in pf["proposals"]),
          "FAIL cell 应收窄维度提案")

    # ---------- 10. 无金标维度：不计算出 agreement，不当代理 ----------
    png = cp.build_profile({"votes": VOTES, "golds": [g for g in GOLDS if g["dimension"] == "outcome"],
                            "calibration_set": CALIBRATION_SET, "swap_runs": SWAP_RUNS,
                            "thresholds": THRESHOLDS_WITH_GATE})
    vpng = {(g["judge"], g["dimension"]): g for g in png["gates"]}
    check(vpng[("judge_a", "process")]["verdict"] == "UNKNOWN"
          and vpng[("judge_a", "process")]["rule"] == "no_gold_for_dimension",
          "process 维无金标时应 UNKNOWN 且 rule=no_gold_for_dimension")
    check(png["coverage"]["dimensions_without_gold"] == ["efficiency", "process", "risk"],
          f"dimensions_without_gold 应如实列出，实得 {png['coverage']['dimensions_without_gold']}")
    # 顺序交换敏感度在无金标维度上仍然可用
    check(png["judges"]["judge_a"]["by_dimension"]["process"]["order_swap_conflict_rate"] == 0.5,
          "无金标维度上顺序交换敏感度仍应可用")

    # ---------- 11. 拒绝路径 ----------
    # 11a 用 LLM 票当金标
    try:
        cp._validate(VOTES, [{"task_id": "t1", "dimension": "outcome", "gold_score": 3,
                              "gold_source": "llm_judge:judge_a"}], CALIBRATION_SET)
    except PreflightError:
        pass
    else:
        failures.append("gold_source 为 llm_judge 时必须抛 PreflightError（循环论证）")

    # 11b calibration 集含无金标条目
    try:
        cp._validate(VOTES, GOLDS, [{"task_id": "t1", "has_golden_outcome": False}])
    except PreflightError:
        pass
    else:
        failures.append("calibration 集含无金标条目时必须抛 PreflightError 并不降级")

    # 11c 同族红线
    try:
        cp.check_same_family({"name": "被评", "model": "qwen-agent-1", "family": "qwen"}, PANEL,
                             ["qwen", "gemma", "step"])
    except PreflightError:
        pass
    else:
        failures.append("被评 agent 与裁判同族时必须抛 PreflightError")
    cp.check_same_family({"name": "被评", "model": "llama-agent-1", "family": "llama"}, PANEL,
                         ["qwen", "gemma", "step"])  # 不同族应放行

    # 11d 空 votes / 未知维度
    for bad, why in (([], "空 votes"), ([{"judge": "j", "dimension": "nope", "task_id": "t", "score": 1}],
                                        "未知维度")):
        try:
            cp._validate(bad, GOLDS, CALIBRATION_SET)
        except (PreflightError, UsageError):
            pass
        else:
            failures.append(f"{why} 必须抛 UsageError/PreflightError")

    # ---------- 12. 提案必须带足四项证据 ----------
    for pr in p01["proposals"]:
        ev = pr.get("evidence") or {}
        for k in ("judge", "dimension", "agreement", "n"):
            if k not in ev:
                failures.append(f"提案 {pr['target_field']} 的 evidence 缺字段 {k}")
                break

    if failures:
        print("FAIL --selftest")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("PASS --selftest（6 票对 4 金标：主票筛选 / 双口径 agreement / over-under 方向分解 / "
          "零方差退化裁判 / n=1 边界 / 顺序交换敏感度 / 重复票稳定性 / 无阈值 UNKNOWN / "
          "注入阈值 PASS-DEGRADE-FAIL / 五条拒绝路径）")
    return 0
