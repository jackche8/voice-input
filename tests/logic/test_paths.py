"""U1 路径唯一表：三平台根目录与派生路径、非 live 拒绝真实根、应用启动命令（只注入 environ / os_name，不碰真实目录）。"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from _contracts import OLLAMA_VERSION
from _paths import APP_DISPLAY_NAME, APP_ID, PROGRAM_ROOT, AppPaths, app_command, default_app_paths, pictures_dir

LINUX_ENV = {
    "HOME": "/home/u",
    "XDG_CONFIG_HOME": "/home/u/.cfg",
    "XDG_DATA_HOME": "/home/u/.dat",
    "XDG_RUNTIME_DIR": "/run/user/1000",
}


def test_linux_roots_follow_xdg_and_fall_back_when_unset_or_relative():
    p = default_app_paths(LINUX_ENV, os_name="linux")
    assert (p.config_root, p.data_root, p.runtime_dir) == (
        Path("/home/u/.cfg") / APP_ID, Path("/home/u/.dat") / APP_ID, Path("/run/user/1000"))
    assert p.live and p.program_root == PROGRAM_ROOT
    bare = default_app_paths({"HOME": "/home/u", "XDG_CONFIG_HOME": "rel", "XDG_RUNTIME_DIR": ""}, os_name="linux")
    assert bare.config_root == Path("/home/u/.config") / APP_ID
    assert bare.data_root == Path("/home/u/.local/share") / APP_ID
    assert bare.runtime_dir == bare.data_root / "run"
    with pytest.raises(RuntimeError, match="HOME"):
        default_app_paths({}, os_name="linux")


def test_windows_and_macos_roots():
    # 宿主是 Linux，用 POSIX 绝对路径代表 %APPDATA% / %LOCALAPPDATA%；只验变量到根目录的映射
    win = default_app_paths({"APPDATA": "/w/Roaming", "LOCALAPPDATA": "/w/Local"}, os_name="windows")
    assert win.config_root == Path("/w/Roaming") / APP_DISPLAY_NAME
    assert win.data_root == Path("/w/Local") / APP_DISPLAY_NAME
    assert win.runtime_dir == win.data_root / "run"
    assert win.ollama_binary == win.ollama_install_dir / "ollama.exe"
    assert win.asr_python == win.asr_venv / "Scripts" / "python.exe"
    with pytest.raises(RuntimeError, match="LOCALAPPDATA"):
        default_app_paths({"APPDATA": "/w/Roaming"}, os_name="windows")
    mac = default_app_paths({"HOME": "/Users/u"}, os_name="macos")
    assert mac.config_root == mac.data_root == Path("/Users/u/Library/Application Support") / APP_DISPLAY_NAME
    assert mac.runtime_dir == mac.data_root / "run"
    assert mac.ollama_binary == mac.ollama_install_dir / "Ollama.app" / "Contents" / "Resources" / "ollama"
    assert mac.asr_python == mac.asr_venv / "bin" / "python"


def test_every_derived_path_hangs_off_its_root():
    p = default_app_paths(LINUX_ENV, os_name="linux")
    c, d = p.config_root, p.data_root
    assert (p.config, p.token) == (c / "config.json", c / "web_input_token")
    assert (p.cert, p.key, p.ca) == (c / "certs" / "cert.pem", c / "certs" / "key.pem", c / "certs" / "ca.pem")
    assert (p.history, p.jobs, p.upload_tmp, p.logs) == (d / "history.jsonl", d / "jobs.json", d / "upload-tmp", d / "logs")
    assert p.temp_dir == d / "tmp"
    assert (p.children_record, p.setup_tasks) == (d / "run" / "children.json", d / "setup" / "tasks")
    assert p.ollama_binary == d / "runtimes" / "ollama" / OLLAMA_VERSION / "bin" / "ollama"
    assert (p.ollama_models, p.asr_model_dir, p.hf_home) == (d / "models" / "ollama", d / "models" / "Qwen3-ASR-1.7B", d / "hf-home")
    assert (p.uv_dir, p.python_dir) == (d / "runtimes" / "uv", d / "runtimes" / "python")
    assert (p.asr_python, p.asr_manifest) == (d / "runtimes/asr/venv/bin/python", d / "runtimes/asr/manifest.json")
    assert p.lock == Path("/run/user/1000") / f"{APP_ID}.lock"


def test_non_live_paths_refuse_anything_under_the_real_roots(tmp_path):
    env = {"HOME": str(tmp_path / "home"), "XDG_RUNTIME_DIR": str(tmp_path / "xdg-run")}
    live = default_app_paths(env, os_name="linux")
    safe = {"config_root": tmp_path / "c", "data_root": tmp_path / "d", "runtime_dir": tmp_path / "r"}
    ok = AppPaths.for_roots(**safe, os_name="linux", environ=env)
    assert not ok.live and ok.config == tmp_path / "c" / "config.json"
    for key, live_root in (("config_root", live.config_root), ("data_root", live.data_root / "sub"),
                           ("runtime_dir", live.runtime_dir / "x")):
        with pytest.raises(RuntimeError, match="真实根之下"):
            AppPaths.for_roots(**{**safe, key: live_root}, os_name="linux", environ=env)


def test_app_command_runs_program_root_entry_with_current_interpreter():
    p = default_app_paths(LINUX_ENV, os_name="linux")
    entry = str(PROGRAM_ROOT / "daemon.py")
    assert app_command(p, background=False) == [sys.executable, entry]
    assert app_command(p, background=True) == [sys.executable, entry, "--background"]


def test_pictures_dir_across_linux_windows_and_macos():
    seen: list[tuple[list[str], dict[str, str]]] = []

    def ok_run(cmd, **kw):
        seen.append((list(cmd), dict(kw.get("env") or {})))
        return subprocess.CompletedProcess(cmd, 0, "/home/u/图片\n", "")

    linux_ok = pictures_dir(LINUX_ENV, os_name="linux", run=ok_run)
    assert linux_ok == Path("/home/u/图片")
    assert linux_ok / APP_DISPLAY_NAME == Path("/home/u/图片") / "Ollama Voice Input"
    assert seen == [(["xdg-user-dir", "PICTURES"], dict(LINUX_ENV))]

    # Linux xdg-user-dir 失败、缺失命令、空输出、相对路径均回退 HOME/Pictures
    def rc_fail(cmd, **kw):
        del kw
        return subprocess.CompletedProcess(cmd, 1, "", "err")

    def missing_cmd(cmd, **kw):
        del cmd, kw
        raise FileNotFoundError("xdg-user-dir")

    def relative_out(cmd, **kw):
        del kw
        return subprocess.CompletedProcess(cmd, 0, "Pictures\n", "")

    def empty_out(cmd, **kw):
        del kw
        return subprocess.CompletedProcess(cmd, 0, "  \n", "")

    for bad_run in (rc_fail, missing_cmd, relative_out, empty_out):
        assert pictures_dir(LINUX_ENV, os_name="linux", run=bad_run) == Path("/home/u/Pictures")
    assert pictures_dir({}, os_name="linux", run=missing_cmd) == Path.home() / "Pictures"

    # Windows 与 macOS
    assert pictures_dir({"USERPROFILE": "/w/Users/alice"}, os_name="windows") == Path("/w/Users/alice/Pictures")
    assert pictures_dir({}, os_name="windows") == Path.home() / "Pictures"
    assert pictures_dir({"HOME": "/Users/bob"}, os_name="macos") == Path("/Users/bob/Pictures")
    assert pictures_dir({}, os_name="macos") == Path.home() / "Pictures"
