"""本地 TLS 证书 `cryptography` 后端（解析、CA 验签、叶证书链密码学验签与签发，无外部进程与墙钟依赖）。"""
from __future__ import annotations

import datetime as dt
import ipaddress
from pathlib import Path

from _atomic_file import atomic_write_bytes
from _platform._certs_backends import (
    CA_COMMON_NAME,
    DEFAULT_CA_CERT_DAYS,
    SERVER_COMMON_NAME,
    CertificateInspection,
    normalize_ip_str,
)


def _public_key_der(pub_key) -> bytes:
    from cryptography.hazmat.primitives import serialization

    return pub_key.public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )


def _verify_signed_by_cert(leaf_cert, issuer_cert) -> None:
    if hasattr(leaf_cert, "verify_directly_issued_by"):
        leaf_cert.verify_directly_issued_by(issuer_cert)
        return
    from cryptography.hazmat.primitives.asymmetric import padding

    if leaf_cert.issuer != issuer_cert.subject:
        raise ValueError("certificate issuer mismatch")
    issuer_cert.public_key().verify(
        leaf_cert.signature,
        leaf_cert.tbs_certificate_bytes,
        padding.PKCS1v15(),
        leaf_cert.signature_hash_algorithm,
    )


def inspect_cert_cryptography(cert_path: Path) -> CertificateInspection:
    from cryptography import x509

    cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
    dns_set: set[str] = set()
    ip_set: set[str] = set()
    try:
        san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
        dns_set = {d.strip().lower() for d in san.get_values_for_type(x509.DNSName)}
        for ip in san.get_values_for_type(x509.IPAddress):
            norm = normalize_ip_str(str(ip))
            if norm:
                ip_set.add(norm)
    except x509.ExtensionNotFound:
        pass
    is_ca = False
    try:
        bc = cert.extensions.get_extension_for_class(x509.BasicConstraints).value
        is_ca = bool(bc.ca)
    except x509.ExtensionNotFound:
        is_ca = False

    sub_cn = cert.subject.get_attributes_for_oid(x509.NameOID.COMMON_NAME)
    iss_cn = cert.issuer.get_attributes_for_oid(x509.NameOID.COMMON_NAME)
    return CertificateInspection(
        not_before=cert.not_valid_before_utc,
        not_after=cert.not_valid_after_utc,
        dns_names=frozenset(dns_set),
        ip_addresses=frozenset(ip_set),
        subject_cn=str(sub_cn[0].value) if sub_cn else "",
        issuer_cn=str(iss_cn[0].value) if iss_cn else "",
        is_ca=is_ca,
    )


def verify_ca_key_and_cert_cryptography(ca_cert_path: Path, ca_key_path: Path) -> bool:
    try:
        from cryptography import x509
        from cryptography.hazmat.primitives import serialization

        ca_cert = x509.load_pem_x509_certificate(ca_cert_path.read_bytes())
        bc = ca_cert.extensions.get_extension_for_class(x509.BasicConstraints).value
        if not bc.ca:
            return False
        ca_key = serialization.load_pem_private_key(ca_key_path.read_bytes(), password=None)
        if _public_key_der(ca_cert.public_key()) != _public_key_der(ca_key.public_key()):
            return False
        _verify_signed_by_cert(ca_cert, ca_cert)
        return True
    except Exception:
        return False


def verify_chain_and_key_cryptography(
    ca_cert_path: Path, server_cert_path: Path, server_key_path: Path
) -> bool:
    """密码学验签：叶证书由 CA 公钥直接签发且叶私钥与叶公钥匹配（不依赖系统墙钟）。"""
    try:
        from cryptography import x509
        from cryptography.hazmat.primitives import serialization

        ca_cert = x509.load_pem_x509_certificate(ca_cert_path.read_bytes())
        bc = ca_cert.extensions.get_extension_for_class(x509.BasicConstraints).value
        if not bc.ca:
            return False
        leaf_cert = x509.load_pem_x509_certificate(server_cert_path.read_bytes())
        leaf_key = serialization.load_pem_private_key(server_key_path.read_bytes(), password=None)
        if _public_key_der(leaf_cert.public_key()) != _public_key_der(leaf_key.public_key()):
            return False
        _verify_signed_by_cert(leaf_cert, ca_cert)
        return True
    except Exception:
        return False


def generate_ca_cryptography(ca_cert_path: Path, ca_key_path: Path, now: dt.datetime) -> None:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = issuer = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, CA_COMMON_NAME)])
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(minutes=5))
        .not_valid_after(now + dt.timedelta(days=DEFAULT_CA_CERT_DAYS))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
        .sign(key, hashes.SHA256())
    )
    atomic_write_bytes(
        ca_key_path,
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        ),
        0o600,
    )
    atomic_write_bytes(ca_cert_path, cert.public_bytes(serialization.Encoding.PEM), 0o644)


def issue_server_cryptography(
    *,
    ca_cert_path: Path,
    ca_key_path: Path,
    server_cert_path: Path,
    server_key_path: Path,
    ip_list: list[str],
    days: int,
    now: dt.datetime,
) -> None:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

    ca_cert = x509.load_pem_x509_certificate(ca_cert_path.read_bytes())
    ca_key = serialization.load_pem_private_key(ca_key_path.read_bytes(), password=None)

    server_key = None
    if server_key_path.is_file():
        try:
            loaded = serialization.load_pem_private_key(server_key_path.read_bytes(), password=None)
            if isinstance(loaded, rsa.RSAPrivateKey) and loaded.key_size >= 2048:
                server_key = loaded
        except Exception:
            server_key = None
    if server_key is None:
        server_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        atomic_write_bytes(
            server_key_path,
            server_key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.TraditionalOpenSSL,
                serialization.NoEncryption(),
            ),
            0o600,
        )

    san_entries: list[x509.GeneralName] = [x509.DNSName("localhost")]
    for ip_text in ip_list:
        san_entries.append(x509.IPAddress(ipaddress.ip_address(ip_text)))

    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, SERVER_COMMON_NAME)])
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(ca_cert.subject)
        .public_key(server_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(minutes=5))
        .not_valid_after(now + dt.timedelta(days=days))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=True,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=False,
                crl_sign=False,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .add_extension(x509.SubjectAlternativeName(san_entries), critical=False)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(server_key.public_key()), critical=False)
        .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False)
        .sign(ca_key, hashes.SHA256())
    )
    atomic_write_bytes(server_cert_path, cert.public_bytes(serialization.Encoding.PEM), 0o644)
