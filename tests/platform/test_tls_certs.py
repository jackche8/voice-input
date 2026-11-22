"""本地 Root CA + `<=397` 天短期 SAN 同步 TLS 证书模块单测。

覆盖：
1. 自动创建长期本地 Root CA (`ca.pem` / `ca-key.pem`) 与 `<=397` 天服务端证书 (`cert.pem` / `key.pem`)；
2. SAN 同时覆盖 `DNS:localhost`、`IP:127.0.0.1`、`IP:::1` 及全部局域网网卡 IPv4，真实 TLS 握手校验通过；
3. 幂等性：未过期且网卡 IP 未变化时不重复重签；
4. 自动替换超过 398 天的旧版 3650 天违规证书（Apple iOS/macOS 规范）；
5. 剩余有效期不足 14 天或已过期时，用同一把 `ca.pem` 自动重签 `cert.pem`；
6. 网卡局域网 IP 变化时，用同一把 `ca.pem` 秒级自动重签 `cert.pem` 补齐新 IP SAN。
"""
from __future__ import annotations

import datetime as dt
import shutil
import ssl
import subprocess
from pathlib import Path

import pytest

from _platform.certs import (
    APPLE_MAX_LEAF_DAYS,
    ensure_tls_certificates,
    inspect_certificate,
)


def _tls_handshake_succeeds(ca_cert: Path, srv_cert: Path, srv_key: Path, hostname: str) -> bool:
    server_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_ctx.load_cert_chain(certfile=str(srv_cert), keyfile=str(srv_key))

    client_ctx = ssl.create_default_context(ssl.Purpose.SERVER_AUTH, cafile=str(ca_cert))
    client_ctx.check_hostname = True
    client_ctx.verify_mode = ssl.CERT_REQUIRED

    c_in, c_out = ssl.MemoryBIO(), ssl.MemoryBIO()
    s_in, s_out = ssl.MemoryBIO(), ssl.MemoryBIO()
    client_ssl = client_ctx.wrap_bio(c_in, c_out, server_side=False, server_hostname=hostname)
    server_ssl = server_ctx.wrap_bio(s_in, s_out, server_side=True)

    c_done = s_done = False
    for _ in range(16):
        if not c_done:
            try:
                client_ssl.do_handshake()
                c_done = True
            except ssl.SSLWantReadError:
                pass
        chunk = c_out.read()
        if chunk:
            s_in.write(chunk)
        if not s_done:
            try:
                server_ssl.do_handshake()
                s_done = True
            except ssl.SSLWantReadError:
                pass
        chunk = s_out.read()
        if chunk:
            c_in.write(chunk)
        if c_done and s_done:
            return True
    return False


def test_generate_ca_and_short_lived_san_cert_with_tls_verification(tmp_path: Path):
    cert_dir = tmp_path / "certs"
    lan_ips = ["192.168.1.25", "10.0.0.42"]

    ca_path, cert_path, key_path = ensure_tls_certificates(
        cert_dir,
        extra_ips=["172.16.5.9"],
        ip_provider=lambda: lan_ips,
    )
    assert ca_path == cert_dir / "ca.pem" and ca_path.is_file()
    assert (cert_dir / "ca-key.pem").is_file()
    assert cert_path == cert_dir / "cert.pem" and cert_path.is_file()
    assert key_path == cert_dir / "key.pem" and key_path.is_file()

    # 不残留任何临时文件
    names = {p.name for p in cert_dir.iterdir()}
    assert names == {"ca.pem", "ca-key.pem", "cert.pem", "key.pem"}

    info = inspect_certificate(cert_path)
    assert 360.0 <= info.total_days <= 397.5
    assert info.total_days <= APPLE_MAX_LEAF_DAYS
    assert "localhost" in info.dns_names
    assert {"127.0.0.1", "::1", "192.168.1.25", "10.0.0.42", "172.16.5.9"} <= set(info.ip_addresses)

    # 真实 TLS 1.3/1.2 握手验证所有主机名与局域网 IP SAN
    for target in ("localhost", "127.0.0.1", "::1", "192.168.1.25", "10.0.0.42", "172.16.5.9"):
        assert _tls_handshake_succeeds(ca_path, cert_path, key_path, target), target

    # 第二次调用（IP 未变、未过期）完全幂等，不重签
    ca_bytes_before = ca_path.read_bytes()
    cert_bytes_before = cert_path.read_bytes()
    ensure_tls_certificates(cert_dir, extra_ips=["172.16.5.9"], ip_provider=lambda: lan_ips)
    assert ca_path.read_bytes() == ca_bytes_before
    assert cert_path.read_bytes() == cert_bytes_before


def test_replaces_legacy_3650_day_violation_certificate(tmp_path: Path):
    openssl = shutil.which("openssl")
    if not openssl:
        pytest.skip("系统无 openssl CLI")
    cert_dir = tmp_path / "legacy_certs"
    cert_dir.mkdir()
    cert_path = cert_dir / "cert.pem"
    key_path = cert_dir / "key.pem"

    # 模拟旧 gen_cert.sh 生成的 3650 天自签证书
    subprocess.run(
        [
            openssl,
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-keyout",
            str(key_path),
            "-out",
            str(cert_path),
            "-days",
            "3650",
            "-subj",
            "/CN=ollama-voice-input",
            "-addext",
            "subjectAltName=DNS:localhost,IP:127.0.0.1",
        ],
        check=True,
        capture_output=True,
    )
    old_info = inspect_certificate(cert_path)
    assert old_info.total_days > 3000

    ca_path, new_cert_path, new_key_path = ensure_tls_certificates(
        cert_dir, ip_provider=lambda: ["192.168.50.2"]
    )
    new_info = inspect_certificate(new_cert_path)
    assert new_info.total_days <= 397.5
    assert "192.168.50.2" in new_info.ip_addresses
    assert "::1" in new_info.ip_addresses
    assert _tls_handshake_succeeds(ca_path, new_cert_path, new_key_path, "192.168.50.2")


def test_lan_ip_change_and_near_expiry_resign_with_same_root_ca(tmp_path: Path):
    cert_dir = tmp_path / "dynamic_certs"
    ca_path, cert_path, key_path = ensure_tls_certificates(
        cert_dir, ip_provider=lambda: ["192.168.1.10"]
    )
    original_ca_bytes = ca_path.read_bytes()
    first_cert_bytes = cert_path.read_bytes()

    # 1. 局域网网卡新增/变更 IP -> 秒级自动用同一把 ca.pem 重签 cert.pem
    ensure_tls_certificates(
        cert_dir, ip_provider=lambda: ["192.168.1.10", "192.168.31.88"]
    )
    assert ca_path.read_bytes() == original_ca_bytes  # 手机已信任的根证书不变！
    second_cert_bytes = cert_path.read_bytes()
    assert second_cert_bytes != first_cert_bytes
    info_after_ip = inspect_certificate(cert_path)
    assert {"192.168.1.10", "192.168.31.88"} <= set(info_after_ip.ip_addresses)
    assert _tls_handshake_succeeds(ca_path, cert_path, key_path, "192.168.31.88")

    # 2. 证书剩余有效期不足 14 天（模拟快进到到期前 10 天）-> 用同一把 ca.pem 自动续签
    near_expiry = info_after_ip.not_after - dt.timedelta(days=10)
    ensure_tls_certificates(
        cert_dir,
        ip_provider=lambda: ["192.168.1.10", "192.168.31.88"],
        now=near_expiry,
    )
    assert ca_path.read_bytes() == original_ca_bytes
    assert cert_path.read_bytes() != second_cert_bytes

    # 3. 非法 >397 天参数必须显式拒绝
    with pytest.raises(ValueError):
        ensure_tls_certificates(cert_dir, server_days=400)


def test_corrupt_or_mismatched_ca_key_and_non_ca_cert_rebuilds_safely(tmp_path: Path):
    """根治回归：ca-key.pem 损坏、与 ca.pem 不匹配或 ca.pem 非 CA:TRUE 时自动重建 CA 与叶证书。"""
    cert_dir = tmp_path / "ca_recovery"
    ca_path, cert_path, key_path = ensure_tls_certificates(
        cert_dir, ip_provider=lambda: ["192.168.10.2"]
    )
    ca_key_path = cert_dir / "ca-key.pem"
    assert inspect_certificate(ca_path).is_ca is True
    assert inspect_certificate(cert_path).is_ca is False

    # 1. ca-key.pem 内容损坏 → 必须自动重建 CA 与叶证书
    orig_ca_bytes = ca_path.read_bytes()
    orig_cert_bytes = cert_path.read_bytes()
    ca_key_path.write_bytes(b"--- CORRUPT CA PRIVATE KEY ---")

    ca2, cert2, key2 = ensure_tls_certificates(
        cert_dir, ip_provider=lambda: ["192.168.10.2"]
    )
    assert ca2.read_bytes() != orig_ca_bytes
    assert cert2.read_bytes() != orig_cert_bytes
    assert inspect_certificate(ca2).is_ca is True
    assert _tls_handshake_succeeds(ca2, cert2, key2, "192.168.10.2")

    # 2. ca-key.pem 被替换成另一套合法但与 ca.pem 公钥不配对的私钥 → 必须重建 CA 与叶证书
    other_dir = tmp_path / "other_ca"
    ensure_tls_certificates(other_dir, ip_provider=lambda: ["192.168.10.2"])
    ca_before_mismatch = ca2.read_bytes()
    ca_key_path.write_bytes((other_dir / "ca-key.pem").read_bytes())

    ca3, cert3, key3 = ensure_tls_certificates(
        cert_dir, ip_provider=lambda: ["192.168.10.2"]
    )
    assert ca3.read_bytes() != ca_before_mismatch
    assert _tls_handshake_succeeds(ca3, cert3, key3, "192.168.10.2")

    # 3. 用叶子证书（CA:FALSE）冒充 ca.pem + 对应私钥 → BasicConstraints 检查拦截并重建
    ca_path.write_bytes(cert3.read_bytes())
    ca_key_path.write_bytes(key3.read_bytes())
    assert inspect_certificate(ca_path).is_ca is False

    ca4, cert4, key4 = ensure_tls_certificates(
        cert_dir, ip_provider=lambda: ["192.168.10.2"]
    )
    assert inspect_certificate(ca4).is_ca is True
    assert _tls_handshake_succeeds(ca4, cert4, key4, "192.168.10.2")


def test_custom_now_is_100_percent_idempotent_without_wall_clock_ssl(tmp_path: Path):
    """根治回归：自定义 `now`（远离系统墙钟）下重复调用 ensure_tls_certificates 必须 100% 幂等不重签。"""
    cert_dir = tmp_path / "custom_now_certs"
    custom_now = dt.datetime(2032, 6, 15, 12, 0, 0, tzinfo=dt.timezone.utc)

    ca_path, cert_path, key_path = ensure_tls_certificates(
        cert_dir,
        ip_provider=lambda: ["192.168.88.9"],
        now=custom_now,
    )
    ca_key_path = cert_dir / "ca-key.pem"
    info = inspect_certificate(cert_path)
    assert abs((info.not_after - custom_now).total_seconds() / 86400.0 - 365.0) < 1.0
    assert info.total_days <= APPLE_MAX_LEAF_DAYS

    ca_b1 = ca_path.read_bytes()
    ca_k1 = ca_key_path.read_bytes()
    cert_b1 = cert_path.read_bytes()
    key_b1 = key_path.read_bytes()

    # 第二次以同一 custom_now 调用：严禁因系统墙钟不匹配而误判并重签
    ensure_tls_certificates(
        cert_dir,
        ip_provider=lambda: ["192.168.88.9"],
        now=custom_now,
    )
    assert ca_path.read_bytes() == ca_b1
    assert ca_key_path.read_bytes() == ca_k1
    assert cert_path.read_bytes() == cert_b1
    assert key_path.read_bytes() == key_b1


def test_certs_module_avoids_private_test_decode_cert():
    import ast

    root = Path(__file__).resolve().parents[2]
    for rel in (
        "_platform/certs.py",
        "_platform/_certs_backends.py",
        "_platform/_certs_crypto.py",
        "_platform/_certs_openssl.py",
    ):
        tree = ast.parse((root / rel).read_text(encoding="utf-8"), filename=rel)
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute):
                assert node.attr != "_test_decode_cert", rel
            elif isinstance(node, ast.Name):
                assert node.id != "_test_decode_cert", rel
            elif isinstance(node, ast.Call):
                for arg in node.args:
                    if isinstance(arg, ast.Constant) and arg.value == "_test_decode_cert":
                        raise AssertionError(f"{rel} 调用私有 _test_decode_cert")


def test_certs_cli_module_runs_cleanly_without_runpy_warning(tmp_path: Path):
    """P2-5 回归：`python -m _platform.certs --cert-dir <dir>` 零 runpy 警告且兼容顶层懒导出。"""
    import sys

    import _platform

    assert _platform.ensure_tls_certificates is ensure_tls_certificates
    assert _platform.APPLE_MAX_LEAF_DAYS == APPLE_MAX_LEAF_DAYS
    assert _platform.inspect_certificate is inspect_certificate

    out_dir = tmp_path / "cli_certs"
    root = Path(__file__).resolve().parents[2]
    proc = subprocess.run(
        [sys.executable, "-W", "error", "-m", "_platform.certs", "--cert-dir", str(out_dir)],
        cwd=str(root),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, f"stdout={proc.stdout}\nstderr={proc.stderr}"
    assert "RuntimeWarning" not in proc.stderr and "runpy" not in proc.stderr
    assert {p.name for p in out_dir.iterdir()} == {"ca.pem", "ca-key.pem", "cert.pem", "key.pem"}
    assert str(out_dir / "ca.pem") in proc.stdout
    assert str(out_dir / "cert.pem") in proc.stdout
    assert str(out_dir / "key.pem") in proc.stdout


def test_discover_lan_ipv4s_windows_ipconfig_flags_and_fullwidth_colon(monkeypatch):
    from _platform import certs as certs_mod
    from _platform import child_process as cp_mod

    which_calls: list[str] = []
    run_calls: list[tuple[list[str], dict]] = []

    def fake_which(name: str) -> str | None:
        which_calls.append(name)
        return f"C:\\Windows\\System32\\{name}.exe"

    def fake_run(cmd, **kw):
        run_calls.append((list(cmd), dict(kw)))
        out = (
            "无线局域网适配器 WLAN:\n"
            "   IPv4 地址 . . . . . . . . . . . . : 192.168.1.33(首选)\n"
            "以太网适配器 以太网:\n"
            "   IPv4 地址 . . . . . . . . . . . . ： 10.0.0.8(首选)\n"
        )
        return subprocess.CompletedProcess(cmd, 0, out, "")

    monkeypatch.setattr(certs_mod.os, "name", "nt")
    monkeypatch.setattr(cp_mod.os, "name", "nt")
    monkeypatch.setattr(certs_mod.socket, "getaddrinfo", lambda *a, **k: [])
    monkeypatch.setattr(certs_mod.socket, "socket", lambda *a, **k: (_ for _ in ()).throw(OSError("offline")))

    ips = certs_mod.discover_lan_ipv4s(run=fake_run, which=fake_which)
    assert ips == ["192.168.1.33", "10.0.0.8"]
    assert which_calls == ["ipconfig"]  # Windows 下不调用 ip / ifconfig
    assert len(run_calls) == 1 and run_calls[0][0] == ["ipconfig"]
    assert run_calls[0][1]["creationflags"] == cp_mod.CREATE_NO_WINDOW
    assert run_calls[0][1]["encoding"] == "oem" and run_calls[0][1]["errors"] == "replace"


def test_run_openssl_passes_windows_subprocess_kwargs(monkeypatch):
    from _platform import _certs_openssl as openssl_mod
    from _platform import child_process as cp_mod

    seen: list[dict] = []

    def fake_run(cmd, **kw):
        seen.append(dict(kw))
        return subprocess.CompletedProcess(cmd, 0, "OpenSSL 3.0\n", "")

    monkeypatch.setattr(openssl_mod.shutil, "which", lambda _n: "C:\\openssl.exe")
    monkeypatch.setattr(openssl_mod.subprocess, "run", fake_run)
    monkeypatch.setattr(cp_mod.os, "name", "nt")
    assert openssl_mod.run_openssl(["version"]) == "OpenSSL 3.0\n"
    assert seen[0]["creationflags"] == cp_mod.CREATE_NO_WINDOW
    assert seen[0]["encoding"] == "utf-8" and seen[0]["errors"] == "replace"
