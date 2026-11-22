"""U13 Windows / macOS 打包脚本、PyInstaller 单可执行规格、合规文档与 GitHub Actions 发布流水线校验。"""
from __future__ import annotations

import ast
import json
import re
import subprocess
from pathlib import Path
from typing import Any

import pytest

from _paths import PROGRAM_ROOT

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
    assert ast.literal_eval(_kw(exe_call, "icon")) is None

    coll_call = calls["COLLECT"][0]
    assert ast.literal_eval(_kw(coll_call, "upx")) is False
    assert ast.literal_eval(_kw(coll_call, "name")) == "ollama-voice-input"

    bundle_call = calls["BUNDLE"][0]
    assert ast.literal_eval(_kw(bundle_call, "icon")) is None
    assert ast.literal_eval(_kw(bundle_call, "bundle_identifier")) == "io.github.jackche8.ollama-voice-input"

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
        "ollama-voice-input.exe",
        r'Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"',
        'ValueName: "OllamaVoiceInput"',
        "Flags: uninsdeletevalue",
    ):
        assert token in iss_src, f"installer.iss 缺少 {token!r}"
    for forbidden in ("ollama-voice-input-window.exe", "IGNORE_SSL_ERRORS", "PYTHONHTTPSVERIFY", "SSL_CERT_FILE"):
        assert forbidden not in iss_src, f"installer.iss 违规包含 {forbidden!r}"

    # Windows build.ps1 关键步骤
    ps1_src = WIN_BUILD.read_text(encoding="utf-8")
    for token in (
        "APP_VERSION",
        r"packaging\pyinstaller\ollama-voice-input.spec",
        r"dist\ollama-voice-input\ollama-voice-input.exe",
        r"_internal\_asr_runtime\runtimes\asr\server.py",
        '"/DMyAppVersion=$Version"',
        r"packaging\windows\installer.iss",
        "Ollama-Voice-Input-$Version-windows-x64-setup.exe",
    ):
        assert token in ps1_src, f"windows/build.ps1 缺少 {token!r}"


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
    ):
        assert token in notices, f"THIRD-PARTY-NOTICES.md 缺少 {token!r}"

    readme = PKG_README.read_text(encoding="utf-8")
    for token in ("Ubuntu 24.04", "Windows", "macOS", "arm64", "SmartScreen", "xattr -cr"):
        assert token in readme, f"packaging/README.md 缺少 {token!r}"
