<div align="center">

# 🎙️ Ollama Voice Input

**跨平台本地语音输入 + 原位截屏翻译 + 纯 OCR — 完全离线，零云服务依赖**

按住快捷键说话 → Qwen3-ASR 流式识别 → 本地 `qwen3.5:4b` 只纠错不扩写 → 自动输入到任意应用

[![Version](https://img.shields.io/badge/Version-3.2-blue?style=flat-square)](https://github.com/jackche8/voice-input)
[![Python](https://img.shields.io/badge/Python-3.10+-3776AB?logo=python&logoColor=white)](https://python.org)
[![Ollama](https://img.shields.io/badge/Ollama-127.0.0.1:17947-000000?logo=ollama)](https://ollama.com)
[![Qwen3-ASR](https://img.shields.io/badge/Qwen3--ASR-1.7B-green)](https://github.com/QwenLM/Qwen3-ASR)
[![Platform](https://img.shields.io/badge/Platform-Linux%20%7C%20Windows%20%7C%20macOS-FCC624)](https://kernel.org)
[![License](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

</div>

---

## ✨ 核心特性

- 🗣️ **全局流式语音输入 (`Ctrl+Alt+Z`)** — 任意应用中按住说话，屏幕底部居中双行大字幕实时流式显示，松开后经 `qwen3.5:4b` 纠错并自动输入到原焦点窗口；支持连续快速录音自动排队（杜绝 `409 Conflict`）与自定义热词提示。
- 📸 **截屏原位背景遮挡翻译 (`Ctrl+Alt+X`)** — 跨平台全屏红色十字定位框选器（`PySide6-Essentials`），框选松手后提取每个文字块坐标与采样背景色，原位 100% 不透明遮挡原文并排版译文，支持「按住对比原图 / 复制译文 / 复制原文 / Esc 关闭」。
- 🔎 **纯 OCR 识别自动复制 (`Ctrl+Alt+O`)** — 100% 复用同一红色十字定位框选器，跳过 LLM 秒级提取中英文文字并自动写入剪贴板，左下角胶囊即时提示字数。
- 🔁 **重复输入 (`Ctrl+Alt+R`)** — 一键将上一次识别结果重新输入到当前光标窗口。
- 🖥️ **方案 C Studio 翻译工作台 (`/`)** — 访问 `https://127.0.0.1:17945/` 直达侧栏翻译与图片粘贴 OCR 主页；历史记录、手机局域网扫码语音页（`/voice`）与模型/热键设置（`/setup`）统一收于侧栏。
- 🔒 **完全本地 & 三端架构** — `PySide6-Essentials` 原生桌面壳 + `_platform`（Linux / Windows / macOS）三端适配器 + 本地长期 Root CA 与 `<=397` 天全网卡 SAN 证书。

---

## ⌨️ 默认快捷键

| 快捷键 | 功能 | 说明 |
|:---:|:---|:---|
| `Ctrl+Alt+Z` | **语音输入** | 按住说话，松开纠错并自动输入（支持热词与连按排队） |
| `Ctrl+Alt+X` | **截屏原位翻译** | 全屏红色十字框选，识别位置采样背景色不透明遮挡渲染译文 |
| `Ctrl+Alt+O` | **纯 OCR 识别** | 复用全屏红色十字框选，秒级提取文字自动复制剪贴板 |
| `Ctrl+Alt+R` | **重复输入** | 将上一次识别文本重发到当前焦点窗口 |
| `Esc` | **关闭选区 / 译文浮窗** | 随时取消框选或关闭原位翻译浮窗 |

快捷键均可在 `https://127.0.0.1:17945/setup` 或 `.config.json`（`hotkey_voice` / `hotkey_screenshot` / `hotkey_ocr` / `hotkey_repeat`）中自定义。

---

## 🚀 三端启动方式

### 1. Linux（Wayland / X11 · systemd 用户服务）

```bash
git clone https://github.com/jackche8/voice-input.git
cd ollama-voice-input
chmod +x install.sh start.sh restart.sh gen_cert.sh
./install.sh
./start.sh               # 正式启动（验收期使用 ./start.sh --acceptance）
```

`install.sh` 经 `_v3/units.py` 渲染 4 个用户级服务：

| 服务 | 绑定地址 | 作用 |
|:---|:---|:---|
| `ollama-voice-server` | `0.0.0.0:17945` (HTTPS) | FastAPI v1 API + 方案 C 翻译工作台（`/`、`/setup`、`/voice`） |
| `ollama-voice-daemon` | 本机桌面会话 | PySide6 跨平台守护进程：四热键、红十字框选、原位翻译、大字幕、托盘、投递 |
| `ollama-voice-asr` | `127.0.0.1:17946` | Qwen3-ASR 1.7B sidecar（`--backend auto --gpu-memory-utilization 0.45 --max-model-len 4096`） |
| `ollama-voice-ollama` | `127.0.0.1:17947` | 独立项目级 Ollama（`qwen3.5:4b`，不与系统 `:11434` 冲突） |

### 2. Windows（PowerShell）

```powershell
.\scripts\start.ps1                # 正式启动（验收期加 -Acceptance）
```

脚本自动创建 `.venv`、同步 Root CA 与 `<=397` 天 SAN 证书、按序启动独立 Ollama（`17947`）、ASR sidecar（`--backend auto`，`17946`）、HTTPS Web 服务（`17945`）与 PySide6 守护进程，并打开 `https://127.0.0.1:17945/`。

### 3. macOS（Bash / Zsh）

```bash
chmod +x scripts/start_macos.sh
./scripts/start_macos.sh           # 正式启动（验收期加 --acceptance）
```

Apple Silicon 自动经 `--backend auto` 启用 `Transformers (MPS)` 推理；首次使用请按系统提示授予「辅助功能」与「屏幕录制」权限。

---

## 📁 项目结构（`ui → biz → logic`）

```text
ollama-voice-input/
├── app.py                   # 薄入口：uvicorn --factory app:create_production_app
├── daemon.py                # 薄入口：PySide6 跨平台守护进程装配（零 GTK/WebKit）
├── _contracts.py            # v1 契约唯一来源（OpenAPI 由代码自动导出）
├── _v3/ + _v3_app.py        # 服务端路由、鉴权、生产装配与 systemd 单元渲染
├── _ui_desktop/             # 原生跨平台纯 UI：红十字框选、原位遮挡浮窗、胶囊/双行大字幕、托盘
├── _desktop/                # 桌面业务编排：CaptureFlow、VoiceFlow 排队上传、DeliveryLoop、SSE 重连
├── _platform/ + _linux/     # 三端平台适配（Linux / Windows / macOS）与本地 Root CA 证书管理
├── runtimes/asr/            # Qwen3-ASR 双后端 sidecar（vLLM + Transformers CUDA/MPS/CPU）
├── static/v3/               # 方案 C 翻译主页（/）、设置页（/setup）、手机录音页（/voice）
├── systemd/                 # 4 个 Linux systemd 用户服务模板
└── scripts/                 # 分层测试门禁 gate.py、Windows start.ps1、macOS start_macos.sh
```

---

## 🔌 API 与界面入口

- 翻译与图片 OCR 工作台：`https://127.0.0.1:17945/`
- 模型安装、热词与热键设置：`https://127.0.0.1:17945/setup`
- 手机局域网语音录入：`https://127.0.0.1:17945/voice`（设置页一键扫码）
- OpenAPI 契约快照：`static/v3/openapi.json`（含 `/api/v1/recognize/sessions`、`/api/v1/utterances`、`/api/v1/jobs/*`、`/api/v1/ocr-inplace`、`/api/v1/ocr-translate`、`/api/v1/translate`、`/api/v1/polish`、`/api/v1/events` 等）

---

## 📄 License

[MIT](LICENSE) — 自由使用、修改和分发。
