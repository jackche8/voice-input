"""组件进程记录 run/children.json：每次拉起/退出后原子重写；应用启动时据此清理上次运行的残留会话。

格式：{"app_pid", "app_identity", "children": {name: {"pid", "sid", "leader_starttime"}}}。
"""
from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, field
from pathlib import Path

from _atomic_file import atomic_write_bytes
from _platform.child_process import SessionRecord, kill_recorded_session, process_identity


@dataclass(frozen=True)
class ChildrenRecord:
    app_pid: int
    app_identity: str | None
    children: Mapping[str, SessionRecord] = field(default_factory=dict)


def write_record(path: Path, record: ChildrenRecord) -> None:
    payload = {
        "app_pid": record.app_pid,
        "app_identity": record.app_identity,
        "children": {name: asdict(child) for name, child in record.children.items()},
    }
    atomic_write_bytes(path, json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8"), 0o600)


def read_record(path: Path) -> ChildrenRecord | None:
    """没有记录 → None；内容损坏抛 ValueError（带文件路径），不当成「没有残留」。"""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        raise ValueError(f"无法读取组件进程记录 {path}：{exc}") from exc
    try:
        children = {name: SessionRecord(**child) for name, child in raw["children"].items()}
        return ChildrenRecord(app_pid=int(raw["app_pid"]), app_identity=raw["app_identity"], children=children)
    except (KeyError, TypeError, AttributeError) as exc:
        raise ValueError(f"组件进程记录 {path} 格式不对：{exc!r}") from exc


def clean_stale_children(
    path: Path,
    *,
    identity: Callable[[int], str | None] = process_identity,
    kill: Callable[[SessionRecord], list[int]] = kill_recorded_session,
) -> list[int]:
    """应用启动（已持有单例锁）后调用：逐条清理上次运行的组件会话，删除记录，返回收到信号的 PID。

    记录里的应用进程若仍在且身份一致，说明另一个实例还在管理这些组件——单例锁本应排除这种情况，直接报错。
    """
    record = read_record(path)
    if record is None:
        return []
    app_now = identity(record.app_pid)
    if app_now is not None and app_now == record.app_identity:
        raise RuntimeError(f"组件进程记录 {path} 的应用实例 PID {record.app_pid} 仍在运行，拒绝清理它的组件")
    killed = [pid for child in record.children.values() for pid in kill(child)]
    path.unlink()
    return killed


__all__ = ["ChildrenRecord", "clean_stale_children", "read_record", "write_record"]
