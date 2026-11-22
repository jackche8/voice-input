"""升级或卸载 .deb 前通知运行中的 Ollama Voice Input 优雅退出（纯标准库，系统 python3 可直接运行）。

扫描 `<run_user_dir>/*/` 下的 `ollama-voice-input.lock` 与 `ollama-voice-input.sock`：
- 若 `.sock` 存在，以短超时发送 `{"cmd": "quit"}\\n`（容错连接拒绝/缺失/超时）；
- 以 `O_RDONLY` + `fcntl.flock(LOCK_EX|LOCK_NB)` 检测 `.lock` 是否被持有；
- 凡持锁或 `.sock` 回 `ok\\n` 且 `.lock` 存在，均轮询等待（50ms）至全部释放（exit 0）；
- 超时仍有实例未释放锁 → 向 stderr 报错并 exit 1（阻止 dpkg 混跑新旧代码）；
- 仅残留死套接字且无持锁、或无实例 → exit 0。
"""
from __future__ import annotations

import argparse
import errno
import fcntl
import os
import socket
import sys
import time
from collections.abc import Sequence
from pathlib import Path

APP_ID = "ollama-voice-input"
SOCK_NAME = f"{APP_ID}.sock"
LOCK_NAME = f"{APP_ID}.lock"
QUIT_PAYLOAD = b'{"cmd": "quit"}\n'
REPLY_OK = b"ok\n"
POLL_INTERVAL_S = 0.05
CONNECT_CAP_S = 3.0
TIMEOUT_MESSAGE = "请先退出 Ollama Voice Input 再升级/卸载"


def is_lock_held(lock_path: Path) -> bool:
    """以只读方式探查单例锁是否正被某实例持有；文件不存在或未加锁返回 False。"""
    if not lock_path.is_file():
        return False
    try:
        fd = os.open(str(lock_path), os.O_RDONLY)
    except OSError:
        return False
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        except OSError as exc:
            if exc.errno in (errno.EAGAIN, errno.EWOULDBLOCK, errno.EACCES):
                return True
            return False
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        return False
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


def send_quit(sock_path: Path, timeout_s: float) -> bool:
    """尝试向单个 AF_UNIX 套接字发送 quit 命令；收到 `ok\\n` 返回 True，任何连接/IO 异常容错返回 False。"""
    sock_timeout = max(0.1, min(CONNECT_CAP_S, timeout_s))
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(sock_timeout)
            client.connect(str(sock_path))
            client.sendall(QUIT_PAYLOAD)
            buf = b""
            while not buf.endswith(b"\n") and len(buf) < 1024:
                chunk = client.recv(256)
                if not chunk:
                    break
                buf += chunk
            return buf == REPLY_OK
    except (FileNotFoundError, ConnectionRefusedError, TimeoutError, OSError):
        return False


def quit_running_instances(run_user_dir: Path, *, timeout_s: float = 30.0) -> int:
    """通知 `run_user_dir` 下所有运行中实例退出并等待锁释放；返回 0 成功，1 超时。"""
    if not run_user_dir.is_dir():
        return 0
    deadline = time.monotonic() + max(0.0, timeout_s)
    pending_locks: list[Path] = []
    try:
        entries = sorted(run_user_dir.iterdir())
    except OSError:
        return 0

    for entry in entries:
        if not entry.is_dir():
            continue
        lock_path = entry / LOCK_NAME
        sock_path = entry / SOCK_NAME
        has_lock = lock_path.is_file()
        has_sock = sock_path.is_socket()
        if not has_lock and not has_sock:
            continue
        replied_ok = send_quit(sock_path, max(0.1, deadline - time.monotonic())) if has_sock else False
        if is_lock_held(lock_path) or (replied_ok and lock_path.is_file()):
            pending_locks.append(lock_path)

    if not pending_locks:
        return 0

    while True:
        pending_locks = [lock for lock in pending_locks if is_lock_held(lock)]
        if not pending_locks:
            return 0
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            print(TIMEOUT_MESSAGE, file=sys.stderr, flush=True)
            return 1
        time.sleep(min(POLL_INTERVAL_S, remaining))


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Notify running Ollama Voice Input instances to quit before dpkg change.")
    parser.add_argument(
        "--run-user-dir",
        type=Path,
        default=Path(os.environ.get("OVI_QUIT_RUN_USER_DIR", "/run/user")),
        help="Directory containing per-UID runtime directories (default: /run/user).",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=float(os.environ.get("OVI_QUIT_TIMEOUT_S", "30.0")),
        help="Maximum seconds to wait for instance lock release (default: 30).",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    return quit_running_instances(args.run_user_dir, timeout_s=args.timeout)


if __name__ == "__main__":
    raise SystemExit(main())
