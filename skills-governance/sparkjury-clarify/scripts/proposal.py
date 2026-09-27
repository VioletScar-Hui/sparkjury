#!/usr/bin/env python3
"""proposal.py — 五问构造与提案编译（clarify.py 的拆分模块）。

把 pack 事实表和用户答案编译成 session payload：pack_diff（含 impact 影响面）、
checker 契约提案（只给契约，一行代码都不写）、no_change_reason、pending_items、
degraded_flags。

编译纪律（SKILL.md 红线在代码里的落点）：
  * 每条 diff 的 `current` 必须来自 pack 扫描值，扫不到就作废该条并记 pending；
  * 方向型答案只产 `direction` + pending，**不许替人填数字**（权重和≠1 即拒产数值 diff）；
  * 与 pack 现值一致的答案走 no_change_reason，不制造无意义 diff；
  * checker 只出契约提案，命名与实现归人。

模块划分见 _contracts.py 顶部说明。
"""
from __future__ import annotations

import json

from _contracts import (
    DEFAULT_PASS_K,
    DEFAULT_PRIMARY_METRIC,
    DEFAULT_VETO_HINTS,
    DIMENSIONS,
    IMPACT,
    PACK_PREFIX,
    MIN_SUPPORT,
    QUESTION_BUDGET,
    _to_int,
)


def build_questions(pack: dict) -> list:
    """按五问预算构造问题列表。默认值来自 pack 现值，不写死数字。"""
    weights = pack.get("weights") or {}
    weight_default = "沿用当前权重 " + json.dumps(weights, ensure_ascii=False) if weights else "沿用 pack 当前权重"
    metric = pack.get("primary_metric") or DEFAULT_PRIMARY_METRIC
    k = pack.get("pass_k")
    k_default = f"pass_k={k}（tau2-bench 口径）" if k is not None else f"pass_k={DEFAULT_PASS_K}（tau2-bench 口径）"
    return [
        {
            "id": "Q1",
            "question": "被评对象是什么 agent、什么域？（决定 pack_id 与 checker 归属）",
            "maps_to": [f"{PACK_PREFIX}/pack.manifest.json:pack_id", f"{PACK_PREFIX}/checkers/"],
            "default_if_unanswered": f"沿用 pack_id={pack.get('pack_id')}",
        },
        {
            "id": "Q2",
            "question": "四维（outcome/process/efficiency/risk）里哪几个更要紧？权重怎么摆？",
            "maps_to": [f"{PACK_PREFIX}/rubrics.yaml:dimensions[].weight"],
            "default_if_unanswered": weight_default,
        },
        {
            "id": "Q3",
            "question": "有没有可判定终态/金标？（决定 outcome 走确定性路径还是 LLM 路径）",
            "maps_to": [
                f"{PACK_PREFIX}/rubrics.yaml:dimensions[id=outcome].method",
                f"{PACK_PREFIX}/checkers/",
            ],
            "default_if_unanswered": "LLM judge 路径 + no_golden_outcome 标记（pack 已内建）",
        },
        {
            "id": "Q4",
            "question": "什么动作算一票否决？（risk 维 hard_rules 的边界）",
            "maps_to": [
                f"{PACK_PREFIX}/rubrics.yaml:dimensions[id=risk].hard_rules",
                f"{PACK_PREFIX}/taxonomy.yaml:categories[id=F08].default_severity",
            ],
            "default_if_unanswered": "沿用 v0.1：越权 / PII 外泄 / 不可逆无确认",
        },
        {
            "id": "Q5",
            "question": "回归口径：pass^k 的 k 取几？主指标是哪个？",
            "maps_to": [
                f"{PACK_PREFIX}/thresholds.yaml:regress.pass_k",
                f"{PACK_PREFIX}/thresholds.yaml:regress.gates.primary_metric",
            ],
            "default_if_unanswered": f"{k_default} + 主指标 {metric}",
        },
    ]


def _diff(target_file, field, current, proposed, rationale, impact_key, direction=None):
    skills, dims = IMPACT[impact_key]
    entry = {
        "target_file": target_file,
        "field": field,
        "current": current,
        "proposed": proposed,
        "rationale": rationale,
        "impact": {"affected_skills": list(skills), "affected_dimensions": [dims]},
    }
    if proposed is None and direction:
        entry["direction"] = direction
    return entry


def _kind_of(answer) -> str:
    if isinstance(answer, dict):
        return answer.get("kind", "explicit")
    return "explicit" if answer not in (None, "") else "skipped"


def _contract_id(answers: dict, existing: list) -> str:
    """checker 契约 id 命名**提案**：取 Q1 答案里最后一个 ASCII 词做域名前缀，最终命名归人。"""
    import re
    q1 = answers.get("Q1")
    text = (q1.get("text", "") if isinstance(q1, dict) else str(q1 or ""))
    tokens = re.findall(r"[a-z][a-z0-9_-]{2,}", text.lower())
    prefix = tokens[-1] if tokens else "checker"
    return f"{prefix}.veto_{len(existing) + 1:02d}"


def compile_proposal(answers: dict, pack: dict) -> tuple:
    """把五问答案编译成 (pack_diff, checker_contracts, no_change_reason, pending_items, degraded_flags)。"""
    diff, contracts, no_change, pending, flags = [], [], [], [], []
    weights = pack.get("weights") or {}

    # --- Q1 域：不相符 => 新 pack 而非 diff
    a1 = answers.get("Q1")
    if _kind_of(a1) == "skipped":
        pending.append("Q1 未答：沿用 pack_id，域归属待确认")
    else:
        text = a1.get("text", "") if isinstance(a1, dict) else str(a1)
        if pack.get("pack_id") and pack["pack_id"].split("-")[0] in text:
            no_change.append(f"Q1 域与 pack_id={pack['pack_id']} 相符，pack_id 不变")
        else:
            pending.append(f"Q1 指向的域（{text[:40]}）与当前 pack 不符 → 需新建 pack，不由 diff 表达")

    # --- Q2 权重
    a2 = answers.get("Q2")
    k2 = _kind_of(a2)
    if k2 == "skipped":
        pending.append("Q2 未答：四维权重沿用 pack 现值")
    elif k2 == "directional":
        flags.append("clarify_direction_only")
        dims = a2.get("dims") or list(DIMENSIONS)
        for d in dims:
            if d not in weights:
                pending.append(f"Q2 要求调整 {d}，但 pack 未扫描到该维权重")
                continue
            diff.append(_diff(
                f"{PACK_PREFIX}/rubrics.yaml", f"dimensions[id={d}].weight", weights[d], None,
                f"用户方向性意见（{a2.get('text', a2.get('direction', ''))[:60]}），数值待人定",
                ("rubrics.yaml", "weight"), direction="replace"))
        pending.append("Q2 只给了方向：请给出四维显式权重（和须为 1.0）")
    else:
        new_w = a2.get("weights") or {}
        total = round(sum(new_w.get(d, 0.0) for d in DIMENSIONS), 6)
        if abs(total - 1.0) > 1e-6:
            flags.append("clarify_direction_only")
            pending.append(f"Q2 权重和={total}≠1.0，拒绝据此生成数值 diff，请重新分配")
        else:
            for d in DIMENSIONS:
                if d not in weights:
                    pending.append(f"Q2 给出 {d} 权重，但 pack 未扫描到该维 current 值，该条作废")
                    continue
                if abs(new_w[d] - weights[d]) < 1e-9:
                    no_change.append(f"Q2 {d} 权重与 pack 现值一致，不产生 diff")
                else:
                    diff.append(_diff(
                        f"{PACK_PREFIX}/rubrics.yaml", f"dimensions[id={d}].weight", weights[d], new_w[d],
                        "用户显式权重（Q2）", ("rubrics.yaml", "weight")))

    # --- Q3 终态/金标
    a3 = answers.get("Q3")
    if _kind_of(a3) == "skipped":
        pending.append("Q3 未答：outcome 判据路径沿用 pack 现值")
    else:
        text = (a3.get("text", "") if isinstance(a3, dict) else str(a3)).lower()
        has_gold = any(tok in text for tok in ("db", "终态", "金标", "golden", "checker", "可判定"))
        no_gold = any(tok in text for tok in ("没有", "无", "缺失", "no_golden", "没法"))
        if has_gold and not no_gold:
            if (pack.get("outcome_method") or "").strip() == "deterministic_first":
                no_change.append("Q3 有可判定终态：pack outcome.method 已是 deterministic_first，不产生 diff")
            else:
                diff.append(_diff(
                    f"{PACK_PREFIX}/rubrics.yaml", "dimensions[id=outcome].method",
                    pack.get("outcome_method"), "deterministic_first",
                    "用户确认存在可判定终态（Q3）", ("rubrics.yaml", "method")))
        elif no_gold:
            no_change.append("Q3 无金标：pack outcome checklist 已内建 no_golden_outcome 上限 2 分 + 置信降档")
        else:
            pending.append("Q3 答案不足以判定有无金标，请明确「有/无」")

    # --- Q4 一票否决
    a4 = answers.get("Q4")
    if _kind_of(a4) == "skipped":
        pending.append("Q4 未答：一票否决清单沿用 v0.1 三条（越权 / PII / 不可逆无确认）")
    else:
        text = (a4.get("text", "") if isinstance(a4, dict) else str(a4))
        covered = [h for h in DEFAULT_VETO_HINTS if h in text]
        if covered and len(covered) == len(DEFAULT_VETO_HINTS) and len(text) <= 40:
            no_change.append("Q4 否决清单与 v0.1 hard_rules 重合，不产生 diff")
        else:
            diff.append(_diff(
                f"{PACK_PREFIX}/rubrics.yaml", "dimensions[id=risk].checklist",
                pack.get("risk_veto_rules"), None,
                f"用户提出的一票否决动作（{text[:60]}），清单待人定",
                ("rubrics.yaml", "hard_rules"), direction="add"))
            pending.append(f"Q4 新否决动作「{text[:40]}」：请补全 risk checklist 条目文字（clarify 不代写）")
            # 描述得足够具体时，只产 checker 契约提案（红线：不写实现）
            if len(text.strip()) >= 8:
                contracts.append({
                    "id": _contract_id(answers, contracts),
                    "task_id_pattern": "<待填>",
                    "input": "Trace.task + Trace.env_final_state + Trace.spans",
                    "output": "passed: boolean, evidence: string（引用 span_id）",
                    "criterion": text.strip(),
                    "status": "contract_proposal_only",
                })
                pending.append(f"checker 契约 {contracts[-1]['id']} 的命名与实现由人完成，clarify 不写代码")

    # --- Q5 回归口径
    a5 = answers.get("Q5")
    if _kind_of(a5) == "skipped":
        pending.append(f"Q5 未答：pass_k 沿用 pack 现值 {pack.get('pass_k')}")
    else:
        raw = a5.get("value") if isinstance(a5, dict) else a5
        k = _to_int(raw)
        if k is None:
            pending.append(f"Q5 给出非法 pass_k={raw!r}：tau2-bench 口径为整数，请给正整数")
        elif pack.get("pass_k") is None:
            pending.append("Q5 无法对比：thresholds.yaml 未扫描到 regress.pass_k")
        elif k == pack["pass_k"]:
            no_change.append(f"Q5 pass_k={k} 与 pack 现值一致，不产生 diff")
        else:
            diff.append(_diff(
                f"{PACK_PREFIX}/thresholds.yaml", "regress.pass_k", pack["pass_k"], k,
                "用户指定回归口径（Q5）", ("thresholds.yaml", "regress")))

    return diff, contracts, no_change, pending, flags


def run_session(source_text: str, answers: dict, pack: dict,
                prior_answered=(), golden_support: int = 0) -> dict:
    questions = build_questions(pack)
    candidates = [q for q in questions if q["id"] not in set(prior_answered)]
    asked = candidates[:QUESTION_BUDGET]
    over_budget = candidates[QUESTION_BUDGET:]  # 超预算问题直接丢弃，不呈现给人

    diff, contracts, no_change, pending, flags = compile_proposal(answers, pack)
    if golden_support < MIN_SUPPORT:
        flags.append("golden_pool_cold")
    if over_budget:
        flags.append("clarify_budget_exhausted")
        pending.append("访谈超出五问预算，" + str(len(over_budget)) + " 个问题未提出，按默认值收敛")

    status = "pending_approval" if (diff or contracts) else "zero_change"
    session = {
        "questions_asked": asked,
        "answers": answers,
        "proposal": {
            "status": status,
            "pack_diff": diff,
            "checker_contracts": contracts,
            "no_change_reason": no_change,
            "pending_items": pending,
        },
        "pending_items": pending,
        "degraded_flags": flags,
        "provenance": {
            "source_text": source_text,
            "pack_id": pack.get("pack_id"),
            "pack_version": pack.get("version"),
            "pack_state": pack.get("state"),
            "ledger_event_ids": [],
            "golden_pool_support": golden_support,
            "scan_gaps": pack.get("scan_gaps", []),
            "expected_decision_kind": "approve_pack_diff" if status == "pending_approval" else None,
        },
    }
    if pack.get("scan_gaps"):
        pending.extend(pack["scan_gaps"])
    return session
