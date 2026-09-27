#!/usr/bin/env python3
"""_contracts.py — calibrate 的输入契约层（异常 / 序列化 / 阈值读取）。

从 compute_profile.py 拆出，原因：单文件 600 行上限。

YAML 说明：本 skill **不复制**任何最小 YAML 子集解析器。跨 skill 复制解析器会让
两份实现各自漂移，而 pack 的读法必须是全库唯一的 —— 所以 calibrate 一律走项目唯一
权威实现 `tools/_yaml_lite.py`（2026-09-26 收敛，v0.1 时这里是"传 JSON 视图否则报错"，
现在 YAML 也能直接读了，且与其余 skill 逐字节同解）。

sparkjury 适配（2026-09-27 移植）：eval-agent 的权威实现在根 `scripts/`，sparkjury 的
根工具在 `tools/`，故 sys.path 目标由 `parents[3]/"scripts"` 改为 `parents[3]/"tools"`。
"""
from __future__ import annotations

import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(PROJECT_ROOT / "tools"))

from _yaml_lite import YamlLiteError, load_yaml_file  # noqa: E402

__all__ = ["YamlLiteError", "load_yaml_file", "load_thresholds"]


class UsageError(Exception):
    """参数或输入文件问题。exit 2。"""


class PreflightError(Exception):
    """输入不可用（同族红线 / 画像阻断 / schema 不符）。exit 3。"""


class ProfileError(Exception):
    """画像构造自检失败。exit 1。"""


def canonical(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def canonical_hash(obj) -> str:
    return sha256_text(canonical(obj))


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def read_json(path: Path, keys=()):
    if not path.is_file():
        raise UsageError(f"输入文件不存在: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise UsageError(f"{path} 不是合法 JSON: {exc}") from exc
    if isinstance(payload, dict) and keys:
        for k in keys:
            if isinstance(payload.get(k), list):
                return payload[k]
    return payload


def load_thresholds(path: Path | None) -> dict:
    """读阈值配置。.yaml/.yml 走权威 YAML 实现；.json 按 JSON 读。不静默降级。"""
    if path is None:
        return {}
    if not path.is_file():
        raise UsageError(f"阈值文件不存在: {path}")
    if path.suffix in (".yaml", ".yml"):
        try:
            return load_yaml_file(path)
        except YamlLiteError as exc:
            # 解析失败不是"环境缺依赖"，是"这份阈值读不出来"。两种都不许拿默认值顶上：
            # 读不到的阈值会让门禁结论看起来像判过。
            raise UsageError(f"{path} 不可用（YAML 解析失败）: {exc}") from exc
    with path.open(encoding="utf-8") as fh:
        return json.load(fh)


def mean(xs) -> float:
    xs = list(xs)
    return sum(xs) / len(xs) if xs else 0.0


def variance(xs) -> float:
    """样本方差（ddof=1）。n<2 时返回 0.0 并在调用方按 n 判读。"""
    xs = list(xs)
    if len(xs) < 2:
        return 0.0
    m = mean(xs)
    return sum((x - m) ** 2 for x in xs) / (len(xs) - 1)


def percentile(xs, q: float):
    """线性插值分位数（q ∈ [0,1]）。空输入返回 None。"""
    xs = sorted(xs)
    if not xs:
        return None
    if len(xs) == 1:
        return float(xs[0])
    pos = q * (len(xs) - 1)
    lo = int(pos)
    hi = min(lo + 1, len(xs) - 1)
    frac = pos - lo
    return float(xs[lo] * (1 - frac) + xs[hi] * frac)


# --------------------------------------------------------------------------- #
# event ledger：契约层。schemas 见 contracts/event-ledger.schema.json
# --------------------------------------------------------------------------- #
def ledger_events(run_id: str, skill: str, steps: list, artifact: str, digest: str,
                  header_payload: dict | None = None) -> list:
    """按 SOP 步骤生成 plan/started/progress*/checkpoint/completed 事件序列。

    事件 id 本地自增（append-only 账本里的行 id 由 ledger 自身持有，这里只保证顺序稳定）。
    `ts` 故意不在这里填 —— 由 append_ledger 落笔时打，保证事件顺序与写入顺序一致。
    """
    base = {"run_id": run_id, "skill": skill}
    events = [
        {"event_id": "1", "event_type": "plan", **base,
         "payload": {"entry": skill, "steps": steps, **(header_payload or {})}},
        {"event_id": "2", "event_type": "started", **base,
         "payload": {"stage": header_payload or {}}},
    ]
    for i, name in enumerate(steps, start=3):
        events.append({"event_id": str(i), "event_type": "progress", **base, "payload": {"step": name}})
    events.append({"event_id": "98", "event_type": "checkpoint", **base,
                   "payload": {"artifact": artifact, "hash": digest, "resumable_from": True}})
    events.append({"event_id": "99", "event_type": "completed", **base,
                   "payload": {"artifact": artifact, "hash": digest}})
    return events


def append_ledger(path, events: list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        for ev in events:
            ev = dict(ev)
            ev.setdefault("ts", utc_now())
            fh.write(canonical(ev) + "\n")
