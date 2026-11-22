"""手机局域网入口：本机签发录音页 URL、二维码与访问令牌。

令牌只有一个来源：Runtime.auth.token（生产入口用 load_or_create_token(明确路径) 读一次，测试给假值）。
本模块不在 import 时读写任何文件。
"""
from __future__ import annotations

import json
import os
import re
import secrets
import socket
import subprocess
from collections.abc import Callable, Iterable
from pathlib import Path
from urllib.parse import quote

from _contracts import ErrorCode, LanAccessEntry, LanAccessResponse, LogicError
from _platform.child_process import windows_subprocess_kwargs
from _platform.detect import detect_os
from _platform.protocol import OsName
from _v3.auth import is_allowed_lan_ip, parse_ip

VOICE_PAGE_PATH = "/voice"


def load_or_create_token(path: Path) -> str:
    """生产入口专用：从显式路径读取令牌；不存在时以 0600 原子创建。读写失败可见，不吞错。

    测试与沙箱不得调用本函数指向 live 文件，应直接给假令牌。
    """
    try:
        existing = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        existing = ""
    except OSError as exc:
        raise LogicError(ErrorCode.config_io, f"无法读取访问令牌文件 {path}: {exc}") from exc
    if existing:
        return existing
    value = secrets.token_urlsafe(24)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return load_or_create_token(path)
    except OSError as exc:
        raise LogicError(ErrorCode.config_io, f"无法创建访问令牌文件 {path}: {exc}") from exc
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(value)
        fh.flush()
        os.fsync(fh.fileno())
    return value


def mask_token(token: str) -> str:
    return f"{token[:4]}…（共 {len(token)} 位）"


Runner = Callable[..., subprocess.CompletedProcess]
_WINDOWS_IPV4_RE = re.compile(r"IPv4[^:：]*[:：]\s*(\d{1,3}(?:\.\d{1,3}){3})(?!\d)", re.IGNORECASE)
_WINDOWS_VIRTUAL_ADAPTER_TOKENS = (
    "vethernet",
    "wsl",
    "hyper-v",
    "docker",
    "vmware",
    "vmnet",
    "virtualbox",
    "vbox",
    "host-only",
    "loopback",
    "tunnel",
    "隧道",
    "bluetooth",
    "蓝牙",
    "npcap",
    "wintun",
    "zerotier",
    "tailscale",
    "wireguard",
    "virtual",
    "虚拟",
    "teredo",
    "isatap",
)
_WINDOWS_TAP_RE = re.compile(r"(?<![a-z])tap(?![a-z])")
_MACOS_IFACE_RE = re.compile(r"^([A-Za-z0-9_.-]+):\s+flags=\w+<([^>]*)>")
_MACOS_INET_RE = re.compile(r"^\s+inet\s+(\d{1,3}(?:\.\d{1,3}){3})\b")
_MACOS_VIRTUAL_PREFIXES = (
    "lo",
    "utun",
    "tun",
    "tap",
    "bridge",
    "awdl",
    "llw",
    "ap",
    "gif",
    "stf",
    "anpi",
    "vmenet",
    "feth",
    "ipsec",
    "vmnet",
    "vboxnet",
    "docker",
)


def _filter_lan_ipv4(candidates: Iterable[str]) -> list[str]:
    found: list[str] = []
    for raw in candidates:
        ip = parse_ip(raw)
        if ip is None or ip.version != 4 or not is_allowed_lan_ip(ip) or ip.is_link_local:
            continue
        text = str(ip)
        if text not in found:
            found.append(text)
    return found


def _is_windows_virtual_adapter(header: str) -> bool:
    lower = header.lower()
    if any(token in lower for token in _WINDOWS_VIRTUAL_ADAPTER_TOKENS):
        return True
    return bool(_WINDOWS_TAP_RE.search(lower))


def _parse_windows_ipconfig(output: str) -> list[str]:
    candidates: list[str] = []
    in_adapter = False
    skip_section = False
    for line in output.splitlines():
        if not line.strip():
            continue
        if not line[0].isspace():
            if line.rstrip().endswith((":", "：")):
                in_adapter = True
                skip_section = _is_windows_virtual_adapter(line)
            else:
                in_adapter = False
                skip_section = False
            continue
        if not in_adapter or skip_section:
            continue
        match = _WINDOWS_IPV4_RE.search(line)
        if match:
            candidates.append(match.group(1))
    return _filter_lan_ipv4(candidates)


def _parse_macos_ifconfig(output: str) -> list[str]:
    candidates: list[str] = []
    allow_iface = False
    for line in output.splitlines():
        header = _MACOS_IFACE_RE.match(line)
        if header:
            name = header.group(1).lower()
            flags = {part.strip().upper() for part in header.group(2).split(",") if part.strip()}
            allow_iface = (
                "UP" in flags
                and "LOOPBACK" not in flags
                and "POINTOPOINT" not in flags
                and not name.startswith(_MACOS_VIRTUAL_PREFIXES)
            )
            continue
        if not allow_iface:
            continue
        match = _MACOS_INET_RE.match(line)
        if match:
            candidates.append(match.group(1))
    return _filter_lan_ipv4(candidates)


def _probe_lan_ipv4_via_socket() -> list[str]:
    candidates: list[str] = []
    for probe_target in ("10.255.255.255", "192.168.255.255"):
        sock = None
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.connect((probe_target, 1))
            candidates.append(str(sock.getsockname()[0]))
        except OSError:
            pass
        finally:
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            sockaddr = info[4]
            if sockaddr and isinstance(sockaddr[0], str):
                candidates.append(sockaddr[0])
    except OSError:
        pass
    return _filter_lan_ipv4(candidates)


def _list_linux_physical_lan_ipv4(*, run: Runner, sys_class_net: Path) -> list[str]:
    try:
        proc = run(
            ["ip", "-j", "-4", "addr", "show", "up"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
            **windows_subprocess_kwargs(encoding="utf-8", errors="replace"),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise LogicError(ErrorCode.dependency_failed, f"无法枚举网卡地址: {exc}") from exc
    if proc.returncode != 0:
        raise LogicError(ErrorCode.dependency_failed, f"ip addr 失败: {(proc.stderr or '').strip()}")
    try:
        interfaces = json.loads(proc.stdout or "[]")
    except json.JSONDecodeError as exc:
        raise LogicError(ErrorCode.dependency_failed, f"ip addr 输出无法解析: {exc}") from exc
    candidates: list[str] = []
    for iface in interfaces:
        name = str(iface.get("ifname") or "")
        if not name or not (sys_class_net / name / "device").exists():
            continue
        for addr in iface.get("addr_info") or []:
            candidates.append(str(addr.get("local") or ""))
    return _filter_lan_ipv4(candidates)


def list_physical_lan_ipv4(
    *,
    run: Runner = subprocess.run,
    sys_class_net: Path = Path("/sys/class/net"),
    os_name: OsName | None = None,
    socket_probe: Callable[[], list[str]] | None = None,
) -> list[str]:
    """跨平台枚举物理网卡局域网 IPv4（排除虚拟网桥/隧道/回环/链路本地）；非 Linux 仅在命令失败时回退套接字探测。"""
    resolved_os = os_name or detect_os()
    if resolved_os == "linux":
        return _list_linux_physical_lan_ipv4(run=run, sys_class_net=sys_class_net)

    probe = _probe_lan_ipv4_via_socket if socket_probe is None else socket_probe
    if resolved_os == "windows":
        try:
            proc = run(
                ["ipconfig"],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
                **windows_subprocess_kwargs(encoding="oem", errors="replace"),
            )
        except (OSError, subprocess.SubprocessError):
            return _filter_lan_ipv4(probe())
        if proc.returncode != 0:
            return _filter_lan_ipv4(probe())
        return _parse_windows_ipconfig(proc.stdout or "")

    try:
        proc = run(
            ["ifconfig"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
            **windows_subprocess_kwargs(encoding="utf-8", errors="replace"),
        )
    except (OSError, subprocess.SubprocessError):
        return _filter_lan_ipv4(probe())
    if proc.returncode != 0:
        return _filter_lan_ipv4(probe())
    return _parse_macos_ifconfig(proc.stdout or "")


def qr_svg(text: str) -> str:
    try:
        import qrcode
        import qrcode.image.svg
    except ImportError as exc:
        raise LogicError(ErrorCode.dependency_failed, f"二维码依赖 qrcode 不可用: {exc}") from exc
    image = qrcode.make(text, image_factory=qrcode.image.svg.SvgPathImage)
    data = image.to_string()
    return data.decode("utf-8") if isinstance(data, bytes) else str(data)


class LanAccessService:
    def __init__(
        self,
        *,
        token: str,
        list_ips: Callable[[], list[str]],
        port: int,
        make_qr: Callable[[str], str] = qr_svg,
    ) -> None:
        if not token.strip():
            raise ValueError("局域网入口需要非空令牌")
        self._token = token
        self._list_ips = list_ips
        self._port = port
        self._make_qr = make_qr

    def _url(self, ip: str, token: str) -> str:
        return f"https://{ip}:{self._port}{VOICE_PAGE_PATH}#token={quote(token, safe='')}"

    def describe(self) -> LanAccessResponse:
        token = self._token
        masked = mask_token(token)
        ips = self._list_ips()
        entries = [
            LanAccessEntry(
                ip=ip,
                url=self._url(ip, token),
                url_masked=f"https://{ip}:{self._port}{VOICE_PAGE_PATH}#token={masked}",
                qr_svg=self._make_qr(self._url(ip, token)),
            )
            for ip in ips
        ]
        message = "" if entries else "未找到可用的局域网地址；请确认电脑已连接局域网。"
        return LanAccessResponse(
            port=self._port,
            token=token,
            token_masked=masked,
            entries=entries,
            message=message,
        )
