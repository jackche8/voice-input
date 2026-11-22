# Third-Party Open Source Notices / 第三方开源组件声明

Ollama Voice Input incorporates or interacts with the following third-party open-source software components. Each component remains governed by its respective license.

---

## 1. LGPL Components & Dynamic-Linking Replaceability / LGPL 组件与动态链接可替换说明

Under both the `AGPL-3.0-only` open-source distribution and the Commercial License path, the following LGPL-licensed libraries are **dynamically linked** and kept replaceable by end users (per LGPL-3.0 Section 4(d) / LGPL-2.1 Section 6):

- **Windows / macOS bundles**: Built with PyInstaller `--onedir` and `upx=False`. All LGPL shared libraries (`.dll` / `.dylib`) and Python packages reside in the unpacked `_internal/` directory and can be replaced directly by users with ABI-compatible builds.
- **Linux `.deb` package**: Installs Python dependencies into an independent `/usr/lib/ollama-voice-input/venv` and links system GTK/WebKit libraries dynamically via `python3-gi` / `gir1.2-webkit2-4.1`, allowing standard replacement of LGPL components.

| Component | License | Project URL / Notes |
|---|---|---|
| **PySide6-Essentials** & **shiboken6** (Qt for Python) | LGPL-3.0-only (or GPL-2.0 / GPL-3.0) | <https://doc.qt.io/qtforpython-6/> — Dynamically linked Qt 6 shared libraries |
| **PyAV (`av`)** with bundled **FFmpeg** libraries | BSD-3-Clause (PyAV) / LGPL-2.1-or-later (FFmpeg shared libs in official wheels) | <https://github.com/PyAV-Org/PyAV> / <https://ffmpeg.org/> — Audio decoding (`av.libs/` dynamic libraries replaceable) |
| **pynput** | LGPL-3.0-only | <https://github.com/moses-palmer/pynput> — Cross-platform keyboard/mouse input |
| **python-xlib** (Linux) | LGPL-2.1-or-later | <https://github.com/python-xlib/python-xlib> — X11 protocol binding |
| **WebKit2GTK 4.1** & **GTK 3** (Linux system runtime) | LGPL-2.1-or-later / BSD-2-Clause | <https://webkitgtk.org/> / <https://www.gtk.org/> — Dynamically loaded via system `gir1.2-webkit2-4.1` & `gir1.2-gtk-3.0` |

---

## 2. Permissive Core Dependencies / 核心宽松许可依赖

| Component | License | Project URL |
|---|---|---|
| **FastAPI** | MIT | <https://fastapi.tiangolo.com/> |
| **Starlette** | BSD-3-Clause | <https://www.starlette.io/> |
| **Pydantic** | MIT | <https://docs.pydantic.dev/> |
| **Uvicorn** | BSD-3-Clause | <https://www.uvicorn.org/> |
| **httpx** | BSD-3-Clause | <https://www.python-httpx.org/> |
| **python-multipart** | Apache-2.0 | <https://github.com/Kludex/python-multipart> |
| **cryptography** | Apache-2.0 OR BSD-3-Clause | <https://cryptography.io/> |
| **Pillow** | MIT-CMU | <https://python-pillow.org/> |
| **pytesseract** | Apache-2.0 | <https://github.com/madmaze/pytesseract> |
| **NumPy** | BSD-3-Clause | <https://numpy.org/> |
| **sounddevice** & bundled **PortAudio** | MIT | <https://python-sounddevice.readthedocs.io/> / <https://www.portaudio.com/> |
| **mss** | MIT | <https://github.com/BoboTiG/python-mss> |
| **qrcode** | BSD-3-Clause | <https://github.com/lincolnloop/python-qrcode> |
| **evdev** (Linux) | BSD-3-Clause | <https://github.com/gvalkov/python-evdev> |
| **pywebview** (Windows / macOS) | BSD-3-Clause | <https://pywebview.flowrl.com/> |

---

## 3. External System Tools & On-Demand Downloaded Runtimes / 外部系统工具与按需下载组件

The following components are **not embedded inside this repository or installer binaries**; they are either installed as independent OS packages or downloaded on demand into the user's data directory when the user triggers one-click setup:

| Component | License | Project URL |
|---|---|---|
| **Ollama** | MIT | <https://github.com/ollama/ollama> |
| **uv** | Apache-2.0 OR MIT | <https://github.com/astral-sh/uv> |
| **Qwen3-ASR** (model & runtime) | Apache-2.0 | <https://github.com/QwenLM/Qwen3-ASR> |
| **Tesseract OCR** (Linux system dependency) | Apache-2.0 | <https://github.com/tesseract-ocr/tesseract> |

Full texts of standard licenses (`LGPL-3.0`, `LGPL-2.1`, `Apache-2.0`, `MIT`, `BSD-3-Clause`) are available from <https://opensource.org/licenses/> and <https://www.gnu.org/licenses/> (and on Debian/Ubuntu systems under `/usr/share/common-licenses/`).
