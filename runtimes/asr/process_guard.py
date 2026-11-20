"""只回收本 sidecar 进程派生的孩子，并校验 /proc/{pid}/stat starttime 防止误杀复用 PID。"""
from __future__ import annotations

import atexit
import os
import signal
import time
from collections.abc import Callable
from pathlib import Path

from _procfs import (
    StatRow,
    descendants_of,
    is_alive,
    iter_process_table_with_starttime,
    proc_starttime,
    unpack_row,
)

_PROC = Path("/proc")


class ChildProcessGuard:
    """登记并回收本进程的子孙，记录每个登记 PID 的 starttime 以杜绝误杀复用 PID。"""

    def __init__(
        self,
        *,
        owner_pid: int | None = None,
        term_timeout_s: float = 5.0,
        kill_timeout_s: float = 2.0,
        proc_root: Path = _PROC,
        starttime_reader: Callable[[int], int | None] | None = None,
    ) -> None:
        self.owner_pid = int(owner_pid or os.getpid())
        self.term_timeout_s = term_timeout_s
        self.kill_timeout_s = kill_timeout_s
        self._proc_root = proc_root
        self._starttime_reader = starttime_reader or (
            lambda pid: proc_starttime(pid, proc_root=self._proc_root)
        )
        self.owner_starttime = self._starttime_reader(self.owner_pid)
        self.invoker_pgid = (
            os.getpgid(self.owner_pid) if hasattr(os, "getpgid") else self.owner_pid
        )
        self.session_pgid: int | None = None
        self.entered_new_session = False
        self.owned: set[int] = set()
        self._owned_starttimes: dict[int, int | None] = {}
        self._reaped = False
        self._installed = False

    def enter_new_session(self) -> int:
        if not hasattr(os, "setsid") or not hasattr(os, "getpgrp"):
            self.entered_new_session = False
            self.session_pgid = self.owner_pid
            return self.owner_pid
        try:
            os.setsid()
            self.entered_new_session = True
        except OSError:
            self.entered_new_session = os.getpgrp() == self.owner_pid
        self.session_pgid = os.getpgrp()
        return self.session_pgid

    def _row_starttime(self, pid: int, row: StatRow | None) -> int | None:
        if row is not None:
            st = unpack_row(row)[3]
            if st is not None:
                return st
        return self._starttime_reader(pid)

    def _verify_pid_identity(self, pid: int, current_st: int | None) -> bool:
        """校验目标 PID 是否仍是最初登记的同一进程实例。"""
        if pid not in self._owned_starttimes:
            return True
        recorded_st = self._owned_starttimes[pid]
        if recorded_st is None:
            return True
        return current_st is not None and current_st == recorded_st

    def _evict_recycled(self, pid: int) -> None:
        self.owned.discard(pid)
        self._owned_starttimes.pop(pid, None)

    def remember(self, table: dict[int, StatRow] | None = None) -> set[int]:
        resolved: dict[int, StatRow] = (
            iter_process_table_with_starttime(proc_root=self._proc_root)
            if table is None
            else table
        )
        # 若 owner 自身在测试模拟表里被替换，先校验 owner_starttime
        if self.owner_pid in resolved and self.owner_starttime is not None:
            owner_cur_st = self._row_starttime(self.owner_pid, resolved[self.owner_pid])
            if owner_cur_st is not None and owner_cur_st != self.owner_starttime:
                live_children: set[int] = set()
            else:
                live_children = descendants_of(self.owner_pid, resolved)
        else:
            live_children = descendants_of(self.owner_pid, resolved)

        if self.entered_new_session and self.session_pgid is not None:
            for pid, row in resolved.items():
                _ppid, pgrp, _sid, _st = unpack_row(row)
                if pid != self.owner_pid and pgrp == self.session_pgid:
                    live_children.add(pid)

        # 检查已登记 PID 是否已退出并被其他无关进程复用
        for pid in list(self.owned):
            if pid not in resolved:
                continue
            cur_st = self._row_starttime(pid, resolved[pid])
            if not self._verify_pid_identity(pid, cur_st):
                if pid not in live_children:
                    self._evict_recycled(pid)
                    continue
                # 极端情况：本进程新派生的直接子孙恰好复用了旧子进程 PID，更新为其新 starttime
                self._owned_starttimes[pid] = cur_st

        for pid in live_children:
            if pid == self.owner_pid:
                continue
            st = self._row_starttime(pid, resolved.get(pid))
            if pid not in self._owned_starttimes:
                self._owned_starttimes[pid] = st
            self.owned.add(pid)
        return set(self.owned)

    def reclaimable(self, table: dict[int, StatRow] | None = None) -> set[int]:
        resolved: dict[int, StatRow] = (
            iter_process_table_with_starttime(proc_root=self._proc_root)
            if table is None
            else table
        )
        self.remember(resolved)
        targets: set[int] = set()
        for pid in list(self.owned):
            if pid == self.owner_pid or pid not in resolved:
                continue
            _ppid, pgrp, _sid, row_st = unpack_row(resolved[pid])
            cur_st = row_st if row_st is not None else self._starttime_reader(pid)
            if not self._verify_pid_identity(pid, cur_st):
                self._evict_recycled(pid)
                continue
            if (
                self.entered_new_session
                and pgrp == self.invoker_pgid
                and pgrp != self.session_pgid
            ):
                continue
            targets.add(pid)
        return targets

    def _same_process_alive(self, pid: int) -> bool:
        expected_st = self._owned_starttimes.get(pid)
        return is_alive(pid, expected_starttime=expected_st, proc_root=self._proc_root)

    def _signal_verified(self, pid: int, sig: int) -> bool:
        cur_st = self._starttime_reader(pid)
        if not self._verify_pid_identity(pid, cur_st):
            self._evict_recycled(pid)
            return False
        try:
            os.kill(pid, sig)
            return True
        except (ProcessLookupError, PermissionError, OSError):
            return False

    def reap(self) -> dict[str, list[int] | bool]:
        if self._reaped:
            return {"already": True, "term": [], "kill": [], "survived": []}
        self._reaped = True
        sent_term: list[int] = []
        sent_kill: list[int] = []
        for _round in range(3):
            targets = self.reclaimable() - set(sent_term) - set(sent_kill)
            if not targets:
                break
            for pid in sorted(targets):
                if self._signal_verified(pid, signal.SIGTERM):
                    sent_term.append(pid)
            deadline = time.monotonic() + self.term_timeout_s
            while time.monotonic() < deadline and any(
                self._same_process_alive(pid) for pid in sent_term
            ):
                time.sleep(0.05)
            still = [pid for pid in sent_term if self._same_process_alive(pid)]
            sigkill = getattr(signal, "SIGKILL", signal.SIGTERM)
            for pid in still:
                if pid in sent_kill:
                    continue
                if self._signal_verified(pid, sigkill):
                    sent_kill.append(pid)
            deadline = time.monotonic() + self.kill_timeout_s
            while time.monotonic() < deadline and any(
                self._same_process_alive(pid) for pid in sent_kill
            ):
                time.sleep(0.05)
        survived = [pid for pid in sent_term if self._same_process_alive(pid)]
        return {"already": False, "term": sent_term, "kill": sent_kill, "survived": survived}

    def install(self, *, request_stop=None) -> None:
        if self._installed:
            return
        self._installed = True
        atexit.register(self.reap)

        def _handle(signum, _frame) -> None:
            if request_stop is not None:
                request_stop(signum)
                return
            self.remember()
            self.reap()
            os._exit(128 + int(signum))

        signal.signal(signal.SIGTERM, _handle)
        signal.signal(signal.SIGINT, _handle)


__all__ = ["ChildProcessGuard"]
