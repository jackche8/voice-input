"""跨平台本地 TLS 证书管理：长期 Root CA (`ca.pem`) + `<=397` 天网卡 SAN 自动重签服务端证书 (`cert.pem`)。

核心约束：
1. Apple iOS / macOS 强制拒绝有效期 `> 398` 天的 TLS 叶子证书；本模块固定签发默认 `365` 天（严格 `<= 397` 天），
   并在启动时自动检测和替换历史遗留的 `3650` 天违规自签证书。
2. 服务端证书 SAN 始终写入 `DNS:localhost`, `IP:127.0.0.1`, `IP:::1` 及当前全部局域网网卡 IPv4 地址。
3. CA 合法性同时校验 `ca.pem` 的 `BasicConstraints: CA:TRUE`、有效期以及 `ca-key.pem` 可读可解析且与 `ca.pem` 公钥配对；
   任一损坏或不匹配即安全重建 CA 与叶证书。
4. 证书解析与验签均不依赖 CPython 私有 `_test_decode_cert` 或系统实时墙钟 `ssl` 握手，保证自定义 `now` 下 100% 幂等。
"""
from __future__ import annotations

import argparse
import datetime as dt
import ipaddress
import json
import os
import re
import shutil
import socket
import subprocess
from collections.abc import Callable, Iterable, Sequence
from pathlib import Path
from typing import Literal

from _platform._certs_backends import (
    CA_COMMON_NAME,
    DEFAULT_CA_CERT_DAYS,
    SERVER_COMMON_NAME,
    CertificateInspection,
    has_cryptography,
    normalize_ip_str,
)
from _platform._certs_crypto import (
    generate_ca_cryptography,
    inspect_cert_cryptography,
    issue_server_cryptography,
    verify_ca_key_and_cert_cryptography,
    verify_chain_and_key_cryptography,
)
from _platform._certs_openssl import (
    generate_ca_openssl,
    inspect_cert_openssl,
    issue_server_openssl,
    verify_ca_key_and_cert_openssl,
    verify_chain_and_key_openssl,
)
from _platform.child_process import windows_subprocess_kwargs

APPLE_MAX_LEAF_DAYS = 398
DEFAULT_SERVER_CERT_DAYS = 365
RENEW_BEFORE_DAYS = 14

_RFC1918_IPV4_NETS = (
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
)


def _is_lan_ipv4(addr: str) -> str | None:
    try:
        ip = ipaddress.ip_address(addr.strip())
    except ValueError:
        return None
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped is not None:
            ip = ip.ipv4_mapped
        else:
            return None
    if ip.is_loopback or ip.is_link_local or ip.is_unspecified or ip.is_multicast:
        return None
    if any(ip in net for net in _RFC1918_IPV4_NETS):
        return str(ip)
    return None


def discover_lan_ipv4s(
    *,
    run: Callable[..., subprocess.CompletedProcess] = subprocess.run,
    which: Callable[[str], str | None] = shutil.which,
) -> list[str]:
    """跨平台枚举当前本机所有局域网 IPv4 地址（Linux `ip` / macOS `ifconfig` / Windows `ipconfig` + 路由套接字）。"""
    found: list[str] = []

    def _add(candidate: str) -> None:
        norm = _is_lan_ipv4(candidate)
        if norm and norm not in found:
            found.append(norm)

    if os.name != "nt" and which("ip"):
        try:
            proc = run(
                ["ip", "-j", "-4", "addr", "show", "up"],
                capture_output=True,
                text=True,
                timeout=3,
                check=False,
                **windows_subprocess_kwargs(encoding="utf-8", errors="replace"),
            )
            if proc.returncode == 0 and proc.stdout:
                for iface in json.loads(proc.stdout):
                    for info in iface.get("addr_info") or []:
                        _add(str(info.get("local") or ""))
        except Exception:
            pass

    if os.name != "nt" and which("ifconfig"):
        try:
            proc = run(
                ["ifconfig"],
                capture_output=True,
                text=True,
                timeout=3,
                check=False,
                **windows_subprocess_kwargs(encoding="utf-8", errors="replace"),
            )
            if proc.returncode == 0 and proc.stdout:
                for match in re.findall(r"\binet\s+(?:addr:)?(\d+\.\d+\.\d+\.\d+)\b", proc.stdout):
                    _add(match)
        except Exception:
            pass

    if os.name == "nt" and which("ipconfig"):
        try:
            proc = run(
                ["ipconfig"],
                capture_output=True,
                text=True,
                timeout=3,
                check=False,
                **windows_subprocess_kwargs(encoding="oem", errors="replace"),
            )
            if proc.returncode == 0 and proc.stdout:
                for match in re.findall(r"IPv4[^:：]*[:：]\s*(\d+\.\d+\.\d+\.\d+)", proc.stdout):
                    _add(match)
        except Exception:
            pass

    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            sockaddr = info[4]
            if sockaddr and isinstance(sockaddr[0], str):
                _add(sockaddr[0])
    except Exception:
        pass

    for probe_target in ("10.255.255.255", "192.168.255.255"):
        sock = None
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.connect((probe_target, 1))
            _add(sock.getsockname()[0])
        except Exception:
            pass
        finally:
            if sock is not None:
                try:
                    sock.close()
                except Exception:
                    pass

    return found


def inspect_certificate(cert_path: Path) -> CertificateInspection:
    """优先使用正式 `cryptography.x509` 解析证书，无 `cryptography` 时回退 `openssl x509`。"""
    if has_cryptography():
        return inspect_cert_cryptography(cert_path)
    return inspect_cert_openssl(cert_path)


def _verify_ca_key_and_cert(ca_cert_path: Path, ca_key_path: Path) -> bool:
    if has_cryptography():
        return verify_ca_key_and_cert_cryptography(ca_cert_path, ca_key_path)
    return verify_ca_key_and_cert_openssl(ca_cert_path, ca_key_path)


def _verify_chain_and_key(ca_cert_path: Path, server_cert_path: Path, server_key_path: Path) -> bool:
    """验证 `cert.pem` 与 `key.pem` 配对且确实由 `ca.pem` 签发（纯密码学验签，不依赖系统墙钟）。"""
    if has_cryptography():
        return verify_chain_and_key_cryptography(ca_cert_path, server_cert_path, server_key_path)
    return verify_chain_and_key_openssl(ca_cert_path, server_cert_path, server_key_path)


def _ca_valid(ca_cert_path: Path, ca_key_path: Path, now: dt.datetime) -> bool:
    if not ca_cert_path.is_file() or not ca_key_path.is_file():
        return False
    try:
        info = inspect_certificate(ca_cert_path)
    except Exception:
        return False
    if not info.is_ca:
        return False
    if now < info.not_before - dt.timedelta(days=1):
        return False
    if info.remaining_days(now) < 30.0:
        return False
    return _verify_ca_key_and_cert(ca_cert_path, ca_key_path)


def _server_cert_needs_renewal(
    *,
    ca_cert_path: Path,
    server_cert_path: Path,
    server_key_path: Path,
    required_ips: set[str],
    now: dt.datetime,
) -> tuple[bool, str]:
    if not server_cert_path.is_file() or not server_key_path.is_file():
        return True, "missing_server_cert_or_key"
    try:
        info = inspect_certificate(server_cert_path)
    except Exception as exc:
        return True, f"corrupt_cert:{exc}"

    if now < info.not_before - dt.timedelta(days=1):
        return True, "not_yet_valid"
    if info.remaining_days(now) < float(RENEW_BEFORE_DAYS):
        return True, "expiring_or_expired"
    if info.total_days > float(APPLE_MAX_LEAF_DAYS):
        return True, f"validity_exceeds_398_days:{info.total_days:.1f}"
    if "localhost" not in info.dns_names:
        return True, "missing_dns_localhost"
    missing_ips = sorted(required_ips - set(info.ip_addresses))
    if missing_ips:
        return True, f"missing_san_ips:{','.join(missing_ips)}"
    if not _verify_chain_and_key(ca_cert_path, server_cert_path, server_key_path):
        return True, "not_signed_by_local_ca_or_key_mismatch"
    return False, "ok"


def ensure_tls_certificates(
    cert_dir: Path,
    extra_ips: Iterable[str] = (),
    *,
    now: dt.datetime | None = None,
    ip_provider: Callable[[], Iterable[str]] | None = None,
    server_days: int = DEFAULT_SERVER_CERT_DAYS,
    backend: Literal["auto", "cryptography", "openssl"] = "auto",
) -> tuple[Path, Path, Path]:
    """确保本地根 CA 与合规短期 SAN 服务端证书就绪，返回 `(ca_cert_path, server_cert_path, server_key_path)`。"""
    if server_days < 1 or server_days > 397:
        raise ValueError(f"server_days 必须在 1..397 之间以符合 Apple <=398 天规范，收到: {server_days}")

    cert_root = Path(cert_dir)
    cert_root.mkdir(parents=True, exist_ok=True)
    ca_cert_path = cert_root / "ca.pem"
    ca_key_path = cert_root / "ca-key.pem"
    server_cert_path = cert_root / "cert.pem"
    server_key_path = cert_root / "key.pem"

    custom_now = (
        None
        if now is None
        else (now if now.tzinfo is not None else now.replace(tzinfo=dt.timezone.utc))
    )
    current_time = dt.datetime.now(dt.timezone.utc) if custom_now is None else custom_now
    use_crypto = (
        True
        if backend == "cryptography"
        else (False if backend == "openssl" else has_cryptography())
    )

    discovered = list(discover_lan_ipv4s() if ip_provider is None else ip_provider())
    ip_list: list[str] = ["127.0.0.1", "::1"]
    for raw_ip in [*discovered, *extra_ips]:
        norm = normalize_ip_str(str(raw_ip))
        if norm and norm not in ip_list:
            ip_list.append(norm)

    if not _ca_valid(ca_cert_path, ca_key_path, current_time):
        if use_crypto:
            generate_ca_cryptography(ca_cert_path, ca_key_path, current_time)
        else:
            generate_ca_openssl(cert_root, ca_cert_path, ca_key_path, custom_now=custom_now)

    needs_renew, _reason = _server_cert_needs_renewal(
        ca_cert_path=ca_cert_path,
        server_cert_path=server_cert_path,
        server_key_path=server_key_path,
        required_ips=set(ip_list),
        now=current_time,
    )
    if needs_renew:
        if use_crypto:
            issue_server_cryptography(
                ca_cert_path=ca_cert_path,
                ca_key_path=ca_key_path,
                server_cert_path=server_cert_path,
                server_key_path=server_key_path,
                ip_list=ip_list,
                days=server_days,
                now=current_time,
            )
        else:
            issue_server_openssl(
                cert_dir=cert_root,
                ca_cert_path=ca_cert_path,
                ca_key_path=ca_key_path,
                server_cert_path=server_cert_path,
                server_key_path=server_key_path,
                ip_list=ip_list,
                days=server_days,
                custom_now=custom_now,
            )

    return ca_cert_path, server_cert_path, server_key_path


def main(argv: Sequence[str] | None = None) -> int:
    """极简 CLI 入口：`python3 -m _platform.certs --cert-dir <dir>`。"""
    parser = argparse.ArgumentParser(
        prog="python3 -m _platform.certs",
        description="同步本地 Root CA 与 <=397 天全网卡 SAN 服务端 TLS 证书",
    )
    parser.add_argument(
        "--cert-dir",
        type=Path,
        default=Path("certs"),
        help="证书输出目录（默认: certs）",
    )
    args = parser.parse_args(argv)
    ca, cert, key = ensure_tls_certificates(args.cert_dir)
    print(f"CA 证书:     {ca}")
    print(f"服务端证书:  {cert}")
    print(f"服务端私钥:  {key}")
    return 0


__all__ = [
    "APPLE_MAX_LEAF_DAYS",
    "CA_COMMON_NAME",
    "DEFAULT_CA_CERT_DAYS",
    "DEFAULT_SERVER_CERT_DAYS",
    "RENEW_BEFORE_DAYS",
    "SERVER_COMMON_NAME",
    "CertificateInspection",
    "discover_lan_ipv4s",
    "ensure_tls_certificates",
    "inspect_certificate",
    "main",
]


if __name__ == "__main__":
    raise SystemExit(main())
