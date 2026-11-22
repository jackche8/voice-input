<div align="center">

# 🎙️ Ollama Voice Input

**跨平台本地语音输入 + 原位截屏翻译 + 纯 OCR — 完全离线，零云服务依赖**

按住快捷键说话 → Qwen3-ASR 流式识别 → 本地 `qwen3.5:4b` 只纠错不扩写 → 自动输入到任意应用

[![Version](https://img.shields.io/badge/Version-3.4-blue?style=flat-square)](https://github.com/jackche8/voice-input)
[![Python](https://img.shields.io/badge/Python-3.11+-3776AB?logo=python&logoColor=white)](https://python.org)
[![Ollama](https://img.shields.io/badge/Ollama-0.40.16-000000?logo=ollama)](https://ollama.com)
[![Qwen3-ASR](https://img.shields.io/badge/Qwen3--ASR-1.7B-green)](https://github.com/QwenLM/Qwen3-ASR)
[![Platform](https://img.shields.io/badge/Platform-Linux%20%7C%20Windows%20%7C%20macOS-FCC624)](https://kernel.org)
[![License](https://img.shields.io/badge/License-AGPL--3.0%20%2B%20Commercial-blue.svg)](LICENSE)

</div>

---

## ✨ 核心特性

- 🗣️ **全局流式语音输入 (`Ctrl+Alt+Z`)** — 任意应用中按住说话，屏幕底部居中一体底栏实时显示状态与流式字幕，松开后经 `qwen3.5:4b` 纠错并自动输入到原焦点窗口；支持连续快速录音自动排队与自定义热词提示。
- 📸 **截屏原位翻译 (`Ctrl+Alt+X`)** — 全屏红色十字框选，按行识别原文位置与格式，整段翻译后以半透明贴片逐行排回原位；框下迷你菜单「复制译文 / 复制原文」；识别简体、繁体与英文。
- 🔎 **纯 OCR 原位识别 (`Ctrl+Alt+O`)** — 复用同一框选器与浮窗，跳过 LLM，识别结果按原位置贴回显示。
- 🔁 **重复输入 (`Ctrl+Alt+R`)** — 一键将上一次识别结果重新输入到当前光标窗口。
- 🖥️ **主窗口工作台** — 应用自带主窗口：翻译与图片粘贴 OCR 主页、历史记录、手机局域网扫码语音页与模型/热键设置。
- 🧩 **一个应用管全部** — 应用自己拉起并看护 Ollama、语音识别服务与本地服务端，崩溃自动重启；首次运行在设置页一键装齐运行环境与模型。

---

## ⌨️ 默认快捷键

| 快捷键 | 功能 | 说明 |
|:---:|:---|:---|
| `Ctrl+Alt+Z` | **语音输入** | 按住说话，松开纠错并自动输入（支持热词与连按排队） |
| `Ctrl+Alt+X` | **截屏原位翻译** | 全屏红色十字框选，译文以半透明贴片按原行位置排回 |
| `Ctrl+Alt+O` | **纯 OCR 识别** | 复用全屏红色十字框选，识别文字原位显示，迷你菜单复制 |
| `Ctrl+Alt+R` | **重复输入** | 将上一次识别文本重发到当前焦点窗口 |
| `Esc` | **关闭选区 / 浮窗** | 随时取消框选或关闭原位浮窗（点框外空白处也可关闭） |

快捷键在主窗口「模型与热键设置」中修改（保存在配置根的 `config.json`：`hotkey_voice` / `hotkey_screenshot` / `hotkey_ocr` / `hotkey_repeat`），修改后重启应用生效。

---

## 🚀 安装与运行

### 1. 准备

```bash
git clone https://github.com/jackche8/voice-input.git
cd ollama-voice-input
python3 -m venv --system-site-packages .venv
.venv/bin/pip install -r requirements.txt
```

**Linux** 另需系统包（Debian / Ubuntu 名称）：

- 主窗口：`gir1.2-webkit2-4.1`、`python3-gi`
- OCR：`tesseract-ocr`、`tesseract-ocr-chi-sim`、`tesseract-ocr-chi-tra`、`tesseract-ocr-eng`
- 录音：`libportaudio2`；解压 Ollama 发行包：`zstd`
- 剪贴板与输入：`xclip`、`xdotool`；Wayland 另需 `python3-dbus`、`wl-clipboard`、`wtype`
- 全局快捷键（evdev）与内核级按键模拟（uinput）：把当前用户加入 `input` 组，并为 `/dev/uinput` 加 udev 规则 `KERNEL=="uinput", GROUP="input", MODE="0660"`，重新登录生效

**Windows / macOS** 的主窗口用 `pywebview`（`pip install pywebview`）；macOS 首次使用请按系统提示授予「辅助功能」与「屏幕录制」权限。Windows / macOS 尚未经真机验收。

### 2. 启动应用

```bash
.venv/bin/python daemon.py              # 启动并打开主窗口
.venv/bin/python daemon.py --background # 只在托盘运行，不弹主窗口（开机自启用这个）
```

应用启动后依次拉起并看护三个组件，组件退出会自动重启（退避 2/4/8/16 秒，连续失败 5 次停止并提示）：

| 组件 | 默认地址 | 作用 |
|:---|:---|:---|
| Ollama | `127.0.0.1:17947` | 应用自带的 Ollama（`qwen3.5:4b`），不占用户自装 Ollama 的 `11434` |
| 语音识别服务 | `127.0.0.1:17946` | Qwen3-ASR 1.7B |
| 本地服务端 | `0.0.0.0:17945`（HTTPS） | 主窗口页面与 API；同一局域网的手机可扫码打开语音页 |

- 托盘菜单：打开主界面、模型与热键设置、截屏原位翻译、纯 OCR 识别、显示状态与字幕提示、重试启动本地服务（本地服务端停止时出现）、退出。
- 再次运行 `daemon.py` 不会启动第二份，只会把已运行的主窗口唤到前面。
- 托盘「退出」会结束全部组件。

### 3. 首次运行：一键装齐

打开主窗口的「模型与热键设置」，点「一键装齐」，依次完成五步（失败停在该步，可重试）：

1. 安装 Ollama `0.40.16`（官方发行包，校验后解压到数据根）
2. 等待 Ollama 启动
3. 拉取 `qwen3.5:4b`
4. 安装语音识别运行环境（按显卡自动选 CUDA / CPU / Apple MPS 版本；独立 Python 与带哈希锁定的依赖，CUDA 版约 7 GB）
5. 下载 Qwen3-ASR-1.7B 模型

全部在数据根内完成，不需要管理员权限，不改系统 Python。

### 4. 开机自启

设置页「开机自启」开关（默认开），改动即时生效，启动命令为 `daemon.py --background`：

- Linux：`~/.config/autostart/ollama-voice-input.desktop`
- Windows：当前用户注册表 `HKCU\Software\Microsoft\Windows\CurrentVersion\Run`
- macOS：当前用户 LaunchAgent（`~/Library/LaunchAgents`）

---

## 📂 数据目录

| 系统 | 配置根（配置、令牌、证书） | 数据根（历史、任务、日志、运行环境、模型） |
|:---|:---|:---|
| Linux | `$XDG_CONFIG_HOME/ollama-voice-input`（缺省 `~/.config/ollama-voice-input`） | `$XDG_DATA_HOME/ollama-voice-input`（缺省 `~/.local/share/ollama-voice-input`） |
| Windows | `%APPDATA%\Ollama Voice Input` | `%LOCALAPPDATA%\Ollama Voice Input` |
| macOS | `~/Library/Application Support/Ollama Voice Input` | 同左 |

- 配置根：`config.json`、`web_input_token`（手机局域网访问令牌）、`certs/`（本地 Root CA 与服务端证书）
- 数据根：`history.jsonl`、`jobs.json`、`logs/`、`runtimes/`（Ollama、uv、Python、识别服务运行环境）、`models/`（Qwen3-ASR 权重与 Ollama 模型）
- 程序目录只读，不存任何数据。

证书在每次启动时自动检查与续签（局域网 IP 变化会重签）。需要手动重签时：

```bash
.venv/bin/python -m _platform.certs --cert-dir ~/.config/ollama-voice-input/certs
```

---

## 🔌 端口覆盖

三个组件的端口可以用环境变量改（启动 `daemon.py` 前设置；取值 1–65535，非法值启动即报错）：

| 环境变量 | 默认 | 组件 |
|:---|:---|:---|
| `OVI_PORT_SERVER` | `17945` | 本地服务端（主窗口与手机访问都走它） |
| `OVI_PORT_ASR` | `17946` | 语音识别服务 |
| `OVI_PORT_OLLAMA` | `17947` | 应用自带的 Ollama |

设置 `OVI_PORT_*` 会同步改变全部派生地址——包括服务端地址（主窗口、桌面客户端与手机局域网入口 `https://127.0.0.1:<OVI_PORT_SERVER>`）、语音识别服务地址（`http://127.0.0.1:<OVI_PORT_ASR>`）、应用自带 Ollama 地址（`http://127.0.0.1:<OVI_PORT_OLLAMA>`）以及对应的本地健康探针地址；环境变量会由看护层自动透传给拉起的子进程。

端口被其他程序占用时，应用不会抢占，会在托盘或底栏提示占用者的 PID 与进程名。

---

## 📁 项目结构（`ui → biz → logic`）

```text
ollama-voice-input/
├── daemon.py                # 应用入口：托盘、主窗口、组件看护、快捷键与投递的装配
├── app.py                   # 本地服务端工厂（由应用以 uvicorn 拉起）
├── _paths.py / _ports.py    # 路径表与端口表（唯一来源）
├── _contracts.py            # v1 契约唯一来源（OpenAPI 由代码导出）
├── _supervision/            # 组件规格、看护状态机、进程记录与端口占用查询
├── _desktop/                # 应用业务编排：看护运行器、主窗口启动器、单实例、开机自启、语音/截屏流程
├── _window/                 # 主窗口进程（Linux WebKit2GTK；Windows/macOS pywebview）
├── _ui_desktop/             # 原生纯 UI：红十字框选、原位浮窗、底栏字幕、托盘
├── _v3/ + _v3_app.py        # 服务端路由、鉴权、生产装配、一键装齐任务
├── _platform/ + _linux/     # 三端平台适配与本地 Root CA 证书管理
├── runtimes/asr/            # Qwen3-ASR sidecar 与各变体锁定依赖
├── static/v3/               # 主页（/）、设置页（/setup）、手机录音页（/voice）
└── scripts/gate.py          # 分层测试门禁
```

---

## 🔌 界面入口与 API

- 主窗口：托盘「打开主界面」；页面同样可在浏览器打开 `https://127.0.0.1:17945/`
- 模型与热键设置：`/setup`
- 手机局域网语音录入：`/voice`（设置页扫码，带访问令牌）
- OpenAPI 契约快照：`static/v3/openapi.json`

---

## 📄 License

[AGPL-3.0-only](LICENSE) + [商业授权](COMMERCIAL.md) — 个人、研究、企业内部使用及遵循 AGPL-3.0 的开源分发免费；闭源二次封装或商用需商业授权。第三方依赖声明见 [THIRD-PARTY-NOTICES.md](THIRD-PARTY-NOTICES.md)。
