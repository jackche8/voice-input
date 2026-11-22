"""构建 Linux `.deb` 安装包（权威打包器）。

产出：`<output-dir>/ollama-voice-input_<APP_VERSION>_<arch>.deb`
- 版本号唯一来自 `_version.APP_VERSION`；
- 动态按构建 Python 的 `(major, minor)` 生成 `Depends: python3 (>= M.m), python3 (<< M.(m+1))`（支持 `--python-minor` 覆盖）；
- 离线测试通过 `--wheelhouse-dir` 注入假轮子目录；在线打包通过 `pip wheel -r requirements.txt` 预编纯 `.whl`（含 `evdev` 等 C 扩展）；
- 显式白名单收集运行时代码，跳过 `__pycache__`/`*.pyc` 与开发/测试文件；
- 使用 `dpkg-deb --build --root-owner-group`，生成原生包 `changelog.gz`（`gzip -9n`）与 `md5sums`，保证 `lintian --fail-on error` 零错误。
"""
from __future__ import annotations

import argparse
import email.utils
import gzip
import hashlib
import os
import shutil
import subprocess
import sys
from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LINUX_DIR = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from _version import APP_VERSION  # noqa: E402

PKG_NAME = "ollama-voice-input"
APP_INSTALL_REL = Path("usr/lib") / PKG_NAME
DOC_REL = Path("usr/share/doc") / PKG_NAME
PRODUCT_DIRS = (
    "_desktop",
    "_linux",
    "_platform",
    "_supervision",
    "_ui_desktop",
    "_v3",
    "_window",
    "runtimes",
    "static",
    "resources",
)
ROOT_EXTRA_FILES = ("requirements.txt", "LICENSE", "COMMERCIAL.md", "THIRD-PARTY-NOTICES.md")
SKIP_NAMES = frozenset({"__pycache__", ".DS_Store"})
SKIP_SUFFIXES = (".pyc", ".pyo")


def parse_python_minor(raw: str | None) -> tuple[str, str]:
    """将 `3.12` / `3.14` 转为 `(python_min, python_max)`，如 `('3.12', '3.13')`。"""
    if raw is None:
        major, minor = sys.version_info.major, sys.version_info.minor
    else:
        parts = raw.strip().split(".")
        if len(parts) != 2 or not all(p.isdigit() for p in parts):
            raise ValueError(f"--python-minor 必须为 MAJOR.MINOR 格式（收到 {raw!r}）")
        major, minor = int(parts[0]), int(parts[1])
    return f"{major}.{minor}", f"{major}.{minor + 1}"


def _copy_file(src: Path, dst: Path, mode: int) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, dst)
    os.chmod(dst, mode)


def _copy_tree_filtered(src_dir: Path, dst_dir: Path) -> None:
    dst_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(dst_dir, 0o755)
    for item in sorted(src_dir.iterdir()):
        if item.name in SKIP_NAMES or item.name.endswith(SKIP_SUFFIXES):
            continue
        target = dst_dir / item.name
        if item.is_dir():
            _copy_tree_filtered(item, target)
        elif item.is_file():
            _copy_file(item, target, 0o644)


def _populate_wheelhouse(dest: Path, *, wheelhouse_dir: Path | None, pip_python: str) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    os.chmod(dest, 0o755)
    if wheelhouse_dir is not None:
        if not wheelhouse_dir.is_dir():
            raise FileNotFoundError(f"指定的 --wheelhouse-dir 不存在或不是目录：{wheelhouse_dir}")
        for item in sorted(wheelhouse_dir.iterdir()):
            if item.is_file() and not item.name.startswith("."):
                _copy_file(item, dest / item.name, 0o644)
        return
    cmd = [pip_python, "-m", "pip", "wheel", "--wheel-dir", str(dest), "-r", str(ROOT / "requirements.txt")]
    proc = subprocess.run(cmd, cwd=ROOT, check=False)
    if proc.returncode != 0:
        raise RuntimeError(f"构建离线 wheelhouse 失败（退出码 {proc.returncode}）：{' '.join(cmd)}")
    for item in dest.iterdir():
        if item.is_file():
            os.chmod(item, 0o644)


def _render_native_changelog(version: str) -> bytes:
    date_str = email.utils.format_datetime(datetime.now(timezone.utc))
    text = (
        f"{PKG_NAME} ({version}) unstable; urgency=medium\n"
        "\n"
        f"  * Release {version}: standalone desktop package with offline wheelhouse,\n"
        "    udev uaccess keyboard/uinput rules, and graceful upgrade quit handling.\n"
        "\n"
        f" -- Ollama Voice Input Maintainers <jackche8@users.noreply.github.com>  {date_str}\n"
    )
    return gzip.compress(text.encode("utf-8"), compresslevel=9, mtime=0)


def _write_md5sums(stage: Path) -> None:
    entries: list[str] = []
    for path in sorted(stage.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(stage)
        if rel.parts[0] == "DEBIAN":
            continue
        digest = hashlib.md5(path.read_bytes()).hexdigest()
        entries.append(f"{digest}  {rel.as_posix()}\n")
    md5_path = stage / "DEBIAN" / "md5sums"
    md5_path.write_text("".join(entries), encoding="utf-8")
    os.chmod(md5_path, 0o644)


def _installed_size_kib(stage: Path) -> int:
    total = 0
    for path in stage.rglob("*"):
        if path.is_file() and path.relative_to(stage).parts[0] != "DEBIAN":
            total += path.stat().st_size
    return max(1, (total + 1023) // 1024)


def build_deb(
    *,
    output_dir: Path,
    wheelhouse_dir: Path | None = None,
    pip_python: str = sys.executable,
    python_minor: str | None = None,
    arch: str = "amd64",
    version: str = APP_VERSION,
) -> Path:
    """在 `output_dir` 产出 `ollama-voice-input_<version>_<arch>.deb` 并返回其路径。"""
    py_min, py_max = parse_python_minor(python_minor)
    output_dir = Path(output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    deb_path = output_dir / f"{PKG_NAME}_{version}_{arch}.deb"
    stage = output_dir / f".deb-staging-{os.getpid()}"
    if stage.exists():
        shutil.rmtree(stage)

    try:
        app_dst = stage / APP_INSTALL_REL
        app_dst.mkdir(parents=True, exist_ok=True)
        os.chmod(app_dst, 0o755)

        # 1. 白名单复制应用运行时源码与必要文件（排除 tests/docs/.pp/.venv/.git 等）
        for py_file in sorted(ROOT.glob("*.py")):
            _copy_file(py_file, app_dst / py_file.name, 0o644)
        for name in ROOT_EXTRA_FILES:
            _copy_file(ROOT / name, app_dst / name, 0o644)
        for dirname in PRODUCT_DIRS:
            _copy_tree_filtered(ROOT / dirname, app_dst / dirname)
        _copy_file(LINUX_DIR / "quit_running.py", app_dst / "quit_running.py", 0o644)

        # 2. 离线 wheelhouse
        _populate_wheelhouse(app_dst / "wheelhouse", wheelhouse_dir=wheelhouse_dir, pip_python=pip_python)

        # 3. 启动器、桌面条目、图标、udev 与 modules-load
        _copy_file(LINUX_DIR / "launcher.sh", stage / "usr/bin" / PKG_NAME, 0o755)
        _copy_file(
            LINUX_DIR / "ollama-voice-input.desktop",
            stage / "usr/share/applications/ollama-voice-input.desktop",
            0o644,
        )
        _copy_file(
            LINUX_DIR / "ollama-voice-input-daemon.desktop",
            stage / "usr/share/applications/ollama-voice-input-daemon.desktop",
            0o644,
        )
        _copy_file(
            ROOT / "resources/ollama-voice-input.svg",
            stage / "usr/share/icons/hicolor/scalable/apps/ollama-voice-input.svg",
            0o644,
        )
        _copy_file(
            LINUX_DIR / "70-ollama-voice-input.rules",
            stage / "usr/lib/udev/rules.d/70-ollama-voice-input.rules",
            0o644,
        )
        _copy_file(
            LINUX_DIR / "ollama-voice-input.conf",
            stage / "usr/lib/modules-load.d/ollama-voice-input.conf",
            0o644,
        )

        # 4. 文档、版权、合规与原生包 changelog.gz（0644）
        doc_dst = stage / DOC_REL
        _copy_file(LINUX_DIR / "copyright", doc_dst / "copyright", 0o644)
        _copy_file(LINUX_DIR / "SECURITY-uaccess.txt", doc_dst / "SECURITY-uaccess.txt", 0o644)
        _copy_file(ROOT / "COMMERCIAL.md", doc_dst / "COMMERCIAL.md", 0o644)
        _copy_file(ROOT / "THIRD-PARTY-NOTICES.md", doc_dst / "THIRD-PARTY-NOTICES.md", 0o644)
        changelog_path = doc_dst / "changelog.gz"
        changelog_path.write_bytes(_render_native_changelog(version))
        os.chmod(changelog_path, 0o644)

        # 5. DEBIAN 控制文件与维护脚本
        debian_dir = stage / "DEBIAN"
        debian_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(debian_dir, 0o755)
        control_template = (LINUX_DIR / "control.in").read_text(encoding="utf-8")
        control_text = (
            control_template.replace("@VERSION@", version)
            .replace("@ARCH@", arch)
            .replace("@PYTHON_MIN@", py_min)
            .replace("@PYTHON_MAX@", py_max)
            .replace("@INSTALLED_SIZE@", str(_installed_size_kib(stage)))
        )
        control_path = debian_dir / "control"
        control_path.write_text(control_text, encoding="utf-8")
        os.chmod(control_path, 0o644)
        for script_name in ("postinst", "prerm", "postrm"):
            _copy_file(LINUX_DIR / script_name, debian_dir / script_name, 0o755)
        _write_md5sums(stage)

        # 6. 统一所有目录权限为 0755 后打包（--root-owner-group 避免非 root UID/GID 报错）
        for dirpath, _dirnames, _filenames in os.walk(stage):
            os.chmod(dirpath, 0o755)

        subprocess.run(
            ["dpkg-deb", "--build", "--root-owner-group", str(stage), str(deb_path)],
            check=True,
        )
        return deb_path
    finally:
        if stage.exists():
            shutil.rmtree(stage)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build Linux .deb package for Ollama Voice Input.")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "dist")
    parser.add_argument("--wheelhouse-dir", type=Path, default=None)
    parser.add_argument("--pip-python", default=sys.executable)
    parser.add_argument("--python-minor", default=None, help="Override target Python MAJOR.MINOR (e.g. 3.12).")
    parser.add_argument("--arch", default="amd64")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    deb_path = build_deb(
        output_dir=args.output_dir,
        wheelhouse_dir=args.wheelhouse_dir,
        pip_python=args.pip_python,
        python_minor=args.python_minor,
        arch=args.arch,
    )
    print(deb_path, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
