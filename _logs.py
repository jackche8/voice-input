"""运行日志脱敏、字节级反向尾读、安全清空、排障汇总与关于信息（逻辑层）。

本模块为日志与关于能力的唯一纯逻辑来源，不依赖路由/业务层（`_v3`/`_desktop`）：
- 精确 9 份规范日志白名单（runtime 5 份 + setup 4 份），拒绝未知文件名与符号链接；
- 64 KiB 反向块读上限 4 MiB，在 bytes 层裁去首半行后再按 UTF-8 解码与规整（去 NUL → 拆 \\n → 先去行尾 \\r → 折叠行内 \\r → 去 ANSI → 脱敏 → 分级）；
- `strip_ansi` / `redact_log_line` / `classify_log_level` / `read_log_tail` 均单源定义供全局复用。
"""
from __future__ import annotations

import json
import os
import platform
import re
import stat
import sys
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal
from urllib.parse import quote

from _contracts import (
    DEFAULT_LOG_MAX_LINES,
    LOG_MAX_LINES_CHOICES,
    LOG_READ_MAX_BYTES,
    OLLAMA_VERSION,
    PRODUCT_ASR_MODEL,
    PRODUCT_OLLAMA_MODEL,
    AboutAsrVariant,
    AboutInfoResponse,
    AboutLinks,
    ComponentStatus,
    DiagnosticSummaryResponse,
    ErrorCode,
    LogClearResponse,
    LogGroup,
    LogLevel,
    LogLineEntry,
    LogReadResponse,
    LogSourceInfo,
    LogSourceListResponse,
    LogicError,
    RunMode,
)
from _paths import DEB_PROGRAM_ROOT, AppPaths
from _platform.system_proxy import redact_url_credentials
from _version import APP_VERSION

APP_DISPLAY_TITLE = "Voice Studio · Ollama Voice Input"
APP_SUMMARY = "100% 本地离线的跨平台语音输入、截屏原位翻译与画笔标注工作台"
LICENSE_TITLE = "AGPL-3.0 + 商业授权"
LICENSE_NOTE = "个人及企业内部非二次封装商用均免费；闭源二次封装或商业再分发需获取商业授权。"
ABOUT_LINKS = AboutLinks(
    repo_url="https://github.com/jackche8/voice-input",
    pages_url="https://jackche8.github.io/voice-input/",
    releases_url="https://github.com/jackche8/voice-input/releases",
    issues_url="https://github.com/jackche8/voice-input/issues",
)

CANONICAL_LOG_SOURCES: tuple[tuple[str, LogGroup], ...] = (
    ("app.log", LogGroup.runtime),
    ("server.log", LogGroup.runtime),
    ("window.log", LogGroup.runtime),
    ("asr.log", LogGroup.runtime),
    ("ollama.log", LogGroup.runtime),
    ("setup-asr_runtime_install.log", LogGroup.setup),
    ("setup-ollama_install.log", LogGroup.setup),
    ("setup-ollama_pull.log", LogGroup.setup),
    ("setup-asr_download.log", LogGroup.setup),
)
CANONICAL_LOG_GROUPS: Mapping[str, LogGroup] = dict(CANONICAL_LOG_SOURCES)

_READ_CHUNK_BYTES = 64 * 1024
_DEFAULT_SUMMARY_TAIL_LINES = 80
_DEFAULT_SUMMARY_MAX_BYTES = 64 * 1024
_MIN_SECRET_TOKEN_LEN = 4
_ASR_MANIFEST_VARIANTS = frozenset({"cuda", "mps", "cpu"})

_ANSI_ESCAPE_RE = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\))")

# 裸 Windows 代理写法（无 ://）：https=user:pass@proxy:7890
_BARE_PROXY_CRED_RE = re.compile(
    r"\b((?:https?|socks5h?)=)([^\s/?#\"'<>;,]+)@",
    re.IGNORECASE,
)

# R2 / S3 预签名查询参数
_AMZ_QUERY_SECRET_RE = re.compile(
    r"(?<![A-Za-z0-9_-])(X-Amz-(?:Signature|Credential|Security-Token))(=)([^&\s\"'<>),;]+)",
    re.IGNORECASE,
)

# Authorization / Proxy-Authorization：JSON/字典带引号形式（严格止步于闭合引号，绝不吞后续字段）
_AUTH_QUOTED_DQ_RE = re.compile(
    r'(?<![A-Za-z0-9_-])(?P<kq>["\']?)(?P<key>(?:Proxy-)?Authorization)(?P=kq)'
    r'(?P<sep>[ \t]*[:=][ \t]*)"(?P<val>(?:[^"\\\r\n]|\\.)*)"',
    re.IGNORECASE,
)
_AUTH_QUOTED_SQ_RE = re.compile(
    r"(?<![A-Za-z0-9_-])(?P<kq>[\"']?)(?P<key>(?:Proxy-)?Authorization)(?P=kq)"
    r"(?P<sep>[ \t]*[:=][ \t]*)'(?P<val>(?:[^'\\\r\n]|\\.)*)'",
    re.IGNORECASE,
)

# Authorization / Proxy-Authorization：非引号形式（兼容任意 scheme 与裸凭证，不误吞后续 key=val 字段）
_AUTH_MULTI_PARAM_SCHEMES = r"(?:Digest|AWS4-HMAC-SHA256)"
_AUTH_UNQUOTED_MULTI_RE = re.compile(
    rf"(?<![A-Za-z0-9_-])(?P<key>(?:Proxy-)?Authorization)(?P<sep>[ \t]*[:=][ \t]*)"
    rf"(?P<scheme>{_AUTH_MULTI_PARAM_SCHEMES})[ \t]+(?P<val>[^\r\n}}\]]+)",
    re.IGNORECASE,
)
_AUTH_UNQUOTED_RE = re.compile(
    r"(?<![A-Za-z0-9_-])(?P<key>(?:Proxy-)?Authorization)(?P<sep>[ \t]*[:=][ \t]*)"
    r"(?!\"|')(?:"
    r"(?P<scheme>[A-Za-z][A-Za-z0-9_-]{1,24})[ \t]+(?![A-Za-z_][A-Za-z0-9_-]*=)(?P<sval>[^\s,;\"'<>}\]]+)"
    r"|(?P<oval>[^\s,;\"'<>}\]]+)"
    r")",
    re.IGNORECASE,
)

# 敏感键名列表（覆盖配置/请求头/查询串/环境变量）
_SENSITIVE_KEY_ALT = (
    r"web_input_token|x-voice-token|hugging_face_hub_token|hf_token|"
    r"baidu_appkey|api_key|password|passwd|secret|token"
)
_KV_QUOTED_DQ_RE = re.compile(
    rf'(?<![A-Za-z0-9_-])(?P<kq>["\']?)(?P<key>{_SENSITIVE_KEY_ALT})(?P=kq)'
    r'(?P<sep>[ \t]*[:=][ \t]*)"(?P<val>(?:[^"\\\r\n]|\\.)*)"',
    re.IGNORECASE,
)
_KV_QUOTED_SQ_RE = re.compile(
    rf"(?<![A-Za-z0-9_-])(?P<kq>[\"']?)(?P<key>{_SENSITIVE_KEY_ALT})(?P=kq)"
    r"(?P<sep>[ \t]*[:=][ \t]*)'(?P<val>(?:[^'\\\r\n]|\\.)*)'",
    re.IGNORECASE,
)
_KV_UNQUOTED_RE = re.compile(
    rf"(?<![A-Za-z0-9_-])(?P<kq>[\"']?)(?P<key>{_SENSITIVE_KEY_ALT})(?P=kq)"
    r"(?P<sep>[ \t]*[:=][ \t]*)(?!\"|')(?P<val>[^\s&\"'<>),;}\]]+)",
    re.IGNORECASE,
)

_ERROR_LEVEL_RE = re.compile(
    r"\[(?:ERROR|CRITICAL|FATAL)\]"
    r"|(?:^|[\s|\]])(?:ERROR|CRITICAL|FATAL):"
    r"|\blevel=(?:ERROR|CRITICAL|FATAL)\b"
    r"|Traceback \(most recent call last\):"
    r"|^\s*error:"
    r"|^\s*×\s",
)
_ERROR_LEVEL_CI_RE = re.compile(
    r"\[(?:ERROR|CRITICAL|FATAL)\]|\blevel=(?:ERROR|CRITICAL|FATAL)\b",
    re.IGNORECASE,
)
_WARN_LEVEL_RE = re.compile(
    r"\[WARN(?:ING)?\]"
    r"|(?:^|[\s|\]])WARN(?:ING)?:"
    r"|\blevel=WARN(?:ING)?\b"
    r"|^\s*warning:",
    re.IGNORECASE,
)

_VARIANT_DEFAULT_DETAIL: Mapping[str, str] = {
    "cuda": "CUDA GPU 加速",
    "mps": "Apple Silicon MPS 加速",
    "cpu": "CPU 推理",
}


def strip_ansi(text: str) -> str:
    """剥离 ANSI CSI / OSC 转义序列（全项目唯一实现）。"""
    if not text:
        return ""
    return _ANSI_ESCAPE_RE.sub("", text)


def _redact_auth_quoted(match: re.Match[str], quote_char: str) -> str:
    val = match.group("val")
    if not val:
        return match.group(0)
    prefix = f"{match.group('kq')}{match.group('key')}{match.group('kq')}{match.group('sep')}"
    scheme_match = re.match(r"^([A-Za-z][A-Za-z0-9_-]{1,24}[ \t]+)(.+)$", val)
    if scheme_match:
        scheme_prefix, rest = scheme_match.group(1), scheme_match.group(2)
        if not rest.strip():
            return match.group(0)
        return f"{prefix}{quote_char}{scheme_prefix}***{quote_char}"
    if not val.strip():
        return match.group(0)
    return f"{prefix}{quote_char}***{quote_char}"


def _redact_auth_unquoted(match: re.Match[str]) -> str:
    prefix = f"{match.group('key')}{match.group('sep')}"
    scheme = match.group("scheme")
    if scheme is not None:
        return f"{prefix}{scheme} ***"
    return f"{prefix}***"


def _redact_kv_quoted(match: re.Match[str], quote_char: str) -> str:
    val = match.group("val")
    if not val:
        return match.group(0)
    prefix = f"{match.group('kq')}{match.group('key')}{match.group('kq')}{match.group('sep')}"
    return f"{prefix}{quote_char}***{quote_char}"


def _prepare_secret_needles(secret_tokens: Iterable[str]) -> tuple[str, ...]:
    needles: set[str] = set()
    for raw in secret_tokens:
        if not isinstance(raw, str):
            continue
        cleaned = raw.strip()
        if len(cleaned) < _MIN_SECRET_TOKEN_LEN:
            continue
        needles.add(cleaned)
        encoded = quote(cleaned, safe="")
        if len(encoded) >= _MIN_SECRET_TOKEN_LEN:
            needles.add(encoded)
    return tuple(sorted(needles, key=len, reverse=True))


def _redact_line_with_needles(line: str, needles: tuple[str, ...]) -> str:
    if not line:
        return ""
    text = line
    for needle in needles:
        if needle in text:
            text = text.replace(needle, "***")
    text = redact_url_credentials(text)
    text = _BARE_PROXY_CRED_RE.sub(r"\1***@", text)
    text = _AMZ_QUERY_SECRET_RE.sub(r"\1\2***", text)
    text = _AUTH_QUOTED_DQ_RE.sub(lambda m: _redact_auth_quoted(m, '"'), text)
    text = _AUTH_QUOTED_SQ_RE.sub(lambda m: _redact_auth_quoted(m, "'"), text)
    text = _AUTH_UNQUOTED_MULTI_RE.sub(r"\g<key>\g<sep>\g<scheme> ***", text)
    text = _AUTH_UNQUOTED_RE.sub(_redact_auth_unquoted, text)
    text = _KV_QUOTED_DQ_RE.sub(lambda m: _redact_kv_quoted(m, '"'), text)
    text = _KV_QUOTED_SQ_RE.sub(lambda m: _redact_kv_quoted(m, "'"), text)
    text = _KV_UNQUOTED_RE.sub(r"\g<kq>\g<key>\g<kq>\g<sep>***", text)
    return text


def redact_log_line(line: str, *, secret_tokens: Iterable[str] = ()) -> str:
    """单源日志与排障文本脱敏：显式令牌、URL/裸代理凭证、R2 预签名、认证头与常见敏感键值。"""
    if not line:
        return ""
    return _redact_line_with_needles(line, _prepare_secret_needles(secret_tokens))


def classify_log_level(line: str) -> LogLevel:
    """单源日志级别判定：仅识别真实错误/告警标记，不误伤 `error=None` / `"error": null`。"""
    if not line:
        return LogLevel.info
    if _ERROR_LEVEL_RE.search(line) or _ERROR_LEVEL_CI_RE.search(line):
        return LogLevel.error
    if _WARN_LEVEL_RE.search(line):
        return LogLevel.warn
    return LogLevel.info


def _resolve_canonical_log(logs_dir: Path, name: str) -> tuple[Path, LogGroup]:
    group = CANONICAL_LOG_GROUPS.get(name)
    if group is None:
        raise LogicError(ErrorCode.not_found, f"未知日志文件: {name}")
    return Path(logs_dir) / name, group


def _lstat_regular_or_missing(path: Path, name: str) -> os.stat_result | None:
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise LogicError(ErrorCode.logs_io, f"无法读取日志文件状态 {name}: {exc}") from exc
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
        raise LogicError(ErrorCode.logs_io, f"日志路径必须是普通文件（拒绝符号链接或非常规文件）: {name}")
    return st


def _open_regular_fd(path: Path, flags: int, name: str) -> tuple[int, os.stat_result]:
    open_flags = flags | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(os.fspath(path), open_flags)
    except OSError as exc:
        raise LogicError(ErrorCode.logs_io, f"无法打开日志文件 {name}: {exc}") from exc
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise LogicError(ErrorCode.logs_io, f"日志路径必须是普通文件: {name}")
    except BaseException:
        os.close(fd)
        raise
    return fd, st


def _format_mtime_iso(mtime: float) -> str:
    return datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat()


@dataclass(frozen=True)
class LogTailResult:
    exists: bool
    bytes: int
    updated_at: str | None
    truncated: bool
    entries: tuple[LogLineEntry, ...]
    has_error: bool
    has_warn: bool


def read_log_tail(
    path: Path,
    max_lines: int = DEFAULT_LOG_MAX_LINES,
    *,
    max_bytes: int = LOG_READ_MAX_BYTES,
    secret_tokens: Iterable[str] = (),
) -> LogTailResult:
    """受 4 MiB 字节上限约束的 64 KiB 反向块尾读，输出规整、脱敏并分级的行列表。"""
    if max_lines < 1:
        raise ValueError(f"max_lines 必须 >= 1，收到 {max_lines}")
    if max_bytes < 1:
        raise ValueError(f"max_bytes 必须 >= 1，收到 {max_bytes}")

    target = Path(path)
    label = target.name
    st0 = _lstat_regular_or_missing(target, label)
    if st0 is None:
        return LogTailResult(
            exists=False,
            bytes=0,
            updated_at=None,
            truncated=False,
            entries=(),
            has_error=False,
            has_warn=False,
        )

    fd, st = _open_regular_fd(target, os.O_RDONLY, label)
    file_size = st.st_size
    updated_at = _format_mtime_iso(st.st_mtime)
    if file_size == 0:
        os.close(fd)
        return LogTailResult(
            exists=True,
            bytes=0,
            updated_at=updated_at,
            truncated=False,
            entries=(),
            has_error=False,
            has_warn=False,
        )

    chunks: list[bytes] = []
    offset = file_size
    bytes_read = 0
    newline_count = 0
    target_newlines = max_lines + 1
    try:
        with os.fdopen(fd, "rb") as fh:
            while offset > 0 and bytes_read < max_bytes and newline_count < target_newlines:
                to_read = min(_READ_CHUNK_BYTES, offset, max_bytes - bytes_read)
                offset -= to_read
                fh.seek(offset)
                chunk = fh.read(to_read)
                if not chunk:
                    break
                chunks.append(chunk)
                bytes_read += len(chunk)
                newline_count += chunk.count(b"\n")
    except OSError as exc:
        raise LogicError(ErrorCode.logs_io, f"读取日志文件失败 {label}: {exc}") from exc

    raw = b"".join(reversed(chunks))
    truncated = False
    if offset > 0:
        truncated = True
        first_nl = raw.find(b"\n")
        # 若 4 MiB 窗口内有完整换行边界则丢弃首半行；若无换行或仅末尾单一换行则保留截断内容
        if first_nl != -1 and first_nl < len(raw) - 1:
            raw = raw[first_nl + 1 :]

    text = raw.decode("utf-8", errors="replace").replace("\x00", "")
    raw_lines = text.split("\n")
    if raw_lines and raw_lines[-1] == "":
        raw_lines.pop()
    if len(raw_lines) > max_lines:
        raw_lines = raw_lines[-max_lines:]
        truncated = True

    prepared_tokens = _prepare_secret_needles(secret_tokens)
    entries: list[LogLineEntry] = []
    has_error = False
    has_warn = False
    for idx, raw_line in enumerate(raw_lines, start=1):
        # 必须先去行尾 \r（保 CRLF 行），再按行内 \r 折叠进度条覆写
        clean = raw_line.rstrip("\r")
        if "\r" in clean:
            clean = clean.rsplit("\r", 1)[-1]
        clean = strip_ansi(clean)
        clean = _redact_line_with_needles(clean, prepared_tokens)
        level = classify_log_level(clean)
        if level == LogLevel.error:
            has_error = True
        elif level == LogLevel.warn:
            has_warn = True
        entries.append(LogLineEntry(line_no=idx, text=clean, level=level))

    return LogTailResult(
        exists=True,
        bytes=file_size,
        updated_at=updated_at,
        truncated=truncated,
        entries=tuple(entries),
        has_error=has_error,
        has_warn=has_warn,
    )


def _normalize_max_lines_choice(value: int) -> Literal[500, 2000, 5000]:
    if value == 500:
        return 500
    if value == 5000:
        return 5000
    return 2000


def list_logs(
    logs_dir: Path,
    *,
    max_lines: int = DEFAULT_LOG_MAX_LINES,
    secret_tokens: Iterable[str] = (),
) -> LogSourceListResponse:
    """扫描 9 份规范日志文件元信息（与详情共用同一反向尾读窗口）。"""
    effective = _normalize_max_lines_choice(max_lines)
    base = Path(logs_dir)
    tokens = tuple(secret_tokens)
    sources: list[LogSourceInfo] = []
    for name, group in CANONICAL_LOG_SOURCES:
        tail = read_log_tail(base / name, max_lines=effective, secret_tokens=tokens)
        sources.append(
            LogSourceInfo(
                name=name,
                group=group,
                exists=tail.exists,
                line_count=len(tail.entries),
                bytes=tail.bytes,
                truncated=tail.truncated,
                has_error=tail.has_error,
                has_warn=tail.has_warn,
                updated_at=tail.updated_at,
            )
        )
    return LogSourceListResponse(
        log_max_lines=effective,
        allowed_max_lines=list(LOG_MAX_LINES_CHOICES),
        sources=sources,
    )


def read_log(
    logs_dir: Path,
    name: str,
    *,
    max_lines: int = DEFAULT_LOG_MAX_LINES,
    secret_tokens: Iterable[str] = (),
) -> LogReadResponse:
    """读取单份规范日志的脱敏尾行。"""
    if max_lines < 1 or max_lines > 5000:
        raise LogicError(ErrorCode.invalid_request, f"lines 必须在 1..5000 之间，收到 {max_lines}")
    target, group = _resolve_canonical_log(logs_dir, name)
    tail = read_log_tail(target, max_lines=max_lines, secret_tokens=secret_tokens)
    return LogReadResponse(
        name=name,
        group=group,
        exists=tail.exists,
        max_lines=max_lines,
        line_count=len(tail.entries),
        bytes=tail.bytes,
        truncated=tail.truncated,
        has_error=tail.has_error,
        has_warn=tail.has_warn,
        updated_at=tail.updated_at,
        entries=list(tail.entries),
    )


def _unlink_rotated_backup(target: Path) -> None:
    backup = target.with_name(target.name + ".1")
    try:
        st = os.lstat(backup)
    except FileNotFoundError:
        return
    except OSError as exc:
        raise LogicError(ErrorCode.logs_io, f"无法访问归档日志 {backup.name}: {exc}") from exc
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
        raise LogicError(ErrorCode.logs_io, f"拒绝删除符号链接或非常规归档日志: {backup.name}")
    try:
        backup.unlink()
    except FileNotFoundError:
        return
    except OSError as exc:
        raise LogicError(ErrorCode.logs_io, f"无法删除归档日志 {backup.name}: {exc}") from exc


def clear_log(
    logs_dir: Path,
    name: str,
    *,
    active_setup_logs: Collection[str] = (),
) -> LogClearResponse:
    """安全原地截断单份规范日志（r+b truncate(0)）并清理 .1 备份；活跃安装日志返回 409 conflict。"""
    target, _group = _resolve_canonical_log(logs_dir, name)
    if name in active_setup_logs:
        raise LogicError(ErrorCode.conflict, f"日志文件 {name} 对应的安装任务正在运行，请等待结束后再清空")

    st0 = _lstat_regular_or_missing(target, name)
    if st0 is None:
        _unlink_rotated_backup(target)
        return LogClearResponse(name=name, bytes_before=0)

    fd, st = _open_regular_fd(target, os.O_RDWR, name)
    bytes_before = st.st_size
    try:
        with os.fdopen(fd, "r+b") as fh:
            fh.truncate(0)
            fh.flush()
    except OSError as exc:
        raise LogicError(ErrorCode.logs_io, f"清空日志文件失败 {name}: {exc}") from exc

    _unlink_rotated_backup(target)
    return LogClearResponse(name=name, bytes_before=bytes_before)


def detect_run_mode(
    *,
    os_name: Literal["linux", "windows", "macos"],
    program_root: Path,
    frozen: bool | None = None,
) -> RunMode:
    """判定应用运行形态：PyInstaller 冻结版、Linux .deb 安装版或源码运行。"""
    is_frozen = bool(getattr(sys, "frozen", False)) if frozen is None else frozen
    if is_frozen:
        return RunMode.frozen
    if os_name == "linux":
        try:
            if Path(program_root).resolve() == DEB_PROGRAM_ROOT:
                return RunMode.deb
        except OSError:
            pass
    return RunMode.source


def _read_linux_pretty_name(os_release_path: Path = Path("/etc/os-release")) -> str | None:
    try:
        content = os_release_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    for line in content.splitlines():
        if line.startswith("PRETTY_NAME="):
            value = line.split("=", 1)[1].strip().strip('"').strip("'")
            if value:
                return value
    return None


def _detect_platform_detail(os_name: Literal["linux", "windows", "macos"]) -> str:
    try:
        arch = platform.machine() or "unknown"
        if os_name == "linux":
            distro = _read_linux_pretty_name() or f"Linux {platform.release()}"
            return f"{distro} ({arch})"
        if os_name == "macos":
            ver = platform.mac_ver()[0] or platform.release()
            return f"macOS {ver} ({arch})"
        return f"Windows {platform.release()} ({arch})"
    except Exception:  # noqa: BLE001 — About 信息 fail-open，不得阻断页面
        return os_name


def _detect_session_type(os_name: Literal["linux", "windows", "macos"], environ: Mapping[str, str]) -> str:
    if os_name == "linux":
        session = (environ.get("XDG_SESSION_TYPE") or "").strip().lower()
        if session:
            return session
        if environ.get("WAYLAND_DISPLAY"):
            return "wayland"
        if environ.get("DISPLAY"):
            return "x11"
        return "headless"
    return "desktop"


def _read_about_asr_manifest(
    manifest_path: Path,
    *,
    hardware_detail: str | None = None,
) -> tuple[AboutAsrVariant | None, str]:
    try:
        raw_text = manifest_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        detail = hardware_detail.strip() if hardware_detail and hardware_detail.strip() else "尚未安装识别运行环境"
        return None, detail
    except OSError as exc:
        return AboutAsrVariant.corrupt, f"清单无法读取：{exc}"

    try:
        payload = json.loads(raw_text)
    except ValueError as exc:
        return AboutAsrVariant.corrupt, f"清单 JSON 损坏：{exc}"

    variant = payload.get("variant") if isinstance(payload, dict) else None
    if variant not in _ASR_MANIFEST_VARIANTS:
        return AboutAsrVariant.corrupt, f"清单 variant={variant!r} 非法"

    resolved = AboutAsrVariant(variant)
    base = _VARIANT_DEFAULT_DETAIL[variant]
    extra = hardware_detail.strip() if hardware_detail and hardware_detail.strip() else ""
    detail = f"{base} · {extra}" if extra and extra != base else base
    return resolved, detail


def build_about_info(
    paths: AppPaths,
    *,
    version: str = APP_VERSION,
    session_type: str | None = None,
    platform_detail: str | None = None,
    python_version: str | None = None,
    run_mode: RunMode | None = None,
    hardware_detail: str | None = None,
    ollama_model: str | None = None,
    ollama_version: str | None = None,
    frozen: bool | None = None,
    environ: Mapping[str, str] | None = None,
) -> AboutInfoResponse:
    """非阻塞构造关于页信息：任何可选探测缺失或清单损坏均降级展示，绝不抛异常。"""
    env = os.environ if environ is None else environ
    os_name = paths.os_name
    resolved_platform = platform_detail if platform_detail is not None else _detect_platform_detail(os_name)
    resolved_session = session_type if session_type is not None else _detect_session_type(os_name, env)
    resolved_python = python_version if python_version is not None else platform.python_version()
    resolved_mode = (
        run_mode
        if run_mode is not None
        else detect_run_mode(os_name=os_name, program_root=paths.program_root, frozen=frozen)
    )
    variant, asr_detail = _read_about_asr_manifest(paths.asr_manifest, hardware_detail=hardware_detail)
    return AboutInfoResponse(
        app_name=APP_DISPLAY_TITLE,
        version=version,
        summary=APP_SUMMARY,
        os_name=os_name,
        platform_detail=resolved_platform,
        session_type=resolved_session,
        python_version=resolved_python,
        run_mode=resolved_mode,
        asr_variant=variant,
        asr_detail=redact_log_line(asr_detail),
        asr_model=PRODUCT_ASR_MODEL,
        ollama_model=(ollama_model or PRODUCT_OLLAMA_MODEL),
        ollama_version=(ollama_version or OLLAMA_VERSION),
        data_root=str(paths.data_root),
        logs_dir=str(paths.logs),
        config_root=str(paths.config_root),
        license_title=LICENSE_TITLE,
        license_note=LICENSE_NOTE,
        links=ABOUT_LINKS,
    )


def build_diagnostic_summary(
    logs_dir: Path,
    *,
    about: AboutInfoResponse,
    components: Sequence[ComponentStatus] = (),
    log_max_lines: int = DEFAULT_LOG_MAX_LINES,
    secret_tokens: Iterable[str] = (),
    tail_lines_per_file: int = _DEFAULT_SUMMARY_TAIL_LINES,
    max_summary_bytes: int = _DEFAULT_SUMMARY_MAX_BYTES,
    now: datetime | None = None,
) -> DiagnosticSummaryResponse:
    """组装一键复制排障汇总：合并环境摘要、脱敏后的组件状态与各非空日志尾段。"""
    if tail_lines_per_file < 1:
        raise ValueError(f"tail_lines_per_file 必须 >= 1，收到 {tail_lines_per_file}")
    if max_summary_bytes < 256:
        raise ValueError(f"max_summary_bytes 必须 >= 256，收到 {max_summary_bytes}")

    effective_cap = _normalize_max_lines_choice(log_max_lines)
    tokens = tuple(secret_tokens)
    prepared_tokens = _prepare_secret_needles(tokens)
    timestamp = (now or datetime.now(tz=timezone.utc)).astimezone(timezone.utc).isoformat()
    asr_label = about.asr_variant.value if about.asr_variant is not None else "未安装"

    lines: list[str] = [
        f"=== {about.app_name} 排障汇总 ===",
        f"生成时间: {timestamp}",
        f"版本: {about.version} ({about.run_mode.value})",
        f"系统: {about.os_name} / {about.platform_detail} / session={about.session_type}",
        f"Python: {about.python_version}",
        f"ASR: {asr_label} ({about.asr_detail}) | 模型: {about.asr_model}",
        f"Ollama: {about.ollama_version} | 模型: {about.ollama_model}",
        f"目录: data={about.data_root} | logs={about.logs_dir} | config={about.config_root}",
        f"单文件日志上限: {effective_cap} 行",
        "",
        "=== 组件状态 ===",
    ]
    if components:
        for comp in components:
            reason_text = " ".join(comp.reason.splitlines()).strip() if comp.reason else "-"
            pid_text = str(comp.pid) if comp.pid is not None else "-"
            lines.append(
                f"- {comp.name.value}: state={comp.state.value} pid={pid_text} "
                f"failures={comp.failures} since={comp.since} reason={reason_text}"
            )
    else:
        lines.append("- （暂无看护层组件上报）")

    header_text = "\n".join(_redact_line_with_needles(line, prepared_tokens) for line in lines)
    parts: list[str] = [header_text]
    used_bytes = len(header_text.encode("utf-8"))

    base = Path(logs_dir)
    included_files: list[str] = []
    truncated = False
    per_file_cap = min(tail_lines_per_file, effective_cap)
    ellipsis_note = "\n...（排障汇总已达体积上限，其余日志已省略）"
    ellipsis_bytes = len(ellipsis_note.encode("utf-8"))

    for name, _group in CANONICAL_LOG_SOURCES:
        tail = read_log_tail(base / name, max_lines=per_file_cap, secret_tokens=tokens)
        if not tail.exists or not tail.entries:
            continue
        section_header = f"\n\n=== {name} (最近 {len(tail.entries)} 行, {tail.bytes} B) ===\n"
        section_body = "\n".join(entry.text for entry in tail.entries)
        section = section_header + section_body
        section_bytes = len(section.encode("utf-8"))

        if used_bytes + section_bytes <= max_summary_bytes:
            parts.append(section)
            used_bytes += section_bytes
            included_files.append(name)
            if tail.truncated:
                truncated = True
            continue

        # 剩余空间装不下完整分节：尽力放入头部 + 可容纳的前缀行并标记 truncated=True
        truncated = True
        remaining = max_summary_bytes - used_bytes - ellipsis_bytes
        header_bytes = len(section_header.encode("utf-8"))
        if remaining > header_bytes:
            kept_lines: list[str] = []
            acc = header_bytes
            for entry in tail.entries:
                line_bytes = len(entry.text.encode("utf-8")) + (1 if kept_lines else 0)
                if acc + line_bytes > remaining:
                    break
                kept_lines.append(entry.text)
                acc += line_bytes
            if kept_lines:
                parts.append(section_header + "\n".join(kept_lines))
                included_files.append(name)
        parts.append(ellipsis_note)
        break

    final_text = "".join(parts)
    return DiagnosticSummaryResponse(
        generated_at=timestamp,
        log_max_lines=effective_cap,
        truncated=truncated,
        included_files=included_files,
        text=final_text,
    )


class LogService:
    """运行日志与关于能力的组合门面（纯逻辑注入，由业务层传入实时读取闭包）。"""

    def __init__(
        self,
        *,
        paths: AppPaths,
        get_log_max_lines: Callable[[], int],
        get_secret_tokens: Callable[[], Iterable[str]] = lambda: (),
        get_components: Callable[[], Sequence[ComponentStatus]] = lambda: (),
        get_active_setup_logs: Callable[[], Collection[str]] = lambda: (),
        get_hardware_detail: Callable[[], str | None] | None = None,
        get_ollama_version: Callable[[], str | None] | None = None,
        get_ollama_model: Callable[[], str | None] | None = None,
        session_type: str | None = None,
    ) -> None:
        self._paths = paths
        self._get_log_max_lines = get_log_max_lines
        self._get_secret_tokens = get_secret_tokens
        self._get_components = get_components
        self._get_active_setup_logs = get_active_setup_logs
        self._get_hardware_detail = get_hardware_detail
        self._get_ollama_version = get_ollama_version
        self._get_ollama_model = get_ollama_model
        self._session_type = session_type

    def _effective_max_lines(self) -> Literal[500, 2000, 5000]:
        try:
            raw = int(self._get_log_max_lines())
        except Exception:  # noqa: BLE001 — 读取异常回落默认上限
            return DEFAULT_LOG_MAX_LINES
        return _normalize_max_lines_choice(raw)

    def _safe_optional(self, getter: Callable[[], str | None] | None) -> str | None:
        if getter is None:
            return None
        try:
            return getter()
        except Exception:  # noqa: BLE001 — 可选探针 fail-open，绝不阻断 About/汇总
            return None

    def list_sources(self) -> LogSourceListResponse:
        return list_logs(
            self._paths.logs,
            max_lines=self._effective_max_lines(),
            secret_tokens=self._get_secret_tokens(),
        )

    def read(self, name: str, *, lines: int | None = None) -> LogReadResponse:
        effective = self._effective_max_lines() if lines is None else lines
        return read_log(
            self._paths.logs,
            name,
            max_lines=effective,
            secret_tokens=self._get_secret_tokens(),
        )

    def clear(self, name: str) -> LogClearResponse:
        return clear_log(
            self._paths.logs,
            name,
            active_setup_logs=self._get_active_setup_logs(),
        )

    def about(self) -> AboutInfoResponse:
        return build_about_info(
            self._paths,
            session_type=self._session_type,
            hardware_detail=self._safe_optional(self._get_hardware_detail),
            ollama_model=self._safe_optional(self._get_ollama_model),
            ollama_version=self._safe_optional(self._get_ollama_version),
        )

    def diagnostic_summary(self) -> DiagnosticSummaryResponse:
        return build_diagnostic_summary(
            self._paths.logs,
            about=self.about(),
            components=self._get_components(),
            log_max_lines=self._effective_max_lines(),
            secret_tokens=self._get_secret_tokens(),
        )


__all__ = [
    "ABOUT_LINKS",
    "APP_DISPLAY_TITLE",
    "APP_SUMMARY",
    "CANONICAL_LOG_GROUPS",
    "CANONICAL_LOG_SOURCES",
    "LICENSE_NOTE",
    "LICENSE_TITLE",
    "LogService",
    "LogTailResult",
    "build_about_info",
    "build_diagnostic_summary",
    "classify_log_level",
    "clear_log",
    "detect_run_mode",
    "list_logs",
    "read_log",
    "read_log_tail",
    "redact_log_line",
    "strip_ansi",
]
