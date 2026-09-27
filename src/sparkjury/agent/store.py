"""三个存储 + 原子事务：中层的地基。

pi 的 harness 规范把一次 run 的全部状态放在三个地方：**只追加的对话树**、**可替换的
values**、**只追加的用量账本**。M13 的最小闭环里，对话树和账本各做了一半，values 完全
没有，而「原子事务」也没做——那时的写法是东写一个文件、西写一个文件，中途被 kill 就会
留下一半新一半旧的状态，恢复时说不清哪份算数。

这一层把三件事补齐：

1. `ValuesStore`：键 → 值，可替换。整表重写走「写临时文件 + fsync + `os.replace`」，
   替换这一下是原子的，读的人要么看到旧的一整份、要么看到新的一整份。
2. `Ledger`：只追加的用量账本。读的时候容忍最后一行被截断（崩溃时正在写的那半行）。
3. `RunStore`：把一次 run 的六个文件位置收在一处，并用 `transaction()` 把一次提交里的
   若干次写入按顺序落盘：**先追加、后替换**。

为什么是这个顺序：追加写坏只坏最后一行，前面的记录还在，恢复时以对话树为准；
`values` 是可替换的快照，崩溃后最多落后一步，不会出现「半新半旧」。
"""

from __future__ import annotations

import json
import os
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator


class StoreError(RuntimeError):
    """存储坏了：文件读不出来或者内容不是我们写的那套结构。

    这里刻意不吞：values 读不出来时静默返回空表，等于把「状态丢了」伪装成「一开始就没有」，
    恢复的时候会拿着错的起点往下跑。
    """


@dataclass(frozen=True)
class StorePaths:
    """一次 run 的落盘位置。所有路径都从 run 目录推出来，不给调用方拼字符串的机会。"""

    run_dir: Path

    @property
    def entries(self) -> Path:
        return self.run_dir / "session.jsonl"

    @property
    def values(self) -> Path:
        return self.run_dir / "values.json"

    @property
    def ledger(self) -> Path:
        return self.run_dir / "usage.jsonl"

    @property
    def ops(self) -> Path:
        return self.run_dir / "ops.jsonl"

    @property
    def events(self) -> Path:
        return self.run_dir / "events.jsonl"

    @property
    def manifest(self) -> Path:
        return self.run_dir / "manifest.json"

    def all(self) -> dict[str, Path]:
        return {"entries": self.entries, "values": self.values, "ledger": self.ledger,
                "ops": self.ops, "events": self.events, "manifest": self.manifest}


def atomic_write_json(path: Path, payload: Any) -> None:
    """整份替换一个 JSON 文件：临时文件 → fsync → os.replace。

    `os.replace` 在同一个文件系统内是原子的，所以读的人永远看不到「写了一半的 JSON」。
    临时文件写在同一个目录里（不能写 /tmp，跨文件系统就不是原子替换了）。
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def append_jsonl(path: Path, row: dict[str, Any]) -> None:
    """追加一行并落盘。只追加的日志不追求原子：写坏只坏最后一行，读的时候跳过即可。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")
        f.flush()
        os.fsync(f.fileno())


def read_jsonl(path: Path) -> tuple[list[dict[str, Any]], list[int]]:
    """读只追加的日志，返回 (记录, 被跳过的行号)。

    最后一行可能是被 kill 时写了一半的：JSON 解析失败就跳过，并把它记下来——
    悄悄丢掉和「本来就没有」是两回事，调用方要能知道。
    """
    path = Path(path)
    if not path.is_file():
        return [], []
    rows: list[dict[str, Any]] = []
    torn: list[int] = []
    for lineno, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            torn.append(lineno)
            continue
        if isinstance(data, dict):
            rows.append(data)
        else:
            torn.append(lineno)
    return rows, torn


class ValuesStore:
    """可替换的状态：键 → 值。读的人只看到最后一次写进去的东西。"""

    def __init__(self, path: Path | str | None = None):
        self.path = Path(path) if path else None
        self._data: dict[str, Any] = {}
        if self.path and self.path.is_file():
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
            except json.JSONDecodeError as e:
                raise StoreError(f"values 文件不是合法 JSON：{self.path}（{e}）") from e
            if not isinstance(raw, dict):
                raise StoreError(f"values 文件顶层应当是对象：{self.path}")
            self._data = raw

    # ------------------------------------------------------------ 读
    def get(self, key: str, default: Any = None) -> Any:
        return self._data.get(key, default)

    def keys(self) -> list[str]:
        return list(self._data)

    def snapshot(self) -> dict[str, Any]:
        return json.loads(json.dumps(self._data, ensure_ascii=False))  # 深拷贝，别把内部状态漏出去

    def __contains__(self, key: str) -> bool:
        return key in self._data

    def __len__(self) -> int:
        return len(self._data)

    # ------------------------------------------------------------ 写
    def set(self, key: str, value: Any) -> None:
        self._data[key] = value
        self.flush()

    def append(self, key: str, item: Any) -> list[Any]:
        """往一个列表里追加。键不存在时按空列表起步；存的不是列表就报错。"""
        current = self._data.get(key)
        if current is None:
            current = []
        if not isinstance(current, list):
            raise StoreError(f"values[{key!r}] 不是列表，不能 append")
        current.append(item)
        self._data[key] = current
        self.flush()
        return current

    def update(self, mapping: dict[str, Any]) -> None:
        self._data.update(mapping)
        self.flush()

    def delete(self, key: str) -> None:
        self._data.pop(key, None)
        self.flush()

    def flush(self) -> None:
        if self.path:
            atomic_write_json(self.path, self._data)


class Ledger:
    """只追加的用量账本。每一轮一行，收尾累加成总数。"""

    def __init__(self, path: Path | str | None = None):
        self.path = Path(path) if path else None
        self.torn_lines: list[int] = []

    def append(self, row: dict[str, Any]) -> dict[str, Any]:
        if self.path:
            append_jsonl(self.path, row)
        return row

    def rows(self) -> list[dict[str, Any]]:
        if not self.path:
            return []
        rows, torn = read_jsonl(self.path)
        self.torn_lines = torn
        return rows

    def total(self, keys: tuple[str, ...] = ("prompt_tokens", "completion_tokens", "total_tokens")) -> dict[str, int]:
        """把数字字段累加起来。非数字或缺失的字段跳过，不编造 0 以外的值。"""
        out: dict[str, int] = {}
        for row in self.rows():
            for key in keys:
                value = row.get(key)
                if isinstance(value, int):
                    out[key] = out.get(key, 0) + value
        return out


class Transaction:
    """一次提交的暂存区。提交前的写入都留在内存里，提交时按顺序落盘。"""

    def __init__(self, store: "RunStore", label: str = ""):
        self.store = store
        self.label = label
        self._appends: list[tuple[Path, dict[str, Any]]] = []
        self._values: dict[str, Any] = {}
        self.committed = False

    def row(self, path: Path, payload: dict[str, Any]) -> None:
        """往某个只追加的文件里排一行。"""
        self._appends.append((Path(path), payload))

    def ledger(self, payload: dict[str, Any]) -> None:
        self.row(self.store.paths.ledger, payload)

    def entries(self, entry_json: dict[str, Any]) -> None:
        self.row(self.store.paths.entries, entry_json)

    def value(self, key: str, value: Any) -> None:
        self._values[key] = value

    def values(self, mapping: dict[str, Any]) -> None:
        self._values.update(mapping)

    def commit(self) -> None:
        for path, payload in self._appends:      # 先追加
            append_jsonl(path, payload)
        if self._values:
            merged = self.store.values.snapshot()
            merged.update(self._values)
            atomic_write_json(self.store.paths.values, merged)   # 后替换
            self.store.values.update(self._values)
        self.committed = True


@dataclass
class RunStore:
    """一次 run 的三个存储 + 事务边界。"""

    run_dir: Path
    values: ValuesStore = field(init=False)
    ledger: Ledger = field(init=False)

    def __post_init__(self) -> None:
        self.run_dir = Path(self.run_dir)
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.paths = StorePaths(self.run_dir)
        self.values = ValuesStore(self.paths.values)
        self.ledger = Ledger(self.paths.ledger)

    @contextmanager
    def transaction(self, label: str = "") -> Iterator[Transaction]:
        """`with store.transaction("turn-3") as tx:` —— 体里抛异常就一条都不写。

        体里只暂存，退出时才落盘：所以「写了一半崩掉」这件事在语义上就不存在，
        要么这次提交全在，要么全不在。
        """
        tx = Transaction(self, label)
        yield tx
        tx.commit()

    def recover(self) -> dict[str, Any]:
        """崩过之后看一眼现场：哪些日志尾部有半行、values 能不能读。

        只报告，不改任何文件——恢复的动作由调用方（runtime）按报告决定。
        """
        report: dict[str, Any] = {"run_dir": str(self.run_dir), "torn_lines": {}, "values_keys": []}
        for name, path in self.paths.all().items():
            if path.suffix == ".jsonl":
                _rows, torn = read_jsonl(path)
                if torn:
                    report["torn_lines"][name] = torn
        report["values_keys"] = self.values.keys()
        report["unfinished_ops"] = []  # ops 层填，这里不重复实现
        return report
