"""Scenario pack 的运行时读取入口（M6/M7/M8 共用）。

pack（`standards/scenario-pack/`）是冻结的：改它里面任何文件，`tools/pack_freeze.py --verify` 算出的
hash 就变了，`tools/verify.sh` 第 5 关立刻红。所以这里只读，而且读的是 `pack.manifest.json` 里声明的
`frozen_hash`，不自己重算——重算是 `pack_freeze --verify` 的活，runtime 再算一遍只会得到一个和回归
对比无关的新数字。

坐标：默认仓库根下的 `standards/scenario-pack`，环境变量 `SPARKJURY_PACK_DIR` 可以指到别处（测试、
节点上换 pack 都走它）。运行时装的是 wheel、仓库里没有 pack，也只能靠它。

缺 pack 一律降级成 `None` / `{}`，不抛异常：`sparkjury run --demo` 不该因为 pack 不在就跑不起来。
真正需要 pack 的地方（回归门禁判定「两侧 pack 身份是否相同」）会把「读不到」当成一个显式的 skip 项
写进报告，而不是当成通过。
"""

from __future__ import annotations

import importlib.util
import json
import os
from functools import lru_cache
from pathlib import Path
from typing import Any

PACK_ENV = "SPARKJURY_PACK_DIR"
LABEL_MAP_ENV = "SPARKJURY_LABEL_MAP"

#: 门禁阈值在 pack 里缺键时用的默认值。会连同来源一起写进门禁报告，避免把默认值当成 pack 的约定。
DEFAULT_DELTA_MIN = 0.02
DEFAULT_SEVERE_SEVERITY = 3.0


def repo_root() -> Path:
    """仓库根。`src/sparkjury/pack.py` -> `parents[2]`。"""
    return Path(__file__).resolve().parents[2]


def pack_dir(pack: str | Path | None = None) -> Path:
    if pack is not None:
        return Path(pack)
    env = os.environ.get(PACK_ENV)
    return Path(env) if env else repo_root() / "standards" / "scenario-pack"


def label_map_path(path: str | Path | None = None) -> Path:
    """运行时失败标签 -> pack 类目的映射文件（在 pack 之外，见文件头的说明）。"""
    if path is not None:
        return Path(path)
    env = os.environ.get(LABEL_MAP_ENV)
    return Path(env) if env else repo_root() / "standards" / "label-taxonomy-map.yaml"


def _load_yaml_file(path: Path) -> dict[str, Any]:
    """用仓库里唯一的 YAML 实现（`tools/_yaml_lite.py`）读一个文件。

    仓库不做 YAML 依赖（`pyproject.toml` 里没有 PyYAML），`tools/_yaml_lite.py` 是那份最小实现，
    六个 skill 也是这么用的。这里按路径加载而不是往 `sys.path` 里塞 `tools/`：runtime 不该因为多了
    一个 import 就把仓库的脚本目录变成可导入包。
    """
    if not path.is_file():
        return {}
    spec = importlib.util.spec_from_file_location("sparkjury._pack_yaml_lite", repo_root() / "tools" / "_yaml_lite.py")
    if spec is None or spec.loader is None:  # pragma: no cover - 打包后不存在
        return {}
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    loaded = mod.load_yaml_file(str(path))
    return loaded if isinstance(loaded, dict) else {}


@lru_cache(maxsize=8)
def _read_json_cached(path_str: str) -> dict[str, Any]:
    p = Path(path_str)
    if not p.is_file():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


@lru_cache(maxsize=8)
def _read_yaml_cached(path_str: str) -> dict[str, Any]:
    return _load_yaml_file(Path(path_str))


def manifest(pack: str | Path | None = None) -> dict[str, Any]:
    """pack.manifest.json（读不到给 `{}`）。"""
    return dict(_read_json_cached(str(pack_dir(pack) / "pack.manifest.json")))


def frozen_hash(pack: str | Path | None = None) -> str | None:
    """pack 声明的 frozen_hash。DRAFT 态或没有 pack 时是 None。"""
    h = manifest(pack).get("frozen_hash")
    return str(h) if h else None


def pack_id(pack: str | Path | None = None) -> str | None:
    v = manifest(pack).get("pack_id")
    return str(v) if v else None


def state(pack: str | Path | None = None) -> str | None:
    v = manifest(pack).get("state")
    return str(v) if v else None


def thresholds(pack: str | Path | None = None) -> dict[str, Any]:
    """thresholds.yaml 的内容（读不到给 `{}`）。"""
    return dict(_read_yaml_cached(str(pack_dir(pack) / "thresholds.yaml")))


def regress_policy(pack: str | Path | None = None) -> dict[str, Any]:
    """`thresholds.yaml` 的 `regress:` 段，缺的键用 DEFAULT_* 顶上。"""
    raw = thresholds(pack).get("regress")
    pol: dict[str, Any] = dict(raw) if isinstance(raw, dict) else {}
    gates = pol.get("gates")
    if not isinstance(gates, dict):
        gates = {}
    pol["gates"] = {
        "primary_metric": gates.get("primary_metric", "task_pass_rate"),
        "new_severe_cluster_blocks": bool(gates.get("new_severe_cluster_blocks", True)),
        "delta_min": gates.get("delta_min"),
    }
    pol["same_pack_hash_required"] = bool(pol.get("same_pack_hash_required", True))
    return pol


def pack_label_map(pack: str | Path | None = None) -> dict[str, str]:
    """pack 自带的 `runtime_label` 映射（pack v0.2 起）：`taxonomy.yaml` 的
    `categories[].runtime_label` -> `id`。

    v0.2 的 taxonomy 头里写得很清楚：「消费方凭 runtime_label 自动翻译，不再需要手工映射表」。
    所以有它就用它——pack 是标准的真源，运行时不该再维护一份自己的翻译表。
    """
    data = _read_yaml_cached(str(pack_dir(pack) / "taxonomy.yaml"))
    cats = data.get("categories")
    if not isinstance(cats, list):
        return {}
    out: dict[str, str] = {}
    for c in cats:
        if isinstance(c, dict) and c.get("runtime_label") and c.get("id"):
            out[str(c["runtime_label"])] = str(c["id"])
    return out


def label_category_map(path: str | Path | None = None) -> dict[str, str]:
    """兜底映射：失败标签 -> 类目 id（F\\d\\d），来自 `standards/label-taxonomy-map.yaml`。

    pack v0.2 起 taxonomy 自带 `runtime_label`，那条路优先级更高（见 `category_for_label`）；
    这张表是 pack 还没有该字段时的兜底（也是 `other` 这类 pack 没给 runtime_label 的标签的去处）。
    它落在 pack 目录之外，因为 pack 已 FROZEN：往里加一个键就会改 frozen_hash。
    """
    data = dict(_read_yaml_cached(str(label_map_path(path))))
    mapping = data.get("map")
    if not isinstance(mapping, dict):
        return {}
    return {str(k): str(v) for k, v in mapping.items()}


def category_for_label(label: str | None, path: str | Path | None = None, pack: str | Path | None = None) -> str | None:
    """一个失败标签对应的 pack 类目；没映射给 None（调用方负责说清「会判 unmatched」）。

    顺序：pack 的 `runtime_label`（v0.2 起是权威）→ `standards/label-taxonomy-map.yaml`（pack 还没有
    这个字段时的兜底，也是 `other` 这类没有 runtime_label 的标签的去处）。
    """
    if not label:
        return None
    lab = str(label)
    return pack_label_map(pack).get(lab) or label_category_map(path).get(lab)


def clear_cache() -> None:
    """清掉 pack/mapping 的读取缓存（测试换 pack 目录时用）。"""
    _read_json_cached.cache_clear()
    _read_yaml_cached.cache_clear()
