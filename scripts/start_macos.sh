#!/usr/bin/env bash
# Ollama Voice Input — macOS 启动脚本
# 用法: ./scripts/start_macos.sh                # 正式启动（生产门闩未关时 fail-closed 拒绝）
#       ./scripts/start_macos.sh --acceptance   # 真机验收运行
#
# 自动创建/激活 .venv、同步本地 Root CA + <=397 天 SAN TLS 证书、启动独立 Ollama (127.0.0.1:17947)、
# Qwen3-ASR sidecar (--backend auto，Apple Silicon 自动走 MPS)、Uvicorn HTTPS (17945) 与 PySide6 daemon.py，
# 并在默认浏览器打开方案 C 翻译主页 https://127.0.0.1:17945/。

set -e

DIR="$(cd "$(dirname "$0")/.." && pwd)"
VENV="$DIR/.venv"
LOG_DIR="$DIR/.cache/logs"
CERT_DIR="$DIR/certs"
RUNTIME_DIR="$DIR/.cache/v3-feasibility"
ASR_MODEL_DIR="$RUNTIME_DIR/models/Qwen3-ASR-1.7B"
OLLAMA_MODELS="$RUNTIME_DIR/ollama-models"
HF_HOME="$RUNTIME_DIR/hf-home"
GATED_EXIT=78

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

case "${1:-}" in
    "") MODE=normal ;;
    --acceptance) MODE=acceptance ;;
    *) echo "用法: ./scripts/start_macos.sh [--acceptance]" >&2; exit 2 ;;
esac

port_listening() {
    local port="$1"
    python3 -c 'import socket, sys; s = socket.socket(); s.settimeout(0.3); rc = s.connect_ex(("127.0.0.1", int(sys.argv[1]))); s.close(); sys.exit(0 if rc == 0 else 1)' "$port" 2>/dev/null
}

echo -e "${GREEN}========================================${NC}"
echo -e "${GREEN}  Ollama Voice Input (macOS) 启动脚本${NC}"
echo -e "${GREEN}========================================${NC}"
echo ""

# ---------- 0. 生产门闩（fail-closed）----------
if [ "$MODE" = "normal" ]; then
    if ! env -u OVI_ACCEPTANCE_RUN python3 "$DIR/_activation.py" check >/dev/null; then
        echo -e "${RED}  生产门闩未关闭，未启动任何服务（验收请使用 ./scripts/start_macos.sh --acceptance）。${NC}"
        exit "$GATED_EXIT"
    fi
else
    echo -e "${YELLOW}  验收运行：官方/真机门闩仍未关闭，状态与设置页会如实显示未达项。${NC}"
    export OVI_ACCEPTANCE_RUN=1
    python3 "$DIR/_activation.py" check >/dev/null
fi

mkdir -p "$LOG_DIR" "$CERT_DIR" "$OLLAMA_MODELS" "$HF_HOME"

# ---------- 1. 创建 / 检查虚拟环境与依赖 ----------
if [ ! -x "$VENV/bin/python3" ]; then
    echo -e "${YELLOW}[1/5] 创建 Python 虚拟环境...${NC}"
    python3 -m venv "$VENV"
fi
echo -e "${YELLOW}[1/5] 检查 Python 依赖...${NC}"
"$VENV/bin/pip" install -q -r "$DIR/requirements.txt"

# ---------- 2. 同步本地 Root CA 与 <=397 天全网卡 SAN 证书 ----------
echo -e "${YELLOW}[2/5] 同步本地 TLS 证书 (Root CA + <=397天 SAN)...${NC}"
(
    cd "$DIR"
    "$VENV/bin/python3" -m _platform.certs --cert-dir "$CERT_DIR"
)

# ---------- 3. 启动独立本地 Ollama（127.0.0.1:17947）----------
echo -e "${YELLOW}[3/5] 检查本地 Ollama (127.0.0.1:17947)...${NC}"
if port_listening 17947; then
    echo -e "${GREEN}  本地 Ollama (17947) 已在运行 ✓${NC}"
else
    OLLAMA_BIN="$RUNTIME_DIR/ollama-bin-0.40.16/bin/ollama"
    if [ ! -x "$OLLAMA_BIN" ]; then
        OLLAMA_BIN="$(command -v ollama 2>/dev/null || true)"
    fi
    if [ -n "$OLLAMA_BIN" ]; then
        OLLAMA_HOST="127.0.0.1:17947" OLLAMA_MODELS="$OLLAMA_MODELS" \
            nohup "$OLLAMA_BIN" serve >"$LOG_DIR/ollama.log" 2>&1 &
        echo -e "${GREEN}  本地 Ollama (127.0.0.1:17947) 已启动 ✓${NC}"
    else
        echo -e "${YELLOW}  未检测到 Ollama，请在设置页完成安装或使用 brew install ollama${NC}"
    fi
fi

# ---------- 4. 启动 Qwen3-ASR sidecar（127.0.0.1:17946，--backend auto）----------
echo -e "${YELLOW}[4/5] 检查 Qwen3-ASR sidecar (127.0.0.1:17946)...${NC}"
if port_listening 17946; then
    echo -e "${GREEN}  Qwen3-ASR sidecar (17946) 已在运行 ✓${NC}"
elif [ -f "$ASR_MODEL_DIR/config.json" ]; then
    ASR_PYTHON="$RUNTIME_DIR/venv-asr/bin/python"
    [ -x "$ASR_PYTHON" ] || ASR_PYTHON="$VENV/bin/python3"
    (
        cd "$DIR"
        PYTHONUNBUFFERED=1 HF_HOME="$HF_HOME" HF_HUB_OFFLINE=1 \
            nohup "$ASR_PYTHON" -m runtimes.asr serve \
            --host 127.0.0.1 --port 17946 --backend auto \
            --gpu-memory-utilization 0.45 --max-model-len 4096 \
            --load --warmup --model "$ASR_MODEL_DIR" \
            >"$LOG_DIR/asr.log" 2>&1 &
    )
    echo -e "${GREEN}  Qwen3-ASR sidecar 已启动 ✓（模型加载中，可在设置页查看就绪状态）${NC}"
else
    echo -e "${YELLOW}  Qwen3-ASR 权重未下载，请在设置页完成下载: https://127.0.0.1:17945/setup${NC}"
fi

# ---------- 5. 启动 Uvicorn HTTPS Web 服务 (17945) 与 PySide6 桌面守护进程 ----------
echo -e "${YELLOW}[5/5] 启动 Web 服务 (17945) 与跨平台守护进程...${NC}"
if ! port_listening 17945; then
    (
        cd "$DIR"
        PYTHONUNBUFFERED=1 \
            nohup "$VENV/bin/uvicorn" --factory app:create_production_app \
            --host 0.0.0.0 --port 17945 \
            --ssl-keyfile "$CERT_DIR/key.pem" \
            --ssl-certfile "$CERT_DIR/cert.pem" \
            >"$LOG_DIR/server.log" 2>&1 &
    )
    for _ in $(seq 1 30); do
        port_listening 17945 && break
        sleep 0.3
    done
fi

(
    cd "$DIR"
    PYTHONUNBUFFERED=1 nohup "$VENV/bin/python3" "$DIR/daemon.py" >"$LOG_DIR/daemon.log" 2>&1 &
)

if command -v open >/dev/null 2>&1; then
    open "https://127.0.0.1:17945/" || true
fi

echo ""
echo -e "${GREEN}========================================${NC}"
echo -e "${GREEN}  服务已就绪！${NC}"
echo -e "${GREEN}  翻译主页: https://127.0.0.1:17945/${NC}"
echo -e "${GREEN}  设置页面: https://127.0.0.1:17945/setup${NC}"
echo -e "${GREEN}  Ctrl+Alt+Z   按住说话，松开自动输入${NC}"
echo -e "${GREEN}  Ctrl+Alt+X   截屏原位翻译（红色十字框选）${NC}"
echo -e "${GREEN}  Ctrl+Alt+O   纯 OCR 原位识别（菜单复制）${NC}"
echo -e "${GREEN}  Ctrl+Alt+R   重复输入上一次文本${NC}"
echo -e "${GREEN}  日志目录:   $LOG_DIR${NC}"
echo -e "${GREEN}========================================${NC}"
