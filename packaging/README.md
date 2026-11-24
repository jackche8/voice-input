# Packaging & Release Guide / 三端打包与发布说明

## 1. Supported Release Targets / 支持的目标平台

| Platform | Target / Runner | Artifact Name (`dist/`) | Status |
|---|---|---|---|
| **Linux (`.deb`)** | Ubuntu 24.04 amd64 (system Python 3.12; local builds adapt dynamically to build Python minor) | `ollama-voice-input_<ver>_amd64.deb` | Locally tested + CI `lintian --fail-on error` |
| **Windows (`.exe`)** | Windows x64 (`windows-latest`, Python 3.12, Inno Setup 6) | `Ollama-Voice-Input-<ver>-windows-x64-setup.exe` | CI automated build + contract tests; **no real Windows hardware QA yet** |
| **macOS (`.dmg`)** | macOS arm64 / Apple Silicon (`macos-15`, Python 3.12) | `Ollama-Voice-Input-<ver>-macos-arm64.dmg` | CI automated build + contract tests; **no real macOS hardware QA yet** |

> **Version Single Source / 唯一版本号来源**：All packagers and `.github/workflows/release.yml` read `_version.APP_VERSION` directly.

---

## 2. Local Build Commands / 本地构建命令

### Linux (`.deb`)
```bash
# Full build (builds offline wheelhouse via pip wheel for current Python minor)
python3 packaging/linux/build_deb.py --output-dir dist

# Offline build using a pre-populated wheelhouse directory
python3 packaging/linux/build_deb.py --output-dir dist --wheelhouse-dir /path/to/wheels --python-minor 3.12

# Verify package & lintian
dpkg-deb --info dist/ollama-voice-input_*_amd64.deb
dpkg-deb --contents dist/ollama-voice-input_*_amd64.deb
lintian --fail-on error dist/ollama-voice-input_*_amd64.deb
```
Install on Ubuntu:
```bash
sudo apt install ./dist/ollama-voice-input_<ver>_amd64.deb
```
Upgrading or removing stops running instances cleanly via `/usr/lib/ollama-voice-input/quit_running.py` and preserves all user data under `~/.config/ollama-voice-input` and `~/.local/share/ollama-voice-input`.

### Windows (`x64` Inno Setup)
Requires Python 3.12, `pip install -r requirements.txt pyinstaller`, and Inno Setup 6 (`ISCC.exe`):
```powershell
pwsh -File packaging\windows\build.ps1
```
Installs per-user into `%LOCALAPPDATA%\Programs\Ollama Voice Input` without administrator privileges.

### macOS (`arm64` `.app` + `.dmg`)
Requires Python 3.12 and `pip install -r requirements.txt pyinstaller`:
```bash
bash packaging/macos/build.sh
```

### Automated Three-Platform Release (GitHub Actions)
- **Dry-run build (no Release published)**: Trigger **Actions → Release → Run workflow (`workflow_dispatch`)** to build all 3 platforms and download the `release-bundle` artifact (including `SHA256SUMS.txt`).
- **Publish Release**: Push a tag matching `v<_version.APP_VERSION>`, e.g.:
  ```bash
  git tag v3.4.7
  git push origin v3.4.7
  ```

---

## 3. Unsigned Installer First-Open Instructions / 未签名安装包首次打开指引

The Windows `.exe` and macOS `.dmg` installers produced by public GitHub Actions are **currently unsigned / unnotarized** (no paid EV code-signing certificate or Apple Developer ID yet). Always verify downloads against `SHA256SUMS.txt` first.

### Windows SmartScreen
- **中文**：首次运行安装器或应用时，如弹出「Windows 已保护你的电脑（SmartScreen）」，请点击 **「更多信息」→「仍要运行」**。应用界面内嵌 WebView2 仅校验并放行 `127.0.0.1` 的本机自签证书，不设任何全局 SSL 忽略。
- **English**: If Windows Defender SmartScreen shows *"Windows protected your PC"*, click **More info → Run anyway**. The embedded WebView2 host trusts only the app's own local certificate on `127.0.0.1` (no global TLS bypass).

### macOS Gatekeeper & Permissions
- **中文**：
  1. 打开 `.dmg`，将 `Ollama Voice Input.app` 拖入 `Applications`（应用程序）。
  2. 首次打开如提示「无法验证开发者 / 已损坏」，请在终端执行清除隔离属性：
     ```bash
     xattr -cr "/Applications/Ollama Voice Input.app"
     ```
     或右键点击应用选择 **「打开」**，或在 **系统设置 → 隐私与安全性** 底部点击 **「仍要打开」**。
  3. 按系统提示授予 **麦克风**、**辅助功能** 与 **屏幕录制** 权限。
- **English**:
  1. Open the `.dmg` and drag `Ollama Voice Input.app` into `Applications`.
  2. Because the app is ad-hoc signed and unnotarized, remove the quarantine attribute on first launch:
     ```bash
     xattr -cr "/Applications/Ollama Voice Input.app"
     ```
     or Control-click the app → **Open** (or **System Settings → Privacy & Security → Open Anyway**).
  3. Grant **Microphone**, **Accessibility**, and **Screen Recording** permissions when prompted.

---

## 4. Licensing & LGPL Replaceability / 许可与 LGPL 合规说明

- **Dual License**: `AGPL-3.0-only` (`LICENSE`) + Commercial License (`COMMERCIAL.md`).
- **LGPL Dynamic Linking**: Windows/macOS bundles use PyInstaller `--onedir` (`upx=False`) and Linux `.deb` uses an independent `/usr/lib/ollama-voice-input/venv` so LGPL libraries (`PySide6-Essentials`, `PyAV/FFmpeg`, `pynput`, `python-xlib`, `WebKit2GTK`) remain dynamically linked and replaceable. See `THIRD-PARTY-NOTICES.md`.
