"""Linux /proc 只读解析：进程表、父子关系、会话成员与 starttime 身份（只用标准库，sidecar 与应用共用）。"""
from __future__ import annotations

import os
from pathlib import Path

_PROC = Path("/proc")
# man 5 proc_pid_stat: 第 22 项 starttime 位于 comm ')' 之后切分数组的索引 19（第 3 项 state 为索引 0）
_STAT_STARTTIME_INDEX = 19

StatRow = tuple[int, int, int] | tuple[int, int, int, int]


def _read_after_comm(pid: int, proc_root: Path = _PROC) -> list[str] | None:
    try:
        text = (proc_root / str(pid) / "stat").read_text()
    except (FileNotFoundError, ProcessLookupError, PermissionError, OSError):
        return None
    rparen = text.rfind(")")
    if rparen < 0:
        return None
    fields = text[rparen + 2 :].split()
    return fields if fields else None


def parse_stat_with_starttime(
    pid: int, *, proc_root: Path = _PROC
) -> tuple[int, int, int, int] | None:
    """返回 (ppid, pgrp, session, starttime)；进程不存在或字段不全时返回 None。"""
    fields = _read_after_comm(pid, proc_root)
    if fields is None or len(fields) <= _STAT_STARTTIME_INDEX:
        return None
    try:
        return (
            int(fields[1]),
            int(fields[2]),
            int(fields[3]),
            int(fields[_STAT_STARTTIME_INDEX]),
        )
    except ValueError:
        return None


def parse_stat(pid: int, *, proc_root: Path = _PROC) -> tuple[int, int, int] | None:
    """返回 (ppid, pgrp, session)；进程不存在时为 None。"""
    fields = _read_after_comm(pid, proc_root)
    if fields is None or len(fields) < 4:
        return None
    try:
        return int(fields[1]), int(fields[2]), int(fields[3])
    except ValueError:
        return None


def proc_starttime(pid: int, *, proc_root: Path = _PROC) -> int | None:
    """读取 /proc/<pid>/stat 的第 22 字段 starttime（时钟滴答），用于识别 PID 复用。"""
    parsed = parse_stat_with_starttime(pid, proc_root=proc_root)
    return parsed[3] if parsed is not None else None


def iter_process_table(*, proc_root: Path = _PROC) -> dict[int, tuple[int, int, int]]:
    table: dict[int, tuple[int, int, int]] = {}
    try:
        entries = proc_root.iterdir()
    except OSError:
        return table
    for entry in entries:
        name = entry.name
        if not name.isdigit():
            continue
        pid = int(name)
        parsed = parse_stat(pid, proc_root=proc_root)
        if parsed is not None:
            table[pid] = parsed
    return table


def iter_process_table_with_starttime(
    *, proc_root: Path = _PROC
) -> dict[int, tuple[int, int, int, int]]:
    table: dict[int, tuple[int, int, int, int]] = {}
    try:
        entries = proc_root.iterdir()
    except OSError:
        return table
    for entry in entries:
        name = entry.name
        if not name.isdigit():
            continue
        pid = int(name)
        parsed = parse_stat_with_starttime(pid, proc_root=proc_root)
        if parsed is not None:
            table[pid] = parsed
    return table


def unpack_row(row: StatRow) -> tuple[int, int, int, int | None]:
    if len(row) >= 4:
        return int(row[0]), int(row[1]), int(row[2]), int(row[3])  # type: ignore[index]
    return int(row[0]), int(row[1]), int(row[2]), None


def descendants_of(root: int, table: dict[int, StatRow] | None = None) -> set[int]:
    resolved_table: dict[int, StatRow] = (
        iter_process_table_with_starttime() if table is None else table
    )
    children: dict[int, list[int]] = {}
    for pid, row in resolved_table.items():
        ppid = unpack_row(row)[0]
        children.setdefault(ppid, []).append(pid)
    found: set[int] = set()
    stack = list(children.get(root, ()))
    while stack:
        cur = stack.pop()
        if cur in found or cur == root:
            continue
        found.add(cur)
        stack.extend(children.get(cur, ()))
    return found


def proc_state(pid: int, *, proc_root: Path = _PROC) -> str | None:
    """/proc/<pid>/stat 的状态字母；进程不存在时为 None。"""
    fields = _read_after_comm(pid, proc_root)
    return fields[0] if fields else None


def is_alive(
    pid: int,
    *,
    expected_starttime: int | None = None,
    proc_root: Path = _PROC,
) -> bool:
    """僵尸或已被内核复用的不同 starttime 进程不算原进程存活。"""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass
    except OSError:
        return False
    if not proc_root.exists():
        return True
    state = proc_state(pid, proc_root=proc_root)
    if state in (None, "Z", "X"):
        return False
    if expected_starttime is not None:
        current_st = proc_starttime(pid, proc_root=proc_root)
        if current_st is None or current_st != expected_starttime:
            return False
    return True


def session_members(sid: int, *, proc_root: Path = _PROC) -> list[int]:
    """会话号为 sid 的全部存活进程（不含僵尸），按 PID 升序。会话号即会话组长 PID。"""
    return sorted(
        pid
        for pid, (_ppid, _pgrp, session) in iter_process_table(proc_root=proc_root).items()
        if session == sid and proc_state(pid, proc_root=proc_root) not in (None, "Z", "X")
    )


__all__ = [
    "StatRow",
    "descendants_of",
    "is_alive",
    "iter_process_table",
    "iter_process_table_with_starttime",
    "parse_stat",
    "parse_stat_with_starttime",
    "proc_starttime",
    "proc_state",
    "session_members",
    "unpack_row",
]
