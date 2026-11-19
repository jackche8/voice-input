#!/bin/bash
# 同步本地 Root CA（ca.pem）与 <=397 天全网卡 SAN 服务端证书（cert.pem / key.pem）。
# 单一来源：_platform.certs.ensure_tls_certificates（支持局域网 IP 变化自动重签与旧证书替换）。
set -e

DIR="$(cd "$(dirname "$0")" && pwd)"
CERT_DIR="$DIR/certs"
PYTHON_BIN="$DIR/.venv/bin/python3"
if [ ! -x "$PYTHON_BIN" ]; then
    PYTHON_BIN="$(command -v python3)"
fi

cd "$DIR"
"$PYTHON_BIN" -m _platform.certs --cert-dir "$CERT_DIR"
