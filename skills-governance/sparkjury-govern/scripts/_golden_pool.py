#!/usr/bin/env python3
"""_golden_pool.py — govern 的黄金决策池投影层。

从 govern.py 拆出，原因：单文件超过 600 行违反 .claude/rules/coding.md（必须拆分）。
本模块只做统计投影：读 pack 事实 + 读 ledger 决策 → proposals[]。
**只提案、绝不 apply**：govern 从不给出 proposed 数值，也不改 pack 内容文件。

三件事：
  1. read_facts —— 取提案里 current 值/下一个可用 F 类 id 所需的 pack 事实（只读）。
  2. audit_override_payloads —— override_priority 事件的"可投影性"审计。
     投影 3 出不来信号时，必须先能分清"没有信号"和"上游没采到字段"，
     否则两者在输出里长得一模一样。
  3. project_golden_pool / build_proposals —— 三类统计投影 + proposals.json 的 payload。
"""
from __future__ import annotations

import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

# sparkjury 适配（2026-09-27 移植）：eval-agent 的根工具在 scripts/，sparkjury 在 tools/。
PROJECT_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(PROJECT_ROOT / "tools"))

from _yaml_lite import load_yaml_file, load_yaml_text  # noqa: E402

from _ledger import now_iso, read_json  # noqa: E402

# 契约常量（非 pack 阈值）：单人 PM 场景下同一信号重复 2 次即构成模式。
# 黄金池早期数据稀薄，阈值设高则信号永远出不来。
MIN_SUPPORT = 2

# pack 在 sparkjury 仓库内的相对路径前缀（eval-agent 原态在根 scenario-pack/）。
# proposals 的 target_file / candidate_fields 由它构造：提案指向的路径必须在本仓库里真实存在，
# 否则人拿着 proposals.json 找不到要改的那份文件。
PACK_PREFIX = "standards/scenario-pack"


def read_dimensions(text: str) -> dict:
    """rubrics.yaml 的 dimensions 段 → {id: 维度定义 dict}。解析权在 _yaml_lite.py。

    曾是逐行扫描器，把每个维度都读成“字符串值”的 dict（weight: "0.40"）。
    现在交给权威实现，值的类型与 PyYAML 一致（weight: 0.40 浮点）；
    下游只用 float(body.get("weight", "")) 取数，两种写法等价。
    """
    doc = load_yaml_text(text, source="<rubrics>") or {}
    return {d["id"]: d for d in (doc.get("dimensions") or []) if isinstance(d, dict) and d.get("id")}


def read_facts(pack_dir: Path) -> dict:
    """取提案里 current 值/下一个可用 F 类 id 所需的 pack 事实（解析走 _yaml_lite.py）。"""
    facts = {"severity": {}, "weights": {}, "free_category_ids": [], "pass_k": None}
    tax = Path(pack_dir) / "taxonomy.yaml"
    if tax.exists():
        for c in load_yaml_file(tax).get("categories") or []:
            if not isinstance(c, dict) or not c.get("id"):
                continue
            if c.get("default_severity") is not None:
                facts["severity"][str(c["id"])] = c["default_severity"]
            m = re.fullmatch(r"F(\d\d)", str(c["id"]))
            if m:
                facts["free_category_ids"].append(int(m.group(1)))
    rub = Path(pack_dir) / "rubrics.yaml"
    if rub.exists():
        dims = read_dimensions(rub.read_text(encoding="utf-8"))
        for d, body in dims.items():
            try:
                facts["weights"][d] = float(body.get("weight", ""))
            except (TypeError, ValueError):
                pass
    return facts


def _group_key(target: str) -> str:
    """target 形如 card:F02 / card:outcome / cluster:8 / priority:F02 / pack_diff:P-001"""
    return str(target).split(":", 1)[1] if ":" in str(target) else str(target)


# override_priority 事件里投影 3 必需的两个字段。v0.1 时 report 侧从未规定过它们，
# 于是这个投影永远吃不到数据 —— 没有任何报错，只是"从来没有信号"。
# 现在 contracts/cross-skill-interfaces.md §B 把 system_top / human_top 写成必填，这里按期收。
OVERRIDE_TOP_FIELDS = ("system_top", "human_top")


def audit_override_payloads(events: list) -> dict:
    """统计 override_priority 事件能不能喂饱投影 3。只统计、不修补、不猜缺字段。

    返回 {usable, unusable, reasons:{reason: [event_id...]}, taxonomy_ids:[...]}。
    缺 system_top/human_top 的事件仍是有效决策（PM 确实拍了板），只是不足以做
    fixability 先验校准 —— 必须能被人看见差在哪，否则"没有信号"和"没采到数据"
    在输出里长得一模一样。
    """
    usable, unusable = 0, 0
    reasons: dict = defaultdict(list)
    taxonomy_ids = set()
    for ev in events:
        if ev.get("event_type") != "decision":
            continue
        p = ev.get("payload") or {}
        if p.get("decision_kind") != "override_priority":
            continue
        tid = p.get("taxonomy_id")
        if isinstance(tid, str) and re.fullmatch(r"F\d\d", tid.strip()):
            taxonomy_ids.add(tid.strip())
        missing = [f for f in OVERRIDE_TOP_FIELDS if not (p.get(f) or "").strip()]
        if missing:
            unusable += 1
            reasons["override_payload_missing_" + "_and_".join(missing)].append(ev["event_id"])
            continue
        sys_top, hum_top = p["system_top"].strip(), p["human_top"].strip()
        for name, val in (("system_top", sys_top), ("human_top", hum_top)):
            if not re.fullmatch(r"F\d\d", val):
                unusable += 1
                reasons[f"override_payload_{name}_not_category_id"].append(ev["event_id"])
                break
        else:
            usable += 1
    return {"usable": usable, "unusable": unusable, "reasons": dict(reasons),
            "taxonomy_ids": sorted(taxonomy_ids)}


def project_golden_pool(events: list, facts: dict) -> dict:
    """三种统计投影。只统计、不裁决：govern 从不给出 proposed 数值。"""
    decisions = [e for e in events if e.get("event_type") == "decision"]
    accept, renames, overrides = defaultdict(lambda: {"accept": [], "reject": []}), defaultdict(list), defaultdict(list)

    for ev in decisions:
        p = ev.get("payload") or {}
        kind, target = p.get("decision_kind"), str(p.get("target", ""))
        key = _group_key(target)
        if kind == "accept_card":
            accept[key]["accept"].append(ev["event_id"])
        elif kind == "reject_proposal":
            accept[key]["reject"].append(ev["event_id"])
        elif kind == "rename_cluster":
            label = (p.get("new_label") or "").strip()
            if label:
                renames[label].append({"event_id": ev["event_id"], "cluster": key,
                                       "old_label": p.get("old_label")})
        elif kind == "override_priority":
            system_top = (p.get("system_top") or "").strip()
            human_top = (p.get("human_top") or "").strip()
            # 只收形状完整的一对（都必须是 F\d\d 的 category_id）；
            # 缺字段或把 cluster_id 混进来的事件由 audit_override_payloads 计数曝光，
            # 不在这里偷偷用 None / "C03" 当键 —— 那会把"没采到"或"上游写错了"
            # 伪装成一个能校准 pack 的反例。
            if re.fullmatch(r"F\d\d", system_top) and re.fullmatch(r"F\d\d", human_top):
                overrides[(system_top, human_top)].append(
                    {"event_id": ev["event_id"],
                     "taxonomy_id": (p.get("taxonomy_id") or "").strip() or None})

    proposals = []

    # 投影 1：PM 常 accept/reject 哪类建议 -> 优先级权重校准信号
    for key, rec in sorted(accept.items()):
        ids = rec["accept"] + rec["reject"]
        if len(ids) < MIN_SUPPORT:
            continue
        rate = round(len(rec["accept"]) / len(ids), 3)
        if re.fullmatch(r"F\d\d", key):
            primary = (f"{PACK_PREFIX}/taxonomy.yaml", f"categories[id={key}].default_severity")
            current = facts["severity"].get(key)
            candidates = [f"{PACK_PREFIX}/thresholds.yaml:prioritize.fixability_boost.{key}",
                          f"{PACK_PREFIX}/thresholds.yaml:prioritize.severity_weights"]
        else:
            primary = (f"{PACK_PREFIX}/rubrics.yaml", f"dimensions[id={key}].weight")
            current = facts["weights"].get(key)
            candidates = [f"{PACK_PREFIX}/rubrics.yaml:dimensions[].weight"]
        proposals.append({
            "signal": "priority_weight_calibration",
            "evidence": {"decision_kind": "accept_card + reject_proposal", "key": key,
                         "support": len(ids), "accept_count": len(rec["accept"]),
                         "reject_count": len(rec["reject"]), "accept_rate": rate,
                         "event_ids": ids},
            "target_file": primary[0], "field": primary[1], "current": current,
            "proposed": None, "direction": "raise" if rate > 0.5 else "lower",
            "candidate_fields": candidates,
        })

    # 投影 2：PM 常 rename 哪些簇 -> taxonomy 缺口信号
    for label, hits in sorted(renames.items(), key=lambda kv: -len(kv[1])):
        if len(hits) < MIN_SUPPORT:
            continue
        used = set(facts.get("free_category_ids", []))  # 13 起未被占用即下一个新类 id
        nxt = next((f"F{n:02d}" for n in range(13, 100) if n not in used), "F99")
        proposals.append({
            "signal": "taxonomy_gap",
            "evidence": {"decision_kind": "rename_cluster", "recurring_human_label": label,
                         "support": len(hits), "clusters": sorted({h["cluster"] for h in hits}),
                         "event_ids": [h["event_id"] for h in hits]},
            "target_file": f"{PACK_PREFIX}/taxonomy.yaml", "field": f"categories[] (新增 {nxt})",
            "current": None, "proposed": None, "direction": "add",
            "candidate_fields": [f"{PACK_PREFIX}/taxonomy.yaml:categories[]"],
        })

    # 投影 3：PM 常 override 哪些排序 -> fixability 先验校准信号
    for (system_top, human_top), hits in sorted(overrides.items(), key=lambda kv: -len(kv[1])):
        ids = [h["event_id"] for h in hits]
        if len(ids) < MIN_SUPPORT or not system_top:
            continue
        proposals.append({
            "signal": "fixability_prior_calibration",
            "evidence": {"decision_kind": "override_priority", "system_top": system_top,
                         "human_top": human_top, "support": len(ids), "event_ids": ids,
                         "taxonomy_ids": sorted({h["taxonomy_id"] for h in hits if h["taxonomy_id"]})},
            "target_file": f"{PACK_PREFIX}/thresholds.yaml",
            "field": f"prioritize.fixability_boost.{system_top}",
            "current": None, "proposed": None, "direction": "lower",
            "candidate_fields": [f"{PACK_PREFIX}/thresholds.yaml:prioritize.fixability_boost.{system_top}",
                                 f"{PACK_PREFIX}/thresholds.yaml:prioritize.severity_weights"],
        })

    for i, p in enumerate(sorted(proposals, key=lambda x: -x["evidence"]["support"]), start=1):
        p["proposal_id"] = f"P-{i:03d}"
        p["status"] = "pending_human_approval"
        p["required_decision_kind"] = "approve_pack_diff"
        p["notes"] = "proposal ≠ 生效：需人批准后由 clarify 编译为 pack diff，govern 不自动 apply"
    return proposals


def build_proposals(events: list, pack_dir: Path, ledger_path: Path, corrupt: int) -> dict:
    """ledger 的 decision 事件 + pack 事实 -> proposals.json 的完整 payload。"""
    facts = read_facts(pack_dir)
    manifest_path = Path(pack_dir) / "pack.manifest.json"
    manifest = read_json(manifest_path) if manifest_path.exists() else {}
    proposals = project_golden_pool(events, facts)
    summary = Counter(p["signal"] for p in proposals)
    audit = audit_override_payloads(events)
    return {
        "generated_at": now_iso(),
        "pack": {"pack_id": manifest.get("pack_id"), "version": manifest.get("version"),
                 "state": manifest.get("state"), "pack_dir": str(pack_dir)},
        "ledger": {"path": str(ledger_path), "events": len(events),
                   "decisions": sum(1 for e in events if e.get("event_type") == "decision"),
                   "corrupt_lines": corrupt},
        "support_threshold": MIN_SUPPORT,
        "proposals": proposals,
        "summary": {"priority_weight_calibration": summary["priority_weight_calibration"],
                    "taxonomy_gap": summary["taxonomy_gap"],
                    "fixability_prior_calibration": summary["fixability_prior_calibration"]},
        # override 决策的"可投影性"审计：投影 3 出不来信号时，先看这里是不是上游没采到字段
        "override_audit": audit,
    }

def thin_candidates(runs_dir, pack_dir) -> dict:
    """Add/Thin 原则（外部情报 2026-09-27：「Harness 组件不是永久资产，去留由真实任务
    的运行证据决定」）落到 pack：扫描历史 run 产物，找出**从未被任何 badcase 消费过**
    的 taxonomy 类与 fixability 系数——它们是下一次 pack 解冻时的 Thin 候选。

    只给方向不动手：输出候选清单与证据基数，删不删由人走 clarify→govern 流程。
    数据源两种形状都认：runs/<id>/clusters.json（接口②落地后）与
    runs/<id>/card/card.json（现状）。扫不到任何 run 时如实返回 basis_runs=0，
    不产生候选——零证据不能当"没人用"的证据。
    """
    import json as _json
    from pathlib import Path as _P
    runs = _P(runs_dir)
    observed, n_runs = set(), 0
    if runs.is_dir():
        for d in sorted(runs.iterdir()):
            if not d.is_dir():
                continue
            labels = set()
            for cand in (d / "clusters.json", d / "card" / "card.json"):
                if not cand.exists():
                    continue
                try:
                    doc = _json.loads(cand.read_text(encoding="utf-8"))
                except Exception:
                    continue
                for cl in (doc.get("clusters") or []):
                    lab = cl.get("label") or cl.get("taxonomy_id")
                    if isinstance(lab, str) and lab:
                        labels.add(lab)
            if labels:
                observed |= labels
                n_runs += 1
    pack = _P(pack_dir)
    defined, fix_keys = set(), set()
    tax = pack / "taxonomy.yaml"
    label_to_id = {}
    if tax.exists():
        for c in load_yaml_file(tax).get("categories") or []:
            if isinstance(c, dict) and c.get("id"):
                defined.add(str(c["id"]))
                if c.get("runtime_label"):
                    label_to_id[str(c["runtime_label"])] = str(c["id"])
    # pack v0.2 起 taxonomy 自带 runtime_label：观测到的运行时标签翻译成规范 id 再比对，
    # namespace_mismatch 防线只在真正没有映射时触发
    observed = {label_to_id.get(l, l) for l in observed}
    thr = pack / "thresholds.yaml"
    if thr.exists():
        pri = (load_yaml_file(thr).get("prioritize") or {})
        fix_keys = {str(k) for k in (pri.get("fixability_boost") or {}).keys()}
    if n_runs == 0:
        return {"basis_runs": 0, "observed_labels": [], "unobserved_categories": [],
                "unused_fixability_keys": [],
                "note": "无可用 run 产物——零证据不构成 Thin 依据，不产生候选"}
    protected = {"F99"}  # 人工兜底队列永不进 Thin 候选
    if observed and defined and not (observed & defined):
        # 观测标签与 pack 类零交集 = 两套命名空间没对齐（正是 D3 待裁决的
        # FailureLabel vs F 编号差异）。此时"全部 F 类都没被消费"是空间错配的
        # 假象，不是 Thin 证据——如实报告并拒绝产生候选，防止误删整套分类。
        return {"basis_runs": n_runs, "observed_labels": sorted(observed),
                "unobserved_categories": [], "unused_fixability_keys": [],
                "namespace_mismatch": True,
                "note": ("观测标签与 pack 分类零交集（如运行时 FailureLabel vs pack F 编号，"
                         "见 D3 裁决）——先统一命名空间再谈 Add/Thin，本轮不产生候选")}
    return {
        "basis_runs": n_runs,
        "observed_labels": sorted(observed),
        "unobserved_categories": sorted((defined - observed) - protected),
        "unused_fixability_keys": sorted((fix_keys - observed) - protected),
        "note": ("Thin 候选=定义了但 N 轮 run 里从未出现的类/系数；只是解冻时的讨论清单，"
                 "删除必须走 clarify 提案→人批准→govern 冻结"),
    }

