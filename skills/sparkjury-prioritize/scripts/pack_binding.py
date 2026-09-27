#!/usr/bin/env python3
"""pack_binding.py — pack 身份绑定（prioritize.py 的拆分模块）。

v0.1 这里把 manifest 的 pack 段写死成 {pack_id: "retail-default", state_at_run: "FROZEN"}，
于是**在 DRAFT pack 上跑也标 FROZEN**，回归对比的"同 hash 铁律"被一个常量骗过。

现在 pack 身份只能来自两个地方：① --pack 指向的真实 pack.manifest.json；② 调用方显式传参。
两个都没有 → pack_bound=false + degraded flag pack_manifest_unbound，state_at_run=UNKNOWN。
宁可写"不知道"，不许写一个看起来像知道的常量。

模块划分（原单文件 623 行，超出 .claude/rules/coding.md 600 行硬上限后按职责拆出）：

  pack_binding.py  本文件：pack 身份绑定 + frozen_hash 校验（hash 算法复用根 tools/pack_freeze.py）
  ranking.py       优先级公式 compute_priority + 排序 rank
  overrides.py     黄金池 hook：override 事件解析、权威匹配键、apply_overrides
  pipeline.py      run() 编排 + IO 助手（hash / 落盘 / ledger 事件）
  prioritize.py    CLI 入口 + 上述模块的 re-export（对外 API 不变）
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

PACK_MANIFEST_NAME = "pack.manifest.json"
STATE_UNKNOWN = "UNKNOWN"
FLAG_PACK_UNBOUND = "pack_manifest_unbound"
FLAG_PACK_NOT_FROZEN = "pack_not_frozen"
FLAG_PACK_HASH_MISSING = "pack_frozen_hash_missing"


def load_pack_freeze():
    """import 项目根 tools/pack_freeze.py（权威 hash 算法），失败返回 None。

    与 skills/evalset/scripts/_contracts.py 同一模式：算法只有一份，不自造等价副本。
    向上逐级找 tools/pack_freeze.py，因此 skill 被复制到别处跑也能定位到所在仓。
    （sparkjury 适配：eval-agent 的实现在根 scripts/，sparkjury 迁到了根 tools/。）
    """
    here = Path(__file__).resolve()
    for base in here.parents:
        cand = base / "tools" / "pack_freeze.py"
        if cand.is_file():
            try:
                spec = importlib.util.spec_from_file_location(f"_pack_freeze_{id(cand)}", cand)
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)  # type: ignore[union-attr]
                if hasattr(mod, "compute_hash"):
                    return mod
            except Exception:  # noqa: BLE001 — 权威实现不可用时由调用方降级，不崩
                return None
    return None


def resolve_pack_binding(pack_dir: Path | None, pack_hash: str | None,
                         pack_id: str | None = None, pack_version: str | None = None) -> dict:
    """解出本轮 manifest 的 pack 段 + 应附带的 degraded flags。

    返回 {pack: {...}, flags: [...]}。pack 内恒有 pack_bound 与 pack_source 两个自证字段。
    --pack 给了但目录里没有可读的 pack.manifest.json → 抛 ValueError（显式失败，不猜）。
    """
    if pack_dir is None:
        # 没有 --pack：身份全部来自调用方传参，pack_id/version/state 无从验证。
        # pack_bound=false 恒假，flag 恒挂 —— 给了 --pack-hash 也只说明"知道一个 hash"，
        # 不说明这个 hash 对应哪份 pack、是不是 FROZEN。
        return {"pack": {"pack_id": pack_id, "version": pack_version,
                         "pack_hash": pack_hash, "state_at_run": STATE_UNKNOWN,
                         "pack_bound": False, "pack_source": "cli_args"},
                "flags": [FLAG_PACK_UNBOUND]}

    manifest_path = Path(pack_dir) / PACK_MANIFEST_NAME
    if not manifest_path.is_file():
        raise ValueError(f"--pack {pack_dir} 缺 {PACK_MANIFEST_NAME}；"
                         "不指定 --pack 时改为显式传参（此时记 pack_bound=false）")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise ValueError(f"{manifest_path} 不是合法 JSON: {e}") from e
    if not isinstance(manifest, dict):
        raise ValueError(f"{manifest_path} 顶层必须是 object")

    state = manifest.get("state")
    pack = {"pack_id": manifest.get("pack_id") or pack_id,
            "version": manifest.get("version") or pack_version,
            # 有 --pack 但 manifest 没写 state：这不是"没有 pack"，是"这份 manifest 缺字段"。
            # 统一写成 UNKNOWN（与未绑定同一形态），下游只需认一个 unknown 词。
            "state_at_run": state if isinstance(state, str) and state.strip() else STATE_UNKNOWN,
            "pack_bound": True,
            "pack_source": f"manifest:{manifest_path}"}
    flags = []

    freeze = load_pack_freeze()
    computed = None
    if freeze is not None:
        try:
            freeze.PACK_DIR = Path(pack_dir)  # type: ignore[attr-defined]
            computed = freeze.compute_hash()
        except Exception:  # noqa: BLE001 — 算不出 hash 只降级，不阻断排序
            computed = None

    declared = manifest.get("frozen_hash")
    if state == "FROZEN":
        if computed and declared and declared != computed:
            # FROZEN 却对不上内容：这已经不是"哪个 pack"的问题，而是"这个 hash 指的
            # 不是眼前这份内容"。带着它往下跑，产物的可复现声明就是假的 —— 显式失败。
            raise ValueError(
                f"{manifest_path}: state=FROZEN 但 frozen_hash 不符"
                f"（manifest={declared[:16]} 现场={computed[:16]}）；"
                "pack 冻结后又被改过，先走 govern 解冻/重新冻结再跑")
        if computed and not declared:
            flags.append(FLAG_PACK_HASH_MISSING)
    else:
        # DRAFT / 缺失：如实记，report 侧照常出卡但徽章不会冒充 FROZEN
        flags.append(FLAG_PACK_NOT_FROZEN)

    # pack_hash 取用顺序：显式传参 > manifest frozen_hash > 现场计算
    pack["pack_hash"] = pack_hash or declared or computed
    return {"pack": pack, "flags": flags}
