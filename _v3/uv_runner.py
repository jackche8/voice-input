"""单源流式 `uv` 子进程运行器（逻辑层）：实时脱敏日志 tee、在途包解析、磁盘活跃度与单调字节/滑窗速率、取消与停滞熔断。

职责边界：
- 仅负责执行单次 `uv` 子命令并上报 `SetupProgress`，不决定变体、锁文件或运行环境清单；
- 停滞活动目录与 UI `completed_bytes` 分离：活动签名检测任一目录字节或文件数变化（含 `.tmp` 收缩），
  UI 字节仅取单一主目录单调 `max_seen`，绝不累加 `asr_venv`（防 NTFS 硬链接双计，且绝不依赖 Windows 下恒为 0 的 `st_ino`）。
"""
from __future__ import annotations

import os
import re
import signal
import stat
import subprocess
import sys
import threading
import time
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

from _contracts import ErrorCode, LogicError, SetupProgress
from _logs import strip_ansi
from _platform.child_process import windows_subprocess_kwargs
from _platform.protocol import OsName
from _platform.system_proxy import format_proxy_hint, redact_url_credentials
from _v3.setup_tasks import ProgressFn

STALL_TIMEOUT_S = 300.0
INSTALL_TIMEOUT_S = 3600.0
POLL_INTERVAL_S = 0.2
SAMPLE_INTERVAL_S = 2.0
RATE_WINDOW_S = 8.0
MIN_DISPLAY_RATE_MIB_S = 0.1
TAIL_BUFFER_LINES = 80
ERROR_SUMMARY_CHARS = 380

_DOWNLOADING_RE = re.compile(
    r"^Downloading\s+(\S+)(?:\s+\([^)]*\))*?\s+\((\d+(?:\.\d+)?)\s*([KMGTP]?i?B)\)\s*$",
    re.IGNORECASE,
)
_DOWNLOADED_SUMMARY_RE = re.compile(r"^Downloaded\s+(\d+)\s+packages?\b")
_DOWNLOADED_PKG_RE = re.compile(r"^Downloaded\s+(\S+)(?:\s+\([^)]*\))*\s*$")
_RESOLVED_RE = re.compile(r"^Resolved\s+(\d+)\s+packages?\b")
_PREPARED_RE = re.compile(r"^Prepared\s+(\d+)\s+packages?\b")
_INSTALLED_RE = re.compile(r"^Installed\s+(\d+)\s+packages?\b")

_SIZE_MULTIPLIERS = {
    "b": 1,
    "kb": 1_000,
    "kib": 1024,
    "mb": 1_000_000,
    "mib": 1024**2,
    "gb": 1_000_000_000,
    "gib": 1024**3,
    "tb": 1_000_000_000_000,
    "tib": 1024**4,
}


def _parse_size_bytes(num_text: str, unit_text: str) -> int:
    mult = _SIZE_MULTIPLIERS.get(unit_text.lower(), 1)
    try:
        return max(0, int(float(num_text) * mult))
    except ValueError:
        return 0


def _scan_path(path: Path) -> tuple[int, int]:
    """迭代 `os.scandir` 统计路径字节数与条目数（兼容单文件与符号链接、全 OSError 容错、绝不用 st_ino）。"""
    try:
        st = os.lstat(path)
    except OSError:
        return 0, 0
    if not stat.S_ISDIR(st.st_mode):
        return st.st_size, 1
    total_bytes = 0
    file_count = 0
    stack = [os.fspath(path)]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as it:
                for entry in it:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(entry.path)
                        else:
                            total_bytes += entry.stat(follow_symlinks=False).st_size
                            file_count += 1
                    except OSError:
                        continue
        except OSError:
            continue
    return total_bytes, file_count


def dir_bytes(path: Path) -> int:
    """迭代统计目录或单文件字节总和（不跟随符号链接，并发写入消失文件安全跳过）。"""
    return _scan_path(path)[0]


class _RateWindow:
    """有界滑动窗口速率计算器：以初始基线 `(t0, initial_bytes)` 锚定，防重试已有缓存产生假速率。"""

    def __init__(self, t0: float, initial_bytes: int, *, window_s: float = RATE_WINDOW_S) -> None:
        self._window_s = max(0.5, float(window_s))
        self._samples: deque[tuple[float, int]] = deque([(t0, max(0, initial_bytes))])

    def sample(self, now: float, total_bytes: int) -> float | None:
        cur = max(0, total_bytes)
        self._samples.append((now, cur))
        cutoff = now - self._window_s
        while len(self._samples) > 2 and self._samples[1][0] <= cutoff:
            self._samples.popleft()
        anchor_t, anchor_b = self._samples[0]
        dt = now - anchor_t
        if dt < 1.0:
            return None
        delta = max(0, cur - anchor_b)
        return delta / dt


def _truncate_summary(text: str, max_chars: int = ERROR_SUMMARY_CHARS) -> str:
    """截断超长错因时保留首段（`error:`/`×` 锚点）与末段根因，避免丢掉首行定位。"""
    if len(text) <= max_chars:
        return text
    if max_chars <= 10:
        return text[:max_chars]
    sep = " … "
    head = (max_chars - len(sep)) // 2
    tail = max_chars - len(sep) - head
    return f"{text[:head].rstrip()}{sep}{text[-tail:].lstrip()}"


class UvStreamTracker:
    """线程安全的 `uv` 文本输出解析器：脱敏行缓冲、在途大包表与里程碑提取。"""

    def __init__(self, what: str) -> None:
        self._what = what.strip()
        self._lock = threading.Lock()
        self._tail: deque[str] = deque(maxlen=TAIL_BUFFER_LINES)
        self._in_flight: dict[str, tuple[int, str, int]] = {}
        self._seq = 0
        self._milestone: str | None = None
        self._line_seq = 0

    def feed_line(self, raw: str) -> str:
        """清洗单行输出（去 ANSI、凭证脱敏）并更新内部状态，返回可直接 tee 到日志的脱敏行。"""
        cleaned = redact_url_credentials(strip_ansi(raw)).rstrip("\r\n")
        visible = cleaned.rsplit("\r", 1)[-1].strip()
        with self._lock:
            self._line_seq += 1
            if visible:
                self._tail.append(visible)
                self._parse_locked(visible)
        return cleaned

    def _parse_locked(self, line: str) -> None:
        m_dl = _DOWNLOADING_RE.match(line)
        if m_dl is not None:
            pkg, num, unit = m_dl.group(1), m_dl.group(2), m_dl.group(3)
            size_bytes = _parse_size_bytes(num, unit)
            size_label = f"{num}{unit}"
            prev = self._in_flight.get(pkg)
            order = prev[2] if prev is not None else self._seq
            if prev is None:
                self._seq += 1
            self._in_flight[pkg] = (size_bytes, size_label, order)
            return

        m_done_sum = _DOWNLOADED_SUMMARY_RE.match(line)
        if m_done_sum is not None:
            self._in_flight.clear()
            self._milestone = f"已下载 {m_done_sum.group(1)} 个包"
            return

        m_done_pkg = _DOWNLOADED_PKG_RE.match(line)
        if m_done_pkg is not None:
            self._in_flight.pop(m_done_pkg.group(1), None)
            return

        m_res = _RESOLVED_RE.match(line)
        if m_res is not None:
            self._milestone = f"已解析 {m_res.group(1)} 个包"
            return

        m_prep = _PREPARED_RE.match(line)
        if m_prep is not None:
            self._in_flight.clear()
            self._milestone = f"已准备 {m_prep.group(1)} 个包"
            return

        m_inst = _INSTALLED_RE.match(line)
        if m_inst is not None:
            self._in_flight.clear()
            self._milestone = f"已安装 {m_inst.group(1)} 个包"

    def line_seq(self) -> int:
        with self._lock:
            return self._line_seq

    def format_stage(self, rate_bytes_s: float | None = None) -> str:
        with self._lock:
            parts = [self._what]
            if self._in_flight:
                best_pkg, (_sz, best_label, _ord) = max(
                    self._in_flight.items(),
                    key=lambda item: (item[1][0], -item[1][2]),
                )
                parts.append(f"下载 {best_pkg} ({best_label})")
            elif self._milestone:
                parts.append(self._milestone)
        if rate_bytes_s is not None:
            rate_mib = rate_bytes_s / (1024 * 1024)
            if rate_mib >= MIN_DISPLAY_RATE_MIB_S:
                parts.append(f"{rate_mib:.1f} MB/s")
        return " · ".join(parts)

    def error_summary(self, max_chars: int = ERROR_SUMMARY_CHARS) -> str:
        with self._lock:
            lines = list(self._tail)
        if not lines:
            return ""
        start_idx = 0
        found_top = False
        for idx, line in enumerate(lines):
            low = line.lower()
            if low.startswith(("error:", "×")):
                start_idx = idx
                found_top = True
            elif not found_top and low.startswith("caused by:"):
                start_idx = idx
        snippet = " | ".join(lines[start_idx:]).strip()
        if not snippet:
            snippet = " | ".join(lines).strip()
        return _truncate_summary(snippet, max_chars)


def _tee_stderr(line: str) -> None:
    stream = sys.stderr
    if stream is None:
        return
    try:
        stream.write(line + "\n")
        stream.flush()
    except OSError:
        pass


def terminate_process_tree(
    proc: subprocess.Popen,
    *,
    is_windows: bool,
    grace_s: float = 0.2,
) -> None:
    """跨平台终止子进程及其后代：Windows 先 `terminate_descendants` 再 kill；POSIX 杀独立进程组 SIGTERM→SIGKILL。"""
    if is_windows:
        pid = getattr(proc, "pid", None)
        if isinstance(pid, int) and pid > 0:
            try:
                from _platform._child_windows import terminate_descendants

                terminate_descendants(pid)
            except (OSError, RuntimeError):
                pass
        try:
            proc.kill()
        except OSError:
            pass
        try:
            proc.wait(timeout=5)
        except (subprocess.TimeoutExpired, OSError):
            pass
        return

    pid = getattr(proc, "pid", None)
    sent_pg = False
    if isinstance(pid, int) and pid > 0 and hasattr(os, "killpg"):
        try:
            os.killpg(pid, signal.SIGTERM)
            sent_pg = True
        except OSError:
            sent_pg = False
    if not sent_pg:
        try:
            proc.terminate()
        except OSError:
            pass
    try:
        proc.wait(timeout=max(0.0, grace_s))
        return
    except (subprocess.TimeoutExpired, OSError):
        pass
    if sent_pg and isinstance(pid, int) and pid > 0 and hasattr(os, "killpg"):
        try:
            os.killpg(pid, signal.SIGKILL)
        except OSError:
            pass
    try:
        proc.kill()
    except OSError:
        pass
    try:
        proc.wait(timeout=5)
    except (subprocess.TimeoutExpired, OSError):
        pass


def _format_failure_message(
    headline: str,
    detail: str,
    *,
    env: Mapping[str, str],
    os_name: OsName,
    log_path: Path,
) -> str:
    safe_detail = _truncate_summary(redact_url_credentials(strip_ansi(detail)).strip(), ERROR_SUMMARY_CHARS)
    proxy_hint = format_proxy_hint(env, os_name=os_name)
    suffix = f"当前代理：{proxy_hint}；详见日志 {log_path}" if proxy_hint else f"详见日志 {log_path}"
    if safe_detail:
        return f"{headline}：{safe_detail}（{suffix}）"
    return f"{headline}（{suffix}）"


def run_uv_streaming(
    argv: Sequence[str],
    *,
    env: Mapping[str, str],
    what: str,
    report: ProgressFn,
    os_name: OsName,
    log_path: Path,
    primary_dir: Path | None = None,
    activity_dirs: Sequence[Path] = (),
    popen: Callable[..., subprocess.Popen] = subprocess.Popen,
    cancel_event: threading.Event | None = None,
    monotonic: Callable[[], float] = time.monotonic,
    poll_interval_s: float = POLL_INTERVAL_S,
    sample_interval_s: float = SAMPLE_INTERVAL_S,
    stall_timeout_s: float = STALL_TIMEOUT_S,
    total_timeout_s: float = INSTALL_TIMEOUT_S,
    rate_window_s: float = RATE_WINDOW_S,
    grace_s: float = 0.2,
) -> None:
    """流式执行单条 `uv` 命令：实时日志、进度与速率上报、取消即时中止、停滞与总超时熔断。"""
    label = what.strip()
    if cancel_event is not None and cancel_event.is_set():
        raise LogicError(ErrorCode.dependency_failed, f"{label}已中止")

    proc_kwargs = windows_subprocess_kwargs(encoding="utf-8", errors="replace")
    is_windows_proc = "creationflags" in proc_kwargs
    if not is_windows_proc:
        proc_kwargs["start_new_session"] = True

    try:
        proc = popen(
            list(argv),
            env=dict(env),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            **proc_kwargs,
        )
    except OSError as exc:
        raise LogicError(
            ErrorCode.dependency_failed,
            _format_failure_message(f"{label}启动失败", str(exc), env=env, os_name=os_name, log_path=log_path),
        ) from exc

    start_t = monotonic()
    tracker = UvStreamTracker(label)

    def _drain_stdout() -> None:
        stream = proc.stdout
        if stream is None:
            return
        try:
            for raw_line in stream:
                try:
                    cleaned = tracker.feed_line(raw_line)
                    _tee_stderr(cleaned)
                except Exception:
                    continue
        except (OSError, ValueError):
            return

    reader = threading.Thread(target=_drain_stdout, name="uv-stdout-reader", daemon=True)
    reader.start()

    last_sample_t = start_t
    last_activity = start_t
    prev_line_seq = tracker.line_seq()
    init_by_dir = {d: _scan_path(d) for d in activity_dirs}
    prev_activity_sig = tuple(init_by_dir[d] for d in activity_dirs)
    if primary_dir is None:
        max_seen = 0
    elif primary_dir in init_by_dir:
        max_seen = init_by_dir[primary_dir][0]
    else:
        max_seen = _scan_path(primary_dir)[0]
    rate_win = _RateWindow(start_t, max_seen, window_s=rate_window_s)
    current_rate: float | None = None
    wait_ev = cancel_event if cancel_event is not None else threading.Event()

    def _make_progress() -> SetupProgress:
        completed = (max_seen if max_seen > 0 else None) if primary_dir is not None else None
        return SetupProgress(
            completed_bytes=completed,
            total_bytes=None,
            percent=None,
            stage=tracker.format_stage(current_rate),
        )

    last_reported = _make_progress()
    report(last_reported)

    terminated_reason: str | None = None
    rc: int | None = None
    try:
        while True:
            rc = proc.poll()
            now = monotonic()
            if cancel_event is not None and cancel_event.is_set():
                terminated_reason = "cancelled"
                break
            cur_line_seq = tracker.line_seq()
            if cur_line_seq != prev_line_seq:
                prev_line_seq = cur_line_seq
                last_activity = now
            if rc is not None:
                break
            if (now - last_sample_t) >= sample_interval_s:
                last_sample_t = now
                sig_by_dir = {d: _scan_path(d) for d in activity_dirs}
                if activity_dirs:
                    sig = tuple(sig_by_dir[d] for d in activity_dirs)
                    if sig != prev_activity_sig:
                        prev_activity_sig = sig
                        last_activity = now
                if primary_dir is not None:
                    cur_bytes = sig_by_dir[primary_dir][0] if primary_dir in sig_by_dir else _scan_path(primary_dir)[0]
                    if cur_bytes > max_seen:
                        max_seen = cur_bytes
                    current_rate = rate_win.sample(now, max_seen)
            prog = _make_progress()
            if prog != last_reported:
                report(prog)
                last_reported = prog
            if (now - start_t) >= total_timeout_s:
                terminated_reason = "timeout"
                break
            if (now - last_activity) >= stall_timeout_s:
                terminated_reason = "stalled"
                break
            wait_ev.wait(poll_interval_s)
    finally:
        abnormal = terminated_reason is not None or (rc is None and proc.poll() is None)
        if abnormal:
            if rc is None:
                terminate_process_tree(proc, is_windows=is_windows_proc, grace_s=grace_s)
            reader.join(timeout=2.0)
        else:
            reader.join(timeout=5.0)
        # 仅在 reader 已退出后关闭 proc.stdout：读线程阻塞在 TextIOWrapper.read() 时跨线程 close() 会死锁
        if proc.stdout is not None and not reader.is_alive():
            try:
                proc.stdout.close()
            except OSError:
                pass

    if terminated_reason == "cancelled" or (cancel_event is not None and cancel_event.is_set()):
        raise LogicError(ErrorCode.dependency_failed, f"{label}已中止")
    if terminated_reason == "stalled":
        detail = tracker.error_summary() or f"连续 {int(stall_timeout_s)} 秒无输出且磁盘无写入变化"
        raise LogicError(
            ErrorCode.dependency_failed,
            _format_failure_message(
                f"{label}疑似停滞（超过 {int(stall_timeout_s)} 秒无进展）",
                detail,
                env=env,
                os_name=os_name,
                log_path=log_path,
            ),
        )
    if terminated_reason == "timeout":
        detail = tracker.error_summary() or f"超过总超时上限 {int(total_timeout_s)} 秒"
        raise LogicError(
            ErrorCode.dependency_failed,
            _format_failure_message(
                f"{label}超时（超过 {int(total_timeout_s)} 秒）",
                detail,
                env=env,
                os_name=os_name,
                log_path=log_path,
            ),
        )
    if rc != 0:
        detail = tracker.error_summary() or "未输出详细错误"
        raise LogicError(
            ErrorCode.dependency_failed,
            _format_failure_message(
                f"{label}失败（退出码 {rc}）",
                detail,
                env=env,
                os_name=os_name,
                log_path=log_path,
            ),
        )


__all__ = [
    "ERROR_SUMMARY_CHARS",
    "INSTALL_TIMEOUT_S",
    "MIN_DISPLAY_RATE_MIB_S",
    "POLL_INTERVAL_S",
    "RATE_WINDOW_S",
    "SAMPLE_INTERVAL_S",
    "STALL_TIMEOUT_S",
    "UvStreamTracker",
    "dir_bytes",
    "run_uv_streaming",
    "strip_ansi",
    "terminate_process_tree",
]
