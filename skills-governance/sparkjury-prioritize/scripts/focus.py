#!/usr/bin/env python3
"""focus.py — 呈现层温度（PM 的浓度滑杆）。

产品定义（2026-09-27 万凌，飞书评论原话）："和大模型输出时的 temperature 一样，
用温度决定输出的浓度"——拉得越低，只看最严重的 P0 级 badcase 重点修；
拉得越高，粒度从粗到细，出卡和需要人工决策的内容越多。

v0.1 落位（诚实边界）：这是**呈现层**温度——badcase 判定与聚类已经发生，
温度只决定"多少类浮上卡片/进入拍板队列"。**判定层**温度（badcase 入选阈值、
聚类粒度随温度变化）要动运行时与 pack，见 SKILL.md 的 v0.2 TODO 与接口需求文档。

三条纪律：
  1. 抑制 ≠ 删除：被温度滤掉的类进 suppressed 并逐条带 reason，绝不静默消失。
  2. 人的决策钉住：被 override 命中的类无视温度永远浮出——人工拍板不被滑杆压掉。
  3. None = 关：不传温度时输出与旧版本逐字节一致（默认行为零变化）。

档位是 v0.1 技能层常量（TODO pack v0.2：映射表进 thresholds.prioritize.focus，
由 clarify→govern 流程治理）：
  t < 0.35   → p0        只保留 severity_max==3 且占比≥10% 的类；拍板队列上限 1
  0.35–0.70  → standard  severity_max≥2；队列上限 3
  t ≥ 0.70   → full      全部浮出（含低严重度）；不设上限
"""
from __future__ import annotations

PROFILES = [
    # (上界exclusive, 名称, 最小severity, 最小share(None=不限), 队列上限(None=不限))
    (0.35, "p0", 3, 0.10, 1),
    (0.70, "standard", 2, None, 3),
    (1.01, "full", 0, None, None),
]

TODO_PACK = "focus 档位映射为 v0.1 技能层常量，待 pack v0.2 收编（thresholds.prioritize.focus）"


def profile_of(temperature: float, pack_profiles: list | None = None):
    """档位判定。pack v0.2 起映射收编进 thresholds.prioritize.focus（PM 批准链 evt-0002），
    传入 pack_profiles 时以 pack 为准；缺失时退回内置表（v0.1 兼容）并沿用其语义。"""
    t = min(max(float(temperature), 0.0), 1.0)
    if pack_profiles:
        for prof in pack_profiles:
            if t < float(prof.get("max_t", 1.01)):
                cap = prof.get("queue_cap")
                share = prof.get("min_share")
                return (str(prof.get("name", "?")), int(prof.get("min_severity", 0)),
                        float(share) if share is not None else None,
                        int(cap) if cap is not None else None)
        last = pack_profiles[-1]
        return (str(last.get("name", "full")), int(last.get("min_severity", 0)), None, None)
    for upper, name, min_sev, min_share, cap in PROFILES:
        if t < upper:
            return name, min_sev, min_share, cap
    return "full", 0, None, None  # pragma: no cover — 1.01 上界已兜住


def apply_temperature(result: dict, temperature: float | None, decided: set | None = None,
                      pack_profiles: list | None = None) -> dict:
    """rank() 之后调用。温度为 None 时原样返回（不加任何字段，保证旧输出逐字节不变）。

    返回的 result 中：ranked 只剩浮出的类（rank 重编号），top_recommendation 从浮出集
    重取（全部被抑制时为 None 并在 note 写明），meta.temperature 记录完整账目。
    """
    if temperature is None:
        return result
    name, min_sev, min_share, cap = profile_of(temperature, pack_profiles)
    # 钉住集 = 本轮 override 生效的类 ∪ ledger 里任意人类决策触碰过的类（decided）。
    # 后者是外部情报 W3 的落地：不可逆决策（如 reject_proposal「本轮不修」）绝不被
    # 高温度档静默吞掉——人看过的东西只能由人再收起来。
    pinned = {a.get("category_id") for a in result.get("override_applied") or []}
    pinned |= set(decided or ())

    surfaced, suppressed = [], []
    for r in result["ranked"]:
        cid = r.get("category_id")
        if cid in pinned:
            src = "override" if any((a.get("category_id") == cid) for a in result.get("override_applied") or []) else "human_decision"
            surfaced.append(dict(r, pinned_by_override=True, pinned_source=src))
            continue
        sev = r.get("severity")
        share = r.get("frequency")
        if isinstance(sev, (int, float)) and sev < min_sev:
            suppressed.append({"category_id": cid, "reason": f"severity {sev} < 当前档位门槛 {min_sev}（{name}）"})
            continue
        if min_share is not None and isinstance(share, (int, float)) and share < min_share:
            suppressed.append({"category_id": cid, "reason": f"占比 {round(share, 3)} < 当前档位门槛 {min_share}（{name}）"})
            continue
        surfaced.append(r)

    if cap is not None and len(surfaced) > cap:
        overflow = [r for r in surfaced[cap:] if not r.get("pinned_by_override")]
        keep_tail = [r for r in surfaced[cap:] if r.get("pinned_by_override")]
        for r in overflow:
            suppressed.append({"category_id": r.get("category_id"),
                               "reason": f"超出当前档位拍板队列上限 {cap}（{name}），按优先级截断"})
        surfaced = surfaced[:cap] + keep_tail

    for i, r in enumerate(surfaced, 1):
        r["rank"] = i

    top = None
    note = None
    if surfaced:
        prev_top = result.get("top_recommendation") or {}
        head = surfaced[0]
        if prev_top.get("category_id") == head.get("category_id"):
            top = prev_top
        else:
            top = {"category_id": head.get("category_id"),
                   "one_line_reason": head.get("rationale") or "",
                   "priority": head.get("priority")}
            note = f"原 top {prev_top.get('category_id')} 被当前温度抑制，top 顺延"
    else:
        note = f"当前温度（{name} 档）下没有满足门槛的类——这是如实结果，不是无 badcase；调高温度可见全部"

    result = dict(result)
    result["ranked"] = surfaced
    result["top_recommendation"] = top
    result["temperature"] = {
        "value": round(float(temperature), 3),
        "profile": name,
        "gates": {"min_severity": min_sev, "min_share": min_share, "queue_cap": cap},
        "surfaced": len(surfaced),
        "suppressed": suppressed,   # 逐条带 reason：抑制不是删除
        "pinned_by_override": sorted(pinned & {r.get("category_id") for r in surfaced}),
        "note": note,
        "profile_source": "pack" if pack_profiles else "builtin_v0.1",
    }
    return result
