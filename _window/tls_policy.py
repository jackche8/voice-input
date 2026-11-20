"""主窗口的唯一证书信任规则：只对 127.0.0.1 放行、且服务端出示的证书与本应用证书逐字节相同。不做任何全局忽略。"""
from __future__ import annotations

import ssl
from pathlib import Path

TRUSTED_HOST = "127.0.0.1"


def load_cert_der(cert_path: Path) -> bytes:
    """读本应用服务端证书（PEM）为 DER；缺失或格式不对直接报错（不能退回不校验）。"""
    return ssl.PEM_cert_to_DER_cert(Path(cert_path).read_text(encoding="ascii"))


def allow(host: str, presented_der: bytes, app_cert_der: bytes) -> bool:
    return host == TRUSTED_HOST and bool(app_cert_der) and presented_der == app_cert_der


__all__ = ["TRUSTED_HOST", "allow", "load_cert_der"]
