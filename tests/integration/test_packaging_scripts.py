"""U13 Windows / macOS 打包脚本、PyInstaller 单可执行规格、合规文档与 GitHub Actions 发布流水线校验。"""
from __future__ import annotations

import ast
import json
import os
import re
import struct
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from _paths import APP_USER_MODEL_ID, PROGRAM_ROOT, RESOURCES_DIR

ROOT = PROGRAM_ROOT
SPEC_PATH = ROOT / "packaging" / "pyinstaller" / "ollama-voice-input.spec"
WIN_BUILD = ROOT / "packaging" / "windows" / "build.ps1"
WIN_ISS = ROOT / "packaging" / "windows" / "installer.iss"
MAC_BUILD = ROOT / "packaging" / "macos" / "build.sh"
PKG_README = ROOT / "packaging" / "README.md"
WORKFLOW_PATH = ROOT / ".github" / "workflows" / "release.yml"


def _load_yaml(path: Path) -> dict[str, Any]:
    """优先当前解释器 `import yaml`，缺失时经 `/usr/bin/python3` 或 `.venv/bin/python3` 解析为 JSON；均缺失则显式 fail。"""
    try:
        import yaml  # type: ignore[import-not-found]

        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert isinstance(data, dict)
        return data
    except ImportError:
        pass

    helper = (
        "import json, sys, yaml; "
        "raw = yaml.safe_load(open(sys.argv[1], encoding='utf-8')); "
        "print(json.dumps({str(k): v for k, v in raw.items()}, ensure_ascii=False))"
    )
    errors: list[str] = []
    for candidate in (Path("/usr/bin/python3"), ROOT / ".venv" / "bin" / "python3"):
        if not candidate.is_file():
            errors.append(f"{candidate}: not found")
            continue
        proc = subprocess.run([str(candidate), "-c", helper, str(path)], capture_output=True, text=True, timeout=15)
        if proc.returncode == 0:
            return json.loads(proc.stdout)
        errors.append(f"{candidate}: rc={proc.returncode} {proc.stderr.strip()}")
    pytest.fail(f"无法解析 YAML {path}（当前 venv 与系统/项目解释器均不可用 PyYAML）：{'; '.join(errors)}")


def test_pyinstaller_spec_compiles_and_enforces_single_exe_isolated_asr_and_lgpl_onedir():
    src = SPEC_PATH.read_text(encoding="utf-8")
    compile(src, str(SPEC_PATH), "exec")
    tree = ast.parse(src, filename=str(SPEC_PATH))

    calls: dict[str, list[ast.Call]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            calls.setdefault(node.func.id, []).append(node)

    # 单可执行 --onedir：严禁 MERGE，单 Analysis/PYZ/EXE/COLLECT，upx=False，icon=None（不传 .svg）
    assert "MERGE" not in calls
    assert len(calls.get("Analysis", [])) == 1
    assert len(calls.get("EXE", [])) == 1
    assert len(calls.get("COLLECT", [])) == 1
    assert len(calls.get("BUNDLE", [])) == 1

    def _kw(call: ast.Call, name: str) -> ast.AST | None:
        for kw in call.keywords:
            if kw.arg == name:
                return kw.value
        return None

    exe_call = calls["EXE"][0]
    assert ast.literal_eval(_kw(exe_call, "exclude_binaries")) is True
    assert ast.literal_eval(_kw(exe_call, "name")) == "ollama-voice-input"
    assert ast.literal_eval(_kw(exe_call, "console")) is False
    assert ast.literal_eval(_kw(exe_call, "upx")) is False
    exe_icon = _kw(exe_call, "icon")
    assert isinstance(exe_icon, ast.IfExp)
    exe_icon_src = ast.get_source_segment(src, exe_icon) or ""
    assert 'sys.platform == "win32"' in exe_icon_src and "WIN_ICON" in exe_icon_src and ".svg" not in exe_icon_src
    assert ast.literal_eval(exe_icon.orelse) is None
    assert 'WIN_ICON = ROOT / "resources" / "ollama-voice-input.ico"' in src

    coll_call = calls["COLLECT"][0]
    assert ast.literal_eval(_kw(coll_call, "upx")) is False
    assert ast.literal_eval(_kw(coll_call, "name")) == "ollama-voice-input"

    bundle_call = calls["BUNDLE"][0]
    bundle_icon = _kw(bundle_call, "icon")
    bundle_icon_src = ast.get_source_segment(src, bundle_icon) or ""
    assert "MAC_ICON" in bundle_icon_src and ".svg" not in bundle_icon_src
    assert 'MAC_ICON = ROOT / "resources" / "ollama-voice-input.icns"' in src
    assert ast.literal_eval(_kw(bundle_call, "bundle_identifier")) == APP_USER_MODEL_ID

    # collect_all 与按平台守卫的 _sounddevice_data 必须齐全
    for pkg in ("webview", "uvicorn", "av", "cryptography", "pythonnet", "clr_loader", "webview.platforms.cocoa"):
        assert f'"{pkg}"' in src, f"spec 缺少 {pkg}"
    sounddevice_guarded = any(
        isinstance(node, ast.If)
        and 'collect_data_files("_sounddevice_data")' in ast.get_source_segment(src, node)
        and '"win32"' in ast.get_source_segment(src, node)
        and '"darwin"' in ast.get_source_segment(src, node)
        for node in ast.walk(tree)
    )
    assert sounddevice_guarded, "spec 必须仅在 win32/darwin 守卫下收集 _sounddevice_data"

    # 回归守卫（F1）：_v3/production.py 顶层导入 _linux.conflicts / _linux.evdev_keyboards，excludes 严禁排除 "_linux"
    analysis_call = calls["Analysis"][0]
    excludes_node = _kw(analysis_call, "excludes")
    assert excludes_node is not None
    excluded_literals = {
        n.value for n in ast.walk(excludes_node) if isinstance(n, ast.Constant) and isinstance(n.value, str)
    }
    assert "_linux" not in excluded_literals, "Analysis.excludes 严禁排除 _linux"

    # hiddenimports 与 ASR 隔离闭包
    for item in (
        '"app"',
        '"_v3.setup_worker"',
        '"_platform.child_guard"',
        '"_platform.certs"',
        '"_window.__main__"',
        '"_window.pywebview_host"',
        '"python_multipart"',
        '"PySide6.QtDBus"',
        '"entry_daemon.py"',
    ):
        assert item in src, f"spec 缺少 {item}"

    # ASR 闭包 4 文件与 runtimes/asr 唯一落入 _asr_runtime/，严禁放到 _MEIPASS 根 "."
    for closure in ("_ports.py", "_contracts.py", "_audio_codec.py", "_procfs.py"):
        assert f'(str(ROOT / "{closure}"), "_asr_runtime")' in src, f"spec 未将 {closure} 放入 _asr_runtime"
        assert f'(str(ROOT / "{closure}"), ".")' not in src, f"{closure} 不得放在 _MEIPASS 根"
    assert '(str(ROOT / "runtimes" / "asr"), "_asr_runtime/runtimes/asr")' in src
    assert '(str(ROOT / "static" / "v3"), "static/v3")' in src
    assert '(str(ROOT / "_v3" / "asr_download_worker.py"), "_v3")' in src
    assert '(str(ROOT / "resources" / "ollama-voice-input.svg"), "resources")' in src
    assert '(str(ROOT / "resources" / "ollama-voice-input.ico"), "resources")' in src
    for doc in ("LICENSE", "COMMERCIAL.md", "THIRD-PARTY-NOTICES.md"):
        assert f'(str(ROOT / "{doc}"), ".")' in src

    # macOS Info.plist 关键权限与版本键
    for plist_key in (
        "CFBundleIdentifier",
        "CFBundleShortVersionString",
        "CFBundleVersion",
        "NSMicrophoneUsageDescription",
        "NSAppleEventsUsageDescription",
        "NSScreenCaptureUsageDescription",
        "LSMinimumSystemVersion",
        "NSHighResolutionCapable",
    ):
        assert f'"{plist_key}"' in src, f"macOS info_plist 缺少 {plist_key}"


def test_macos_and_windows_packaging_scripts_and_inno_setup():
    # macOS build.sh 语法与关键加固项
    bash_chk = subprocess.run(["bash", "-n", str(MAC_BUILD)], capture_output=True, text=True, timeout=10)
    assert bash_chk.returncode == 0, bash_chk.stderr
    mac_src = MAC_BUILD.read_text(encoding="utf-8")
    for token in (
        "APP_VERSION",
        "packaging/pyinstaller/ollama-voice-input.spec",
        "plutil -lint",
        "Contents/Frameworks/_asr_runtime/runtimes/asr/server.py",
        "Contents/Resources/_asr_runtime/runtimes/asr/server.py",
        "ln -s /Applications",
        "hdiutil create",
        "max_attempts=3",
        "Ollama-Voice-Input-${VERSION}-macos-${ARCH}.dmg",
    ):
        assert token in mac_src, f"macos/build.sh 缺少 {token!r}"

    # Windows installer.iss 关键契约与卸载注册表清理
    iss_src = WIN_ISS.read_text(encoding="utf-8")
    for token in (
        "#ifndef MyAppVersion",
        "PrivilegesRequired=lowest",
        r"DefaultDirName={localappdata}\Programs\Ollama Voice Input",
        "CloseApplications=yes",
        "RestartApplications=no",
        r"LicenseFile=..\..\LICENSE",
        r"InfoBeforeFile=..\..\COMMERCIAL.md",
        "OutputBaseFilename=Ollama-Voice-Input-{#MyAppVersion}-windows-x64-setup",
        r"SetupIconFile=..\..\resources\ollama-voice-input.ico",
        r"UninstallDisplayIcon={app}\{#MyAppExeName}",
        "ollama-voice-input.exe",
        r'Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"',
        'ValueName: "OllamaVoiceInput"',
        "Flags: uninsdeletevalue",
    ):
        assert token in iss_src, f"installer.iss 缺少 {token!r}"
    icon_lines = [line.strip() for line in iss_src.splitlines() if line.strip().startswith('Name: "{auto')]
    assert len(icon_lines) == 2
    for icon_line in icon_lines:
        assert r'IconFilename: "{app}\{#MyAppExeName}"' in icon_line, icon_line
        assert f'AppUserModelID: "{APP_USER_MODEL_ID}"' in icon_line, icon_line
    for forbidden in ("ollama-voice-input-window.exe", "IGNORE_SSL_ERRORS", "PYTHONHTTPSVERIFY", "SSL_CERT_FILE"):
        assert forbidden not in iss_src, f"installer.iss 违规包含 {forbidden!r}"

    # Windows build.ps1 关键步骤（防 Chocolatey shim、DLL 强校验、tessdata_fast 4.1.0 SHA256、含空格去 PATH 双路径冒烟）
    ps1_src = WIN_BUILD.read_text(encoding="utf-8")
    for token in (
        "APP_VERSION",
        r"packaging\pyinstaller\ollama-voice-input.spec",
        r"dist\ollama-voice-input\ollama-voice-input.exe",
        r"_internal\_asr_runtime\runtimes\asr\server.py",
        r"${env:ProgramFiles}\Tesseract-OCR",
        r"${env:ProgramFiles(x86)}\Tesseract-OCR",
        "Get-Command tesseract",
        r"chocolatey\\bin",
        r"dist\ollama-voice-input\_internal\tesseract",
        "tesseract.exe",
        "*.dll",
        "*tesseract*.dll",
        "*lept*.dll",
        r"tessdata\configs",
        "https://github.com/tesseract-ocr/tessdata_fast/raw/4.1.0",
        "7d4322bd2a7749724879683fc3912cb542f19906c83bcc1a52132556427170b2",
        "a5fcb6f0db1e1d6d8522f39db4e848f05984669172e584e8d76b6b3141e1f730",
        "529c5b5797d64b126065cd55f2bb4c7fd7b15790798091b1ff259941a829330b",
        "Get-FileHash",
        "-Algorithm SHA256",
        r".pp\tmp\Ollama Voice Input",
        "Remove-Item Env:TESSDATA_PREFIX",
        "--list-langs",
        r"docs\site\assets\workbench-light.png",
        "_ocr.image_to_text",
        "_ocr_layout.extract_ocr_layout",
        '"/DMyAppVersion=$Version"',
        r"packaging\windows\installer.iss",
        "Ollama-Voice-Input-$Version-windows-x64-setup.exe",
    ):
        assert token in ps1_src, f"windows/build.ps1 缺少 {token!r}"
    # 必须先查 Program Files\Tesseract-OCR 再兜底 Get-Command tesseract，防止误拷贝 Chocolatey shim
    assert ps1_src.index(r"${env:ProgramFiles}\Tesseract-OCR") < ps1_src.index("Get-Command tesseract")
    # 拷贝后强校验 *tesseract*.dll 与 *lept*.dll；PATH 过滤显式剥离 chocolatey\bin
    staged_idx = ps1_src.index(r"dist\ollama-voice-input\_internal\tesseract")
    assert "*tesseract*.dll" in ps1_src[staged_idx:] and "*lept*.dll" in ps1_src[staged_idx:]
    filter_idx = ps1_src.index("$FilteredPath")
    assert r"chocolatey\\bin" in ps1_src[filter_idx : filter_idx + 400]
    # 含空格双路径冒烟（image_to_text + extract_ocr_layout）必须在编 Inno Setup 安装包之前完成
    assert ps1_src.index("_ocr.image_to_text") < ps1_src.index("_ocr_layout.extract_ocr_layout") < ps1_src.index(
        r"packaging\windows\installer.iss"
    )


def test_release_workflow_yaml_enforces_security_runners_checksums_and_tag_only_publish():
    doc = _load_yaml(WORKFLOW_PATH)
    triggers = doc.get("on") or doc.get("True") or doc.get(True)
    assert isinstance(triggers, dict)
    assert triggers["push"]["tags"] == ["v*"]
    assert "workflow_dispatch" in triggers

    # 顶层最小权限 + 仅 publish-release 授予 contents: write
    assert doc.get("permissions") == {"contents": "read"}
    jobs = doc["jobs"]
    assert set(jobs) == {"verify-version", "build-linux", "build-windows", "build-macos", "publish-release"}
    assert jobs["publish-release"].get("permissions") == {"contents": "write"}

    # 防 shell 注入：任何 step.run 不得直接内联 ${{ github.ref_name }} 或 ${{ github.ref_type }}
    for job_name, job in jobs.items():
        for step in job.get("steps", []):
            run_block = step.get("run", "")
            assert not re.search(r"\$\{\{\s*github\.ref_(name|type)\s*\}\}", run_block), (
                f"{job_name} 的 run 块直接内联了 github.ref_name/ref_type"
            )

    # verify-version 经 env 传 REF_NAME / REF_TYPE 并校验 _version.APP_VERSION
    verify_steps = jobs["verify-version"]["steps"]
    check_step = next(s for s in verify_steps if s.get("id") == "check")
    assert check_step["env"]["REF_NAME"] == "${{ github.ref_name }}"
    assert check_step["env"]["REF_TYPE"] == "${{ github.ref_type }}"
    assert "APP_VERSION" in check_step["run"] and 'ref_type == "tag"' in check_step["run"]

    # build-linux：ubuntu-24.04 + 系统 /usr/bin/python3（绝不用 setup-python）+ dash -n + lintian
    linux_job = jobs["build-linux"]
    assert linux_job["runs-on"] == "ubuntu-24.04"
    linux_uses = [s.get("uses", "") for s in linux_job["steps"]]
    assert not any("setup-python" in u for u in linux_uses), "build-linux 不得使用 actions/setup-python"
    linux_runs = "\n".join(s.get("run", "") for s in linux_job["steps"])
    for token in (
        "dpkg-dev lintian python3-venv python3-pip python3-dev build-essential",
        "dash -n",
        "/usr/bin/python3 packaging/linux/build_deb.py",
        "lintian --fail-on error",
        "ollama-voice-input_${APP_VERSION}_amd64.deb",
    ):
        assert token in linux_runs, f"build-linux 缺少 {token!r}"

    # build-windows & build-macos：分别固定 windows-latest / macos-15 与 Python 3.12
    for job_name, runner in (("build-windows", "windows-latest"), ("build-macos", "macos-15")):
        job = jobs[job_name]
        assert job["runs-on"] == runner
        py_step = next(s for s in job["steps"] if "actions/setup-python@v5" in s.get("uses", ""))
        assert str(py_step["with"]["python-version"]) == "3.12"
    win_runs = "\n".join(s.get("run", "") for s in jobs["build-windows"]["steps"])
    assert "choco install -y tesseract" in win_runs, "build-windows 缺少安装 Tesseract 5 步骤"
    assert win_runs.index("choco install -y tesseract") < win_runs.index(r"packaging\windows\build.ps1")

    # publish-release：依赖全部 4 任务、basename SHA256SUMS.txt、仅 tag 发 softprops/action-gh-release@v2
    pub = jobs["publish-release"]
    assert set(pub["needs"]) == {"verify-version", "build-linux", "build-windows", "build-macos"}
    pub_runs = "\n".join(s.get("run", "") for s in pub["steps"])
    for token in (
        "cd release-assets",
        "sha256sum",
        "SHA256SUMS.txt",
        "ollama-voice-input_${APP_VERSION}_amd64.deb",
        "Ollama-Voice-Input-${APP_VERSION}-windows-x64-setup.exe",
        "Ollama-Voice-Input-${APP_VERSION}-macos-arm64.dmg",
    ):
        assert token in pub_runs, f"publish-release 缺少 {token!r}"
    gh_step = next(s for s in pub["steps"] if s.get("uses") == "softprops/action-gh-release@v2")
    assert gh_step.get("if") == "github.ref_type == 'tag'"


def test_dual_license_notices_and_packaging_readme_are_complete():
    lic = (ROOT / "LICENSE").read_text(encoding="utf-8")
    first_lines = [line.strip() for line in lic.splitlines()[:3]]
    assert first_lines[0] == "GNU AFFERO GENERAL PUBLIC LICENSE"
    assert first_lines[1] == "Version 3, 19 November 2007"
    assert "END OF TERMS AND CONDITIONS" in lic and len(lic) > 30_000

    comm = (ROOT / "COMMERCIAL.md").read_text(encoding="utf-8")
    for token in ("AGPL-3.0-only", "jackche8@users.noreply.github.com", "https://github.com/jackche8/voice-input/issues"):
        assert token in comm

    notices = (ROOT / "THIRD-PARTY-NOTICES.md").read_text(encoding="utf-8")
    for token in (
        "PySide6-Essentials",
        "PyAV",
        "FFmpeg",
        "WebKit2GTK",
        "sounddevice",
        "cryptography",
        "pywebview",
        "pynput",
        "python-xlib",
        "Ollama",
        "Qwen3-ASR",
        "--onedir",
        "Tesseract OCR 5",
        "Leptonica",
        "BSD-2-Clause",
        "tessdata_fast 4.1.0",
        "Apache-2.0",
        "_internal/tesseract/",
        "Linux system dependency",
    ):
        assert token in notices, f"THIRD-PARTY-NOTICES.md 缺少 {token!r}"

    readme = PKG_README.read_text(encoding="utf-8")
    for token in ("Ubuntu 24.04", "Windows", "macOS", "arm64", "SmartScreen", "xattr -cr"):
        assert token in readme, f"packaging/README.md 缺少 {token!r}"


def test_single_source_icons_match_svg_and_binary_layouts():
    """单源 SVG → .ico/.icns 校验：build_icons.py --check 逐字节一致，且 ICO 8 尺寸与 ICNS 11 块布局合规。"""
    chk = subprocess.run(
        [sys.executable, str(ROOT / "packaging" / "build_icons.py"), "--check"],
        cwd=str(ROOT),
        env={**os.environ, "QT_QPA_PLATFORM": "offscreen"},
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert chk.returncode == 0, chk.stdout + chk.stderr

    ico = (RESOURCES_DIR / "ollama-voice-input.ico").read_bytes()
    res, typ, count = struct.unpack_from("<HHH", ico, 0)
    assert (res, typ, count) == (0, 1, 8)
    found_ico_sizes: list[int] = []
    for i in range(count):
        w, h, _cc, _r, planes, bpp, payload_len, offset = struct.unpack_from("<BBBBHHII", ico, 6 + i * 16)
        dim = 256 if w == 0 else w
        assert h == w and planes == 1 and bpp == 32
        found_ico_sizes.append(dim)
        payload = ico[offset : offset + payload_len]
        if dim == 256:
            assert payload.startswith(b"\x89PNG\r\n\x1a\n")
            assert struct.unpack_from(">II", payload, 16) == (256, 256)
        else:
            hdr = struct.unpack_from("<IiiHH", payload, 0)
            assert hdr == (40, dim, dim * 2, 1, 32)
            and_stride = ((dim + 31) // 32) * 4
            assert len(payload) == 40 + dim * dim * 4 + dim * and_stride
    assert found_ico_sizes == [16, 20, 24, 32, 48, 64, 128, 256]

    icns = (RESOURCES_DIR / "ollama-voice-input.icns").read_bytes()
    magic, total_len = struct.unpack_from(">4sI", icns, 0)
    assert magic == b"icns" and total_len == len(icns)
    pos = 8
    found_chunks: list[tuple[bytes, int, int]] = []
    while pos < len(icns):
        tag, chunk_len = struct.unpack_from(">4sI", icns, pos)
        data = icns[pos + 8 : pos + chunk_len]
        assert data.startswith(b"\x89PNG\r\n\x1a\n")
        w, h = struct.unpack_from(">II", data, 16)
        found_chunks.append((tag, w, h))
        pos += chunk_len
    assert pos == len(icns)
    assert found_chunks == [
        (b"icp4", 16, 16),
        (b"icp5", 32, 32),
        (b"icp6", 64, 64),
        (b"ic07", 128, 128),
        (b"ic08", 256, 256),
        (b"ic09", 512, 512),
        (b"ic10", 1024, 1024),
        (b"ic11", 32, 32),
        (b"ic12", 64, 64),
        (b"ic13", 256, 256),
        (b"ic14", 512, 512),
    ]
