#!/usr/bin/env python3
"""overrides.py — 黄金池 override hook（prioritize.py 的拆分模块）。

PM 在 ledger 里 decision_kind=override_priority 的人工修正，下一轮排序必须消费
（把该类提升到人工曾给出的名次），并在 rationale 注明来源。

override payload 的权威匹配键（v0.2 联调补充，见 contracts/cross-skill-interfaces.md）：
  `taxonomy_id` = category_id（形如 F04）—— apply_overrides 唯一认的键；
  `target`      = cluster_id（形如 C03，PM 语义，report 侧规定，优先级的兼容路径）；
  `to_rank` / `system_top` / `human_top` = 人工名次与改序前后的 top 类。
两条历史路径都接：只有 target 且形如 F\\d\\d 时按 target 匹配（v0.1 遗物）；
匹配不上一律记进 `override_unmatched` 并在 stderr 打 debug 级说明，**不许静默丢弃**。

为什么不许静默丢弃：「PM 改了排序但下一轮没有任何反应」这种故障，
看起来跟「根本没人改过」一模一样 —— 这正是 v0.1 真实发生的事。

模块划分见 pack_binding.py 顶部说明。
"""
from __future__ import annotations

import json
import re
from pathlib import Path

# category_id（taxonomy 标签）的形状。用来把 v0.1 遗物「target 直接写 F04」与
# v0.2 规定的「target = cluster_id（形如 C03）」区分开 —— 只有前者能走兼容路径。
CATEGORY_ID_RE = re.compile(r"^F\d{2}$")


def override_match_key(payload: dict) -> tuple:
    """从 decision payload 解出 apply_overrides 的权威匹配键。返回 (key, source)。

    优先级（v0.2 联调补充，contracts/cross-skill-interfaces.md）：
      1. payload.taxonomy_id —— report 侧规定的权威键，值 = cluster 的 label，
         也就是本文件 by_id 的键（category_id）。PM 点「换一类先修」时必须带上它。
      2. payload.target 且形如 F\\d\\d —— v0.1 兼容路径：那会儿 target 直接写
         taxonomy id（prioritize SKILL.md 的旧示例即如此）。cluster_id 形如 C03，
         形状不同，不会被误判成 taxonomy id。
    两者都取不到 → (None, "missing")：调用方必须记一条 unmatched，不许静默丢弃。
    """
    tid = payload.get("taxonomy_id")
    if isinstance(tid, str) and CATEGORY_ID_RE.match(tid.strip()):
        return tid.strip(), "payload.taxonomy_id"
    target = payload.get("target")
    if isinstance(target, str) and CATEGORY_ID_RE.match(target.strip()):
        return target.strip(), "payload.target(legacy)"
    return None, "missing"


def load_overrides(path: Path) -> list:
    """读 ledger 里的 decision_kind=override_priority 事件（JSONL，一条一行）。

    契约见 contracts/event-ledger.schema.json：decision 事件必须带
    decision_kind / actor / target；人工新名次记 payload.to_rank（缺省视为只标记不改序）。
    v0.2 起 payload 另带 taxonomy_id / system_top / human_top：
      taxonomy_id = 权威匹配键（=category_id）；system_top / human_top 供 govern
      投影 3 做 fixability 先验校准，本 skill 只透传不解释。
    """
    events = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        e = json.loads(line)
        if not isinstance(e, dict) or e.get("event_type") != "decision":
            continue
        payload = e.get("payload") or {}
        if payload.get("decision_kind") != "override_priority":
            continue
        to_rank = payload.get("to_rank")
        try:
            to_rank = int(to_rank) if to_rank is not None else None
        except (TypeError, ValueError):
            to_rank = None
        key, key_src = override_match_key(payload)
        events.append({
            "event_id": e.get("event_id"),
            "actor": payload.get("actor"),
            "target": payload.get("target"),
            "taxonomy_id": payload.get("taxonomy_id"),
            "match_key": key,
            "match_key_source": key_src,
            "to_rank": to_rank,
            "system_top": payload.get("system_top"),
            "human_top": payload.get("human_top"),
            "rationale": payload.get("rationale", ""),
        })
    return events


def apply_overrides(ranked: list, overrides: list) -> tuple:
    """把人工曾给出的修正应用到新一轮排序上。

    语义（刻意保守）：只提升、不压低。若该类本轮排名已经 ≤ 人工名次，
    说明人的判断与公式一致或更靠前，不动。提升后其余顺延。
    幂等：同一 override 事件重复应用结果相同。

    匹配只用 match_key（v0.1 兼容路径见 override_match_key 的 docstring）。
    匹配不上的事件**不静默丢弃**：进 unmatched，每条带 reason 与 debug 级说明，
    由调用方写进 priority.json 的 meta.override_unmatched 与 ledger。
    否则「PM 改了排序但下一轮没有任何反应」这种故障，
    看起来跟「根本没人改过」一模一样 —— 这正是 v0.1 真实发生的事。

    返回 (新列表, 应用记录, 未生效记录)。
    """
    by_id = {r["category_id"]: i for i, r in enumerate(ranked)}

    def _order(o):
        """能生效的（键命中 + 有 to_rank）按名次升序先处理，避免前移时索引偏移。"""
        usable = bool(o.get("match_key") in by_id and o.get("to_rank"))
        return (0 if usable else 1,
                o.get("to_rank") if usable else 1 << 30,
                str(o.get("event_id")))

    applied, used = [], set()
    unmatched = []
    for ev in sorted(overrides, key=_order):
        cid = ev.get("match_key")
        to = ev.get("to_rank")
        base = {"event_id": ev.get("event_id"), "actor": ev.get("actor"),
                "target": ev.get("target"), "taxonomy_id": ev.get("taxonomy_id"),
                "to_rank": to, "rationale": ev.get("rationale")}
        if cid is None:
            unmatched.append({**base, "level": "debug",
                              "reason": "override_payload_missing_taxonomy_id",
                              "detail": ("payload 无 taxonomy_id 且 target 不是 F\\d\\d 形状；"
                                         "report 侧 override_priority 事件必须带 taxonomy_id"
                                         "（=category_id）才能被下一轮排序消费")})
            continue
        if cid not in by_id:
            unmatched.append({**base, "level": "debug",
                              "reason": "taxonomy_id_not_in_this_round",
                              "detail": (f"taxonomy_id={cid} 不在本轮 ranked 里"
                                             "（可能已修好、被拒排或落进 F99）；"
                                             "人工决策留在 ledger，本轮无对象可应用")})
            continue
        if not to or to < 1:
            unmatched.append({**base, "level": "debug",
                              "reason": "to_rank_missing_marker_only",
                              "detail": ("payload 无可用 to_rank，按既定语义视为"
                                         "「只标记不改序」，本轮排序未改动")})
            continue
        if cid in used:
            continue  # 同一类只按最靠前的名次提升一次
        cur = by_id[cid]
        if cur + 1 <= to:
            continue  # 已不低于人工名次，不动
        item = ranked.pop(cur)
        ranked.insert(to - 1, item)
        by_id = {r["category_id"]: i for i, r in enumerate(ranked)}
        applied.append({**base, "category_id": cid,
                        "match_key_source": ev.get("match_key_source"),
                        "from_rank": cur + 1, "to_rank": to})
        used.add(cid)
    return ranked, applied, unmatched
