#!/usr/bin/env python3
"""pack_scan.py — scenario-pack 的只读扫描层（clarify.py 的拆分模块）。

把 pack 收敛成 clarify 需要的事实表（dimensions / weights / taxonomy / thresholds /
risk_veto_rules / pass_k / primary_metric）。任何字段缺失都记进 `scan_gaps`，
由调用方转成 pending_items —— 扫不到就明说扫不到，**禁止凭记忆填写 current 值**。

YAML 读取统一走项目唯一权威实现 tools/_yaml_lite.py（2026-09-26 收敛）。
原来本 skill 有 3 个自建的"数行扫描器"（read_rubric_dimensions / read_list_items_by_id /
scan_dict_scalars），各自把值读成字符串且互不一致。收敛后：先丢给权威解析器拿结构化
文档，再按各自的返回形状投影。

只管读，不写、不冻结、不补默认值。模块划分见 _contracts.py 顶部说明。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

# sparkjury 适配（2026-09-27 移植）：eval-agent 的根工具在 scripts/，sparkjury 在 tools/。
PROJECT_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(PROJECT_ROOT / "tools"))

from _yaml_lite import load_yaml_text  # noqa: E402

from _contracts import DEFAULT_VETO_HINTS, DIMENSIONS, PackReadError, _to_int  # noqa: E402


def project_root() -> Path:
    return Path(__file__).resolve().parents[3]


def read_manifest(pack_dir: Path) -> dict:
    path = pack_dir / "pack.manifest.json"
    return json.loads(path.read_text(encoding="utf-8"))


def _doc(text: str) -> dict:
    """读 pack 的 YAML 成 dict，解析权在权威模块。顶层非 mapping 按空 dict 处理。"""
    doc = load_yaml_text(text, source="<pack>")
    return doc if isinstance(doc, dict) else {}


def read_rubric_dimensions(text: str) -> dict:
    """rubrics.yaml 的 dimensions 列表项：{id: {weight, method, checklist[], ...原值}}

    曾是行扫描器：`checklist` 是把该维度下所有 `- ` 行都并进来的（连 `forbidden` 的条目
    也算进去），且值一律是字符串。现在直接取权威解析出的维度 dict，checklist/forbidden
    各自归位、数值是真数值。risk 维实际没有 forbidden，所以 Q4 的否决规则判定结果不变。
    """
    dims = {}
    for d in _doc(text).get("dimensions") or []:
        if not (isinstance(d, dict) and d.get("id")):
            continue
        body = dict(d)
        body.setdefault("checklist", [])
        dims[str(d["id"])] = body
    return dims


def read_list_items_by_id(text: str) -> dict:
    """扫描 `- id: XXX` 形式的列表，返回 {id: {子键: 值}}。值类型与 PyYAML 一致。"""
    items = {}
    for it in _doc(text).get("categories") or _doc(text).get("items") or []:
        if isinstance(it, dict) and it.get("id"):
            items[str(it["id"])] = dict(it)
    return items


def scan_dict_scalars(text: str) -> dict:
    """把纯字典结构 YAML 压平成 dotted path -> 叶子值。

    曾是逐行扫描器，只能到两层且把值留成字符串；现在直接压权威解析出的 dict。
    返回形状保持 {dotted path: 值}。旧实现对列表项/锚点/流式集合整段跳过 —— 本实现
    保留"压平到叶子"的行为，因此 pack 里的 flow 映射（如 severity_weights）也会按
    `父.键` 展开成一条，是个严格更全的超集，不会让任何原有 dotted path 消失。
    """
    paths: dict = {}

    def walk(node, prefix: str) -> None:
        if isinstance(node, dict) and node:
            for k, v in node.items():
                walk(v, f"{prefix}.{k}" if prefix else str(k))
        elif prefix:
            paths[prefix] = node

    walk(_doc(text), "")
    return paths


def read_pack(pack_dir: Path) -> dict:
    """把 pack 收敛成 clarify 需要的事实表。任何字段缺失都记进 scan_gaps。"""
    gaps = []
    facts = {"pack_dir": str(pack_dir), "scan_gaps": gaps}

    try:
        manifest = read_manifest(pack_dir)
    except (OSError, json.JSONDecodeError) as exc:
        raise PackReadError(f"pack.manifest.json 不可读: {exc}") from exc
    facts["pack_id"] = manifest.get("pack_id")
    facts["version"] = manifest.get("version")
    facts["state"] = manifest.get("state")

    rubrics_path = pack_dir / "rubrics.yaml"
    dims = read_rubric_dimensions(rubrics_path.read_text(encoding="utf-8")) if rubrics_path.exists() else {}
    facts["dimensions"] = dims
    weights = {}
    for d, body in dims.items():
        if "weight" not in body:
            continue
        try:
            weights[d] = float(body["weight"])
        except (TypeError, ValueError):
            # 扫不到合法数值就记缺口，不让 Q2 拿一个崩掉的进程去"填"权重
            gaps.append(f"rubrics.yaml 维度 {d} 的 weight 不是数值（{body['weight']!r}）")
    facts["weights"] = weights
    for d in DIMENSIONS:
        if d not in dims:
            gaps.append(f"rubrics.yaml 未扫描到维度 {d}")
    facts["outcome_method"] = dims.get("outcome", {}).get("method")

    tax_path = pack_dir / "taxonomy.yaml"
    tax = read_list_items_by_id(tax_path.read_text(encoding="utf-8")) if tax_path.exists() else {}
    facts["taxonomy"] = tax
    facts["f08_severity"] = tax.get("F08", {}).get("default_severity")
    if facts["f08_severity"] is None:
        gaps.append("taxonomy.yaml 未扫描到 F08.default_severity")

    # Q4 的 current：risk 维 checklist 里形如一票否决的条目（由扫描得出，不手写）
    risk_checklist = dims.get("risk", {}).get("checklist", [])
    facts["risk_veto_rules"] = [
        item for item in risk_checklist
        if any(h in item for h in DEFAULT_VETO_HINTS) or "否决" in item
    ]

    thr_path = pack_dir / "thresholds.yaml"
    thr = scan_dict_scalars(thr_path.read_text(encoding="utf-8")) if thr_path.exists() else {}
    facts["thresholds"] = thr
    facts["pass_k"] = _to_int(thr.get("regress.pass_k"))
    if facts["pass_k"] is None:
        gaps.append("thresholds.yaml 未扫描到 regress.pass_k，Q5 将只给方向不给数值")
    # primary_metric 现在由权威解析器直接给字符串；strip('"') 是对旧扫描器
    # （值可能残留引号）的残留兼容，对已是裸串的输入是 no-op。
    facts["primary_metric"] = str(thr.get("regress.gates.primary_metric") or "").strip('"') or None
    return facts
