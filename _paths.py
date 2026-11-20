"""程序根、配置根、数据根与全部文件路径的唯一表（三平台）。

程序根 = 本文件所在目录（源码运行 = 仓库；.deb = /usr/lib/ollama-voice-input），只读。
配置根放小而私密的文件（配置、令牌、证书），数据根放大文件（历史、任务、日志、运行环境、模型）。
源码运行与安装版用同一套规则；全部读取方只从这里取路径，不再自己拼项目根。
"""
from __future__ import annotations

import os
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from _contracts import OLLAMA_VERSION
from _platform.detect import detect_os
from _platform.protocol import OsName

APP_ID = "ollama-voice-input"  # 目录名、锁名、套接字名
APP_DISPLAY_NAME = "Ollama Voice Input"
PROGRAM_ROOT = Path(__file__).resolve().parent
STATIC_V3 = PROGRAM_ROOT / "static" / "v3"


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


def app_command(paths: AppPaths, *, background: bool) -> list[str]:
    """启动应用主进程的命令（开机自启、单例唤起说明共用）。源码运行与安装版都由当前解释器执行程序根下的入口。"""
    command = [sys.executable, str(paths.program_root / "daemon.py")]
    return [*command, "--background"] if background else command


__all__ = [
    "APP_DISPLAY_NAME",
    "APP_ID",
    "PROGRAM_ROOT",
    "STATIC_V3",
    "AppPaths",
    "app_command",
    "default_app_paths",
]
