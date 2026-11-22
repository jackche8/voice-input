"""R2 + U14 Linux .deb 打包真实链路与 quit_running.py 真实 AF_UNIX/flock 契约测试。"""
from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

from _paths import PROGRAM_ROOT
from _version import APP_VERSION

pytestmark = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux .deb 与 AF_UNIX/flock 用例仅在 Linux 运行")

ROOT = PROGRAM_ROOT
LINUX_DIR = ROOT / "packaging" / "linux"
BUILD_DEB = LINUX_DIR / "build_deb.py"
QUIT_RUNNING = LINUX_DIR / "quit_running.py"

FAKE_INSTANCE = """
import fcntl, os, socket, sys, time
from pathlib import Path

mode = sys.argv[1]
user_dir = Path(sys.argv[2])
ready_file = Path(sys.argv[3])
got_file = Path(sys.argv[4])
user_dir.mkdir(parents=True, exist_ok=True)
lock_path = user_dir / "ollama-voice-input.lock"
sock_path = user_dir / "ollama-voice-input.sock"

fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o644)
fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

if mode == "lock_only":
    ready_file.write_text("ready", encoding="utf-8")
    time.sleep(30)
    sys.exit(0)

if sock_path.exists():
    sock_path.unlink()
srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
srv.bind(str(sock_path))
srv.listen(4)
ready_file.write_text("ready", encoding="utf-8")

if mode == "hang_sock":
    conn, _ = srv.accept()
    _ = conn.recv(256)
    time.sleep(30)
    sys.exit(0)

conn, _ = srv.accept()
data = conn.recv(256)
got_file.write_bytes(data)
conn.sendall(b"ok\\n")
conn.close()
srv.close()
time.sleep(0.15)  # 模拟主进程逆序关闭子服务后再释放单例锁
os.close(fd)
"""


def _wait_until(predicate, timeout_s: float = 5.0) -> None:
    deadline = time.monotonic() + timeout_s
    while not predicate():
        assert time.monotonic() < deadline, "等待测试条件超时"
        time.sleep(0.02)


@pytest.fixture
def short_run_user_dir():
    """复用 test_single_instance 模式：优先 $XDG_RUNTIME_DIR，回退 .pp/tmp，保证 sun_path < 104B 且绝不写 /tmp。"""
    xdg = os.environ.get("XDG_RUNTIME_DIR", "")
    if xdg and Path(xdg).is_dir():
        base = Path(xdg) / f"ovi-qr-{os.getpid()}"
    else:
        base = ROOT / ".pp" / "tmp" / f"q-{os.getpid()}"
    if base.exists():
        shutil.rmtree(base)
    base.mkdir(parents=True)
    probe_sock = base / "1000" / "ollama-voice-input.sock"
    assert len(str(probe_sock).encode("utf-8")) < 104, f"AF_UNIX 路径超长：{probe_sock}"
    try:
        yield base
    finally:
        shutil.rmtree(base, ignore_errors=True)


def _run_lintian(deb: Path) -> subprocess.CompletedProcess[str]:
    """运行 `lintian --fail-on error`；若路径/TMPDIR 含非 ASCII 字符（如中文目录），改经纯 ASCII 的 `$XDG_RUNTIME_DIR` 暂存规避 Perl lintian 路径双重编码缺陷（绝不写 /tmp）。"""
    env = dict(os.environ)
    tmpdir = env.get("TMPDIR", "")
    needs_ascii = (not str(deb).isascii()) or (tmpdir and not tmpdir.isascii())
    xdg = env.get("XDG_RUNTIME_DIR", "")
    if needs_ascii and xdg and Path(xdg).is_dir():
        scratch = Path(xdg) / f"ovi-lintian-{os.getpid()}"
        if scratch.exists():
            shutil.rmtree(scratch)
        scratch.mkdir(parents=True)
        try:
            deb_copy = scratch / deb.name
            shutil.copyfile(deb, deb_copy)
            env["TMPDIR"] = str(scratch)
            return subprocess.run(
                ["lintian", "--fail-on", "error", str(deb_copy)],
                env=env,
                capture_output=True,
                text=True,
                timeout=60,
            )
        finally:
            shutil.rmtree(scratch, ignore_errors=True)
    return subprocess.run(["lintian", "--fail-on", "error", str(deb)], env=env, capture_output=True, text=True, timeout=60)


def test_build_deb_produces_lintian_clean_package_and_safe_maintainer_scripts(tmp_path: Path):
    # 1. 维护脚本与启动器必须通过 POSIX dash 语法检查
    for script in ("postinst", "prerm", "postrm", "launcher.sh"):
        chk = subprocess.run(["dash", "-n", str(LINUX_DIR / script)], capture_output=True, text=True, timeout=10)
        assert chk.returncode == 0, f"{script} dash -n 失败：{chk.stderr}"

    # 2. 离线假 wheelhouse 真实调用 build_deb.py 产出 .deb（覆盖 --python-minor 3.12 动态依赖）
    wheelhouse = tmp_path / "wheels"
    wheelhouse.mkdir()
    (wheelhouse / "dummy-0.1-py3-none-any.whl").write_bytes(b"fake-wheel-bytes")
    out_dir = tmp_path / "out"
    build = subprocess.run(
        [
            sys.executable,
            str(BUILD_DEB),
            "--output-dir",
            str(out_dir),
            "--wheelhouse-dir",
            str(wheelhouse),
            "--python-minor",
            "3.12",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert build.returncode == 0, build.stdout + build.stderr
    deb = out_dir / f"ollama-voice-input_{APP_VERSION}_amd64.deb"
    assert deb.is_file()

    # 3. dpkg-deb --info 核对控制字段与动态 Python 依赖
    info = subprocess.run(["dpkg-deb", "--info", str(deb)], capture_output=True, text=True, check=True, timeout=30).stdout
    for expected in (
        "Package: ollama-voice-input",
        f"Version: {APP_VERSION}",
        "Architecture: amd64",
        "Maintainer: Ollama Voice Input Maintainers <jackche8@users.noreply.github.com>",
        "python3 (>= 3.12), python3 (<< 3.13)",
        "python3-venv",
        "libxcb-cursor0",
        "libxkbcommon-x11-0",
        "gir1.2-webkit2-4.1",
        "tesseract-ocr",
        "xclip",
        "xdotool",
        "libportaudio2",
        "zstd",
    ):
        assert expected in info, f"control 缺少 {expected!r}\n{info}"

    # 4. dpkg-deb --contents 核对文件清单、权限与排除规则
    contents = subprocess.run(["dpkg-deb", "--contents", str(deb)], capture_output=True, text=True, check=True, timeout=30).stdout
    lines = contents.splitlines()

    def _entry_line(rel: str) -> str:
        matches = [line for line in lines if line.endswith(f" {rel}")]
        assert matches, f".deb 缺少文件 {rel}"
        return matches[0]

    assert _entry_line("./usr/bin/ollama-voice-input").startswith("-rwxr-xr-x root/root")
    for mode0644 in (
        "./usr/lib/ollama-voice-input/daemon.py",
        "./usr/lib/ollama-voice-input/quit_running.py",
        "./usr/lib/ollama-voice-input/requirements.txt",
        "./usr/lib/ollama-voice-input/wheelhouse/dummy-0.1-py3-none-any.whl",
        "./usr/lib/ollama-voice-input/static/v3/index.html",
        "./usr/lib/ollama-voice-input/runtimes/asr/server.py",
        "./usr/share/applications/ollama-voice-input.desktop",
        "./usr/share/applications/ollama-voice-input-daemon.desktop",
        "./usr/share/icons/hicolor/scalable/apps/ollama-voice-input.svg",
        "./usr/lib/udev/rules.d/70-ollama-voice-input.rules",
        "./usr/lib/modules-load.d/ollama-voice-input.conf",
        "./usr/share/doc/ollama-voice-input/copyright",
        "./usr/share/doc/ollama-voice-input/SECURITY-uaccess.txt",
        "./usr/share/doc/ollama-voice-input/COMMERCIAL.md",
        "./usr/share/doc/ollama-voice-input/THIRD-PARTY-NOTICES.md",
        "./usr/share/doc/ollama-voice-input/changelog.gz",
    ):
        assert _entry_line(mode0644).startswith("-rw-r--r-- root/root"), mode0644

    for forbidden in ("./lib/udev/", "__pycache__", ".pyc", "/tests/", "/docs/", "/.pp/", "/.venv/", "/.git/", "/packaging/"):
        assert forbidden not in contents, f".deb 违规包含 {forbidden!r}"

    # 5. DEBIAN 维护脚本权限与核心保护条款核对
    ctrl_dir = tmp_path / "control-extract"
    subprocess.run(["dpkg-deb", "--control", str(deb), str(ctrl_dir)], check=True, timeout=30)
    for script in ("postinst", "prerm", "postrm"):
        assert ((ctrl_dir / script).stat().st_mode & 0o777) == 0o755
    for meta in ("control", "md5sums"):
        assert ((ctrl_dir / meta).stat().st_mode & 0o777) == 0o644
    prerm_text = (ctrl_dir / "prerm").read_text(encoding="utf-8")
    assert "quit_running.py" in prerm_text and "failed-upgrade)" in prerm_text
    postinst_text = (ctrl_dir / "postinst").read_text(encoding="utf-8")
    assert "configure)" in postinst_text and "--only-binary=:all:" in postinst_text and 'rm -rf "$VENV"' in postinst_text

    # 6. 真实执行 lintian --fail-on error（0 error）
    lint = _run_lintian(deb)
    assert lint.returncode == 0, f"lintian 报错（rc={lint.returncode}）：\n{lint.stdout}\n{lint.stderr}"

    # 7. 模拟 prerm failed-upgrade / postinst abort-upgrade / postrm remove 行为
    failed_up = subprocess.run(["sh", str(ctrl_dir / "prerm"), "failed-upgrade"], capture_output=True, text=True, timeout=10)
    assert failed_up.returncode == 1, "prerm failed-upgrade 必须返回非 0 阻止绕过拒退"

    fake_app = tmp_path / "fake-app"
    fake_venv = fake_app / "venv"
    fake_venv.mkdir(parents=True)
    sentinel = fake_venv / "keep.txt"
    sentinel.write_text("alive", encoding="utf-8")
    user_data = tmp_path / "user-data"
    user_data.mkdir()
    (user_data / "config.json").write_text("{}", encoding="utf-8")

    abort_up = subprocess.run(
        ["sh", str(ctrl_dir / "postinst"), "abort-upgrade"],
        env={**os.environ, "OVI_APP_DIR": str(fake_app)},
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert abort_up.returncode == 0 and sentinel.is_file(), "postinst abort-upgrade 不得破坏旧 venv"

    postrm_rm = subprocess.run(
        ["sh", str(ctrl_dir / "postrm"), "remove"],
        env={**os.environ, "OVI_APP_DIR": str(fake_app)},
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert postrm_rm.returncode == 0 and not fake_venv.exists() and (user_data / "config.json").is_file()


def test_quit_running_handles_graceful_exit_stale_socket_and_stuck_lock(short_run_user_dir: Path, tmp_path: Path):
    def _run_quit(timeout_s: float) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(QUIT_RUNNING), "--run-user-dir", str(short_run_user_dir), "--timeout", str(timeout_s)],
            capture_output=True,
            text=True,
            timeout=15,
        )

    # 1. 无运行实例 → exit 0
    assert _run_quit(2.0).returncode == 0

    # 2. 残留死套接字（无持锁）→ 容错跳过 exit 0
    stale_dir = short_run_user_dir / "1002"
    stale_dir.mkdir()
    stale_sock = stale_dir / "ollama-voice-input.sock"
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.bind(str(stale_sock))
    s.close()
    assert stale_sock.is_socket()
    assert _run_quit(2.0).returncode == 0

    # 3. 正常实例：收到 quit 回 ok 后延后释放锁 → 等锁释放后 exit 0
    ready_ok = tmp_path / "ready-ok"
    got_ok = tmp_path / "got-ok"
    user_ok = short_run_user_dir / "1000"
    proc_ok = subprocess.Popen([sys.executable, "-c", FAKE_INSTANCE, "graceful", str(user_ok), str(ready_ok), str(got_ok)])
    try:
        _wait_until(ready_ok.is_file)
        res = _run_quit(5.0)
        assert res.returncode == 0, res.stderr
        assert got_ok.read_bytes() == b'{"cmd": "quit"}\n'
        assert proc_ok.wait(timeout=5) == 0
    finally:
        if proc_ok.poll() is None:
            proc_ok.kill()
            proc_ok.wait()

    # 4. 持锁但尚无套接字（启动中或套接字丢失）→ 超时 exit 1 并提示退出
    ready_lock = tmp_path / "ready-lock"
    got_lock = tmp_path / "got-lock"
    user_lock = short_run_user_dir / "1001"
    proc_lock = subprocess.Popen([sys.executable, "-c", FAKE_INSTANCE, "lock_only", str(user_lock), str(ready_lock), str(got_lock)])
    try:
        _wait_until(ready_lock.is_file)
        res = _run_quit(0.4)
        assert res.returncode == 1 and "请先退出 Ollama Voice Input 再升级/卸载" in res.stderr
    finally:
        proc_lock.kill()
        proc_lock.wait()

    # 5. 持锁且套接字无响应 → 超时 exit 1
    ready_hang = tmp_path / "ready-hang"
    got_hang = tmp_path / "got-hang"
    user_hang = short_run_user_dir / "1003"
    proc_hang = subprocess.Popen([sys.executable, "-c", FAKE_INSTANCE, "hang_sock", str(user_hang), str(ready_hang), str(got_hang)])
    try:
        _wait_until(ready_hang.is_file)
        res = _run_quit(0.4)
        assert res.returncode == 1 and "请先退出 Ollama Voice Input 再升级/卸载" in res.stderr
    finally:
        proc_hang.kill()
        proc_hang.wait()
