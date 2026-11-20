"""本地 TLS 证书 `openssl` CLI 回退后端（供未安装 `cryptography` 的隔离环境使用，不触碰 `/tmp`）。"""
from __future__ import annotations

import datetime as dt
import os
import re
import shutil
import ssl
import subprocess
import tempfile
from pathlib import Path

from _atomic_file import atomic_write_bytes
from _platform._certs_backends import (
    CA_COMMON_NAME,
    DEFAULT_CA_CERT_DAYS,
    SERVER_COMMON_NAME,
    CertificateInspection,
    normalize_ip_str,
)


def run_openssl(args: list[str], *, cwd: Path | None = None) -> str:
    openssl_bin = shutil.which("openssl")
    if not openssl_bin:
        raise RuntimeError("未找到 cryptography 库且系统缺少 openssl 命令，无法操作本地 TLS 证书")
    proc = subprocess.run(
        [openssl_bin, *args],
        cwd=str(cwd) if cwd is not None else None,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        raise RuntimeError(f"openssl {' '.join(args[:2])} 失败: {detail}")
    return proc.stdout or ""


def _extract_cn_from_openssl_line(line: str) -> str:
    match = re.search(r"\bCN\s*=\s*([^,\n/]+)", line)
    return match.group(1).strip() if match else ""


def inspect_cert_openssl(cert_path: Path) -> CertificateInspection:
    text = run_openssl(["x509", "-in", str(cert_path), "-noout", "-text"])
    m_before = re.search(r"Not Before\s*:\s*(.+)", text)
    m_after = re.search(r"Not After\s*:\s*(.+)", text)
    if not m_before or not m_after:
        raise ValueError(f"无法从 openssl 输出解析证书有效期: {cert_path}")

    not_before = dt.datetime.fromtimestamp(
        ssl.cert_time_to_seconds(m_before.group(1).strip()), tz=dt.timezone.utc
    )
    not_after = dt.datetime.fromtimestamp(
        ssl.cert_time_to_seconds(m_after.group(1).strip()), tz=dt.timezone.utc
    )

    subject_cn = ""
    issuer_cn = ""
    is_ca = False
    dns_set: set[str] = set()
    ip_set: set[str] = set()

    lines = text.splitlines()
    for idx, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("Subject:") and not subject_cn:
            subject_cn = _extract_cn_from_openssl_line(stripped)
        elif stripped.startswith("Issuer:") and not issuer_cn:
            issuer_cn = _extract_cn_from_openssl_line(stripped)
        elif stripped.startswith("X509v3 Basic Constraints:"):
            if idx + 1 < len(lines) and re.search(r"\bCA:TRUE\b", lines[idx + 1], re.IGNORECASE):
                is_ca = True
        elif stripped.startswith("X509v3 Subject Alternative Name:"):
            if idx + 1 < len(lines):
                for item in lines[idx + 1].split(","):
                    entry = item.strip()
                    if entry.startswith("DNS:"):
                        dns_set.add(entry[4:].strip().lower())
                    elif entry.startswith("IP Address:"):
                        norm = normalize_ip_str(entry[11:])
                        if norm:
                            ip_set.add(norm)
                    elif entry.startswith("IP:"):
                        norm = normalize_ip_str(entry[3:])
                        if norm:
                            ip_set.add(norm)

    return CertificateInspection(
        not_before=not_before,
        not_after=not_after,
        dns_names=frozenset(dns_set),
        ip_addresses=frozenset(ip_set),
        subject_cn=subject_cn,
        issuer_cn=issuer_cn,
        is_ca=is_ca,
    )


def verify_ca_key_and_cert_openssl(ca_cert_path: Path, ca_key_path: Path) -> bool:
    try:
        info = inspect_cert_openssl(ca_cert_path)
        if not info.is_ca:
            return False
        cert_pub = run_openssl(["x509", "-in", str(ca_cert_path), "-pubkey", "-noout"]).strip()
        key_pub = run_openssl(["pkey", "-in", str(ca_key_path), "-pubout"]).strip()
        if not cert_pub or cert_pub != key_pub:
            return False
        run_openssl(["verify", "-CAfile", str(ca_cert_path), "-no_check_time", str(ca_cert_path)])
        return True
    except Exception:
        return False


def verify_chain_and_key_openssl(
    ca_cert_path: Path, server_cert_path: Path, server_key_path: Path
) -> bool:
    """无 `cryptography` 时用 `openssl verify -no_check_time` + 公钥比对验签（不依赖系统墙钟）。"""
    try:
        cert_pub = run_openssl(["x509", "-in", str(server_cert_path), "-pubkey", "-noout"]).strip()
        key_pub = run_openssl(["pkey", "-in", str(server_key_path), "-pubout"]).strip()
        if not cert_pub or cert_pub != key_pub:
            return False
        run_openssl(["verify", "-CAfile", str(ca_cert_path), "-no_check_time", str(server_cert_path)])
        return True
    except Exception:
        return False


def _openssl_time_args(custom_now: dt.datetime | None, days: int) -> list[str]:
    if custom_now is None:
        return ["-days", str(days)]
    utc_now = custom_now.astimezone(dt.timezone.utc)
    nb = (utc_now - dt.timedelta(minutes=5)).strftime("%Y%m%d%H%M%SZ")
    na = (utc_now + dt.timedelta(days=days)).strftime("%Y%m%d%H%M%SZ")
    return ["-not_before", nb, "-not_after", na]


def generate_ca_openssl(
    cert_dir: Path,
    ca_cert_path: Path,
    ca_key_path: Path,
    *,
    custom_now: dt.datetime | None = None,
) -> None:
    cert_dir.mkdir(parents=True, exist_ok=True)
    fd_cfg, cfg_name = tempfile.mkstemp(prefix=".ca-openssl.", suffix=".cnf", dir=str(cert_dir))
    fd_key, key_name = tempfile.mkstemp(prefix=".ca-key.", suffix=".tmp", dir=str(cert_dir))
    fd_crt, crt_name = tempfile.mkstemp(prefix=".ca-cert.", suffix=".tmp", dir=str(cert_dir))
    os.close(fd_key)
    os.close(fd_crt)
    cfg_path, tmp_key, tmp_crt = Path(cfg_name), Path(key_name), Path(crt_name)
    try:
        with os.fdopen(fd_cfg, "w", encoding="utf-8") as fh:
            fh.write(
                "[req]\n"
                "distinguished_name = dn\n"
                "x509_extensions = v3_ca\n"
                "prompt = no\n"
                "[dn]\n"
                f"CN = {CA_COMMON_NAME}\n"
                "[v3_ca]\n"
                "basicConstraints = critical, CA:TRUE, pathlen:0\n"
                "keyUsage = critical, digitalSignature, keyCertSign, cRLSign\n"
                "subjectKeyIdentifier = hash\n"
            )
        base_args = [
            "req", "-x509", "-newkey", "rsa:2048", "-sha256", "-nodes",
            "-config", str(cfg_path), "-extensions", "v3_ca",
            "-keyout", str(tmp_key), "-out", str(tmp_crt),
        ]
        try:
            run_openssl([*base_args, *_openssl_time_args(custom_now, DEFAULT_CA_CERT_DAYS)], cwd=cert_dir)
        except RuntimeError:
            if custom_now is None:
                raise
            run_openssl([*base_args, "-days", str(DEFAULT_CA_CERT_DAYS)], cwd=cert_dir)
        atomic_write_bytes(ca_key_path, tmp_key.read_bytes(), 0o600)
        atomic_write_bytes(ca_cert_path, tmp_crt.read_bytes(), 0o644)
    finally:
        for p in (cfg_path, tmp_key, tmp_crt):
            p.unlink(missing_ok=True)


def issue_server_openssl(
    *,
    cert_dir: Path,
    ca_cert_path: Path,
    ca_key_path: Path,
    server_cert_path: Path,
    server_key_path: Path,
    ip_list: list[str],
    days: int,
    custom_now: dt.datetime | None = None,
) -> None:
    cert_dir.mkdir(parents=True, exist_ok=True)
    fd_cfg, cfg_name = tempfile.mkstemp(prefix=".srv-openssl.", suffix=".cnf", dir=str(cert_dir))
    fd_csr, csr_name = tempfile.mkstemp(prefix=".srv-csr.", suffix=".tmp", dir=str(cert_dir))
    fd_crt, crt_name = tempfile.mkstemp(prefix=".srv-cert.", suffix=".tmp", dir=str(cert_dir))
    fd_key, key_name = tempfile.mkstemp(prefix=".srv-key.", suffix=".tmp", dir=str(cert_dir))
    os.close(fd_csr)
    os.close(fd_crt)
    os.close(fd_key)
    cfg_path, csr_path, tmp_crt, tmp_key = Path(cfg_name), Path(csr_name), Path(crt_name), Path(key_name)
    srl_path = cert_dir / ".ca.srl"
    try:
        san_items = ["DNS:localhost", *(f"IP:{ip}" for ip in ip_list)]
        with os.fdopen(fd_cfg, "w", encoding="utf-8") as fh:
            fh.write(
                "[req]\n"
                "distinguished_name = dn\n"
                "prompt = no\n"
                "[dn]\n"
                f"CN = {SERVER_COMMON_NAME}\n"
                "[v3_server]\n"
                "basicConstraints = critical, CA:FALSE\n"
                "keyUsage = critical, digitalSignature, keyEncipherment\n"
                "extendedKeyUsage = serverAuth\n"
                "subjectKeyIdentifier = hash\n"
                "authorityKeyIdentifier = keyid,issuer\n"
                f"subjectAltName = {','.join(san_items)}\n"
            )
        reuse_key = False
        if server_key_path.is_file():
            try:
                run_openssl(["rsa", "-in", str(server_key_path), "-check", "-noout"], cwd=cert_dir)
                reuse_key = True
            except Exception:
                reuse_key = False

        if not reuse_key:
            run_openssl(["genrsa", "-out", str(tmp_key), "2048"], cwd=cert_dir)
            atomic_write_bytes(server_key_path, tmp_key.read_bytes(), 0o600)

        run_openssl(
            ["req", "-new", "-key", str(server_key_path), "-config", str(cfg_path), "-out", str(csr_path)],
            cwd=cert_dir,
        )
        sign_base = [
            "x509", "-req", "-sha256", "-in", str(csr_path),
            "-CA", str(ca_cert_path), "-CAkey", str(ca_key_path),
            "-CAcreateserial", "-CAserial", str(srl_path),
            "-extfile", str(cfg_path), "-extensions", "v3_server",
            "-out", str(tmp_crt),
        ]
        try:
            run_openssl([*sign_base, *_openssl_time_args(custom_now, days)], cwd=cert_dir)
        except RuntimeError:
            if custom_now is None:
                raise
            run_openssl([*sign_base, "-days", str(days)], cwd=cert_dir)
        atomic_write_bytes(server_cert_path, tmp_crt.read_bytes(), 0o644)
    finally:
        for p in (cfg_path, csr_path, tmp_crt, tmp_key, srl_path):
            p.unlink(missing_ok=True)
