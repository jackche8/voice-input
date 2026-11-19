"""本地 TLS 证书后端公共数据模型与基础工具（被 `_certs_crypto` / `_certs_openssl` / `certs` 单向依赖）。"""
from __future__ import annotations

import datetime as dt
import ipaddress
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

CA_COMMON_NAME = "Ollama Voice Input Local Root CA"
SERVER_COMMON_NAME = "ollama-voice-input"
DEFAULT_CA_CERT_DAYS = 3650


@dataclass(frozen=True)
class CertificateInspection:
    not_before: dt.datetime
    not_after: dt.datetime
    dns_names: frozenset[str]
    ip_addresses: frozenset[str]
    subject_cn: str
    issuer_cn: str
    is_ca: bool = False

    @property
    def total_days(self) -> float:
        return (self.not_after - self.not_before).total_seconds() / 86400.0

    def remaining_days(self, now: dt.datetime) -> float:
        return (self.not_after - now).total_seconds() / 86400.0


def normalize_ip_str(addr: str) -> str | None:
    try:
        ip = ipaddress.ip_address(addr.strip())
        if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
            ip = ip.ipv4_mapped
        return str(ip)
    except ValueError:
        return None


def atomic_write_bytes(path: Path, data: bytes, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        try:
            os.chmod(tmp, mode)
        except OSError:
            pass
        os.replace(tmp, path)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise


def has_cryptography() -> bool:
    try:
        import cryptography.x509  # noqa: F401

        return True
    except Exception:
        return False
