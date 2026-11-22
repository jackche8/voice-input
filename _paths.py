"""程序根、配置根、数据根与全部文件路径的唯一表（三平台）。

程序根 = 本文件所在目录（源码运行 = 仓库；.deb = /usr/lib/ollama-voice-input），只读。
配置根放小而私密的文件（配置、令牌、证书），数据根放大文件（历史、任务、日志、运行环境、模型）。
源码运行与安装版用同一套规则；全部读取方只从这里取路径，不再自己拼项目根。
"""
from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from _contracts import OLLAMA_VERSION
from _platform.detect import detect_os
from _platform.protocol import OsName

APP_ID = "ollama-voice-input"  # 目录名、锁名、套接字名
APP_DISPLAY_NAME = "Ollama Voice Input"
APP_USER_MODEL_ID = "io.github.jackche8.ollama-voice-input"
DEB_PROGRAM_ROOT = Path("/usr/lib/ollama-voice-input")
DEB_LAUNCHER = "/usr/bin/ollama-voice-input"
ASR_RUNTIME_DIRNAME = "_asr_runtime"
INTERNAL_CHILD_GUARD_FLAG = "--internal-child-guard"
INTERNAL_SERVER_FLAG = "--internal-server"
INTERNAL_SETUP_WORKER_FLAG = "--internal-setup-worker"
INTERNAL_WINDOW_FLAG = "--internal-window"


def resolve_program_root(
    *,
    frozen: bool | None = None,
    meipass: str | Path | None = None,
    file_path: Path | None = None,
) -> Path:
    """程序根：PyInstaller 冻结态取 `sys._MEIPASS`，源码 / `.deb` 取本文件所在目录。"""
    is_frozen = bool(getattr(sys, "frozen", False)) if frozen is None else frozen
    bundle_dir = getattr(sys, "_MEIPASS", None) if meipass is None else meipass
    if is_frozen and bundle_dir:
        return Path(bundle_dir).resolve()
    return (Path(__file__) if file_path is None else Path(file_path)).resolve().parent


PROGRAM_ROOT = resolve_program_root()
RESOURCES_DIR = PROGRAM_ROOT / "resources"
STATIC_V3 = PROGRAM_ROOT / "static" / "v3"


def asr_runtime_root(program_root: Path = PROGRAM_ROOT, *, frozen: bool | None = None) -> Path:
    """外部 ASR 解释器（`paths.asr_python`）的运行目录与 PYTHONPATH 根。

    冻结态隔离在 `<_MEIPASS>/_asr_runtime`，避免 `_MEIPASS` 根的 `_internal/` 遮蔽 ASR 独立 venv 的二进制依赖。
    """
    is_frozen = bool(getattr(sys, "frozen", False)) if frozen is None else frozen
    base = Path(program_root)
    return base / ASR_RUNTIME_DIRNAME if is_frozen else base


@dataclass(frozen=True)
class AppPaths:
    config_root: Path
    data_root: Path
    runtime_dir: Path
    os_name: OsName
    live: bool
    program_root: Path = PROGRAM_ROOT

    @classmethod
    def for_roots(
        cls,
        *,
        config_root: Path,
        data_root: Path,
        runtime_dir: Path,
        os_name: OsName | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> "AppPaths":
        """测试与隔离运行用：显式给根目录。任一根位于当前用户的真实配置根、数据根或运行目录之下（含相等）即拒绝；
        只有 default_app_paths 能绑定真实根。"""
        paths = cls(
            config_root=Path(config_root).resolve(),
            data_root=Path(data_root).resolve(),
            runtime_dir=Path(runtime_dir).resolve(),
            os_name=os_name or detect_os(),
            live=False,
        )
        live = default_app_paths(os.environ if environ is None else environ, os_name=paths.os_name)
        live_roots = tuple(root.resolve() for root in (live.config_root, live.data_root, live.runtime_dir))
        for given in (paths.config_root, paths.data_root, paths.runtime_dir):
            for root in live_roots:
                if given.is_relative_to(root):
                    raise RuntimeError(f"非 live 路径不得位于当前用户的真实根之下：{given}（真实根 {root}）")
        return paths

    # ---- 配置根 ----------------------------------------------------------
    @property
    def config(self) -> Path:
        return self.config_root / "config.json"

    @property
    def token(self) -> Path:
        return self.config_root / "web_input_token"

    @property
    def cert_dir(self) -> Path:
        return self.config_root / "certs"

    @property
    def cert(self) -> Path:
        return self.cert_dir / "cert.pem"

    @property
    def key(self) -> Path:
        return self.cert_dir / "key.pem"

    @property
    def ca(self) -> Path:
        return self.cert_dir / "ca.pem"

    # ---- 数据根 ----------------------------------------------------------
    @property
    def history(self) -> Path:
        return self.data_root / "history.jsonl"

    @property
    def jobs(self) -> Path:
        return self.data_root / "jobs.json"

    @property
    def upload_tmp(self) -> Path:
        return self.data_root / "upload-tmp"

    @property
    def logs(self) -> Path:
        return self.data_root / "logs"

    @property
    def temp_dir(self) -> Path:
        """应用自己的临时文件（如 macOS 截屏、剪贴板快照）；程序根在安装版里只读，不能放那里。"""
        return self.data_root / "tmp"

    @property
    def run_dir(self) -> Path:
        return self.data_root / "run"

    @property
    def children_record(self) -> Path:
        return self.run_dir / "children.json"

    @property
    def setup_tasks(self) -> Path:
        return self.data_root / "setup" / "tasks"

    @property
    def ollama_install_dir(self) -> Path:
        return self.data_root / "runtimes" / "ollama" / OLLAMA_VERSION

    @property
    def ollama_binary(self) -> Path:
        if self.os_name == "windows":
            return self.ollama_install_dir / "ollama.exe"
        if self.os_name == "macos":
            return self.ollama_install_dir / "Ollama.app" / "Contents" / "Resources" / "ollama"
        return self.ollama_install_dir / "bin" / "ollama"

    @property
    def ollama_models(self) -> Path:
        return self.data_root / "models" / "ollama"

    @property
    def asr_model_dir(self) -> Path:
        return self.data_root / "models" / "Qwen3-ASR-1.7B"

    @property
    def hf_home(self) -> Path:
        return self.data_root / "hf-home"

    @property
    def uv_dir(self) -> Path:
        return self.data_root / "runtimes" / "uv"

    @property
    def python_dir(self) -> Path:
        return self.data_root / "runtimes" / "python"

    @property
    def asr_venv(self) -> Path:
        return self.data_root / "runtimes" / "asr" / "venv"

    @property
    def asr_manifest(self) -> Path:
        return self.data_root / "runtimes" / "asr" / "manifest.json"

    @property
    def asr_python(self) -> Path:
        if self.os_name == "windows":
            return self.asr_venv / "Scripts" / "python.exe"
        return self.asr_venv / "bin" / "python"

    # ---- 运行目录 --------------------------------------------------------
    @property
    def lock(self) -> Path:
        return self.runtime_dir / f"{APP_ID}.lock"


def _required_dir(environ: Mapping[str, str], name: str) -> Path:
    value = environ.get(name, "")
    if not value or not Path(value).is_absolute():
        raise RuntimeError(f"环境变量 {name} 缺失或不是绝对路径，无法确定用户数据目录（收到 {value!r}）")
    return Path(value)


def _xdg_dir(environ: Mapping[str, str], name: str, home: Path, default: str) -> Path:
    value = environ.get(name, "")
    # XDG 规范：未设置或相对路径时视为无效，用默认值
    return Path(value) if value and Path(value).is_absolute() else home / default


def default_app_paths(environ: Mapping[str, str], *, os_name: OsName | None = None) -> AppPaths:
    """当前用户的真实路径（live）：Linux 按 XDG，Windows 按 APPDATA/LOCALAPPDATA，macOS 按 Application Support。"""
    os_name = os_name or detect_os()
    if os_name == "windows":
        config_root = _required_dir(environ, "APPDATA") / APP_DISPLAY_NAME
        data_root = _required_dir(environ, "LOCALAPPDATA") / APP_DISPLAY_NAME
        runtime_dir = data_root / "run"
    else:
        home = _required_dir(environ, "HOME")
        if os_name == "macos":
            config_root = data_root = home / "Library" / "Application Support" / APP_DISPLAY_NAME
            runtime_dir = data_root / "run"
        else:
            config_root = _xdg_dir(environ, "XDG_CONFIG_HOME", home, ".config") / APP_ID
            data_root = _xdg_dir(environ, "XDG_DATA_HOME", home, ".local/share") / APP_ID
            xdg_runtime = environ.get("XDG_RUNTIME_DIR", "")
            runtime_dir = Path(xdg_runtime) if xdg_runtime and Path(xdg_runtime).is_absolute() else data_root / "run"
    return AppPaths(config_root=config_root, data_root=data_root, runtime_dir=runtime_dir, os_name=os_name, live=True)


def _home_dir(environ: Mapping[str, str]) -> Path:
    raw = environ.get("HOME", "").strip()
    return Path(raw) if raw and Path(raw).is_absolute() else Path.home()


def pictures_dir(
    environ: Mapping[str, str],
    *,
    os_name: OsName | None = None,
    run: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> Path:
    """用户图片目录（截屏标注保存的父目录，子目录为 `pictures_dir(...) / APP_DISPLAY_NAME`）。"""
    resolved_os = os_name or detect_os()
    if resolved_os == "windows":
        profile = environ.get("USERPROFILE", "").strip()
        base = Path(profile) if profile else Path.home()
        return base / "Pictures"
    if resolved_os == "macos":
        return _home_dir(environ) / "Pictures"
    try:
        proc = run(
            ["xdg-user-dir", "PICTURES"],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
            env=dict(environ),
        )
    except (OSError, subprocess.SubprocessError):
        proc = None
    if proc is not None and proc.returncode == 0:
        raw = (
            proc.stdout.decode("utf-8", errors="replace")
            if isinstance(proc.stdout, (bytes, bytearray))
            else str(proc.stdout or "")
        )
        out = raw.strip()
        if out and Path(out).is_absolute():
            return Path(out)
    return _home_dir(environ) / "Pictures"


def app_command(
    paths: AppPaths,
    *,
    background: bool,
    frozen: bool | None = None,
    executable: str | None = None,
) -> list[str]:
    """启动应用主进程的命令（开机自启、门户条目共用）：统一处理冻结态、Linux `.deb` 安装版与源码运行。"""
    is_frozen = bool(getattr(sys, "frozen", False)) if frozen is None else frozen
    exe = sys.executable if executable is None else executable
    if is_frozen:
        command = [exe]
    elif paths.os_name == "linux" and paths.program_root.resolve() == DEB_PROGRAM_ROOT:
        command = [DEB_LAUNCHER]
    else:
        command = [exe, str(paths.program_root / "daemon.py")]
    return [*command, "--background"] if background else command


def internal_subcommand(
    flag: str,
    source_args: Sequence[str],
    *,
    frozen: bool | None = None,
    executable: str | None = None,
) -> list[str]:
    """单源构造冻结态内部派发 / 源码子模块命令前缀（child_guard / server / setup_worker / window 共用）。"""
    is_frozen = bool(getattr(sys, "frozen", False)) if frozen is None else frozen
    exe = sys.executable if executable is None else executable
    if is_frozen:
        return [exe, flag]
    return [exe, *source_args]


__all__ = [
    "APP_DISPLAY_NAME",
    "APP_ID",
    "APP_USER_MODEL_ID",
    "ASR_RUNTIME_DIRNAME",
    "DEB_LAUNCHER",
    "DEB_PROGRAM_ROOT",
    "INTERNAL_CHILD_GUARD_FLAG",
    "INTERNAL_SERVER_FLAG",
    "INTERNAL_SETUP_WORKER_FLAG",
    "INTERNAL_WINDOW_FLAG",
    "PROGRAM_ROOT",
    "RESOURCES_DIR",
    "STATIC_V3",
    "AppPaths",
    "app_command",
    "asr_runtime_root",
    "default_app_paths",
    "internal_subcommand",
    "pictures_dir",
    "resolve_program_root",
]
