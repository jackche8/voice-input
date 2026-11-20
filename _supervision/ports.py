"""端口占用与占用者识别（拉起组件前检查：被别的进程占着就不抢占，如实说明是谁）。

是否占用：向 127.0.0.1:<port> 发起连接，连上即有进程在监听（三平台同一判据）。
是谁：Linux 读 /proc/net/tcp{,6} 的监听 inode 再找持有该 socket 的进程；macOS `lsof`；Windows `netstat -ano` + `tasklist`。
识别不了（别的用户的进程、命令失败）时如实写「未能识别占用进程」及原因，占用结论不变。
"""
from __future__ import annotations

import csv
import io
import os
import socket
import subprocess
from collections.abc import Callable
from pathlib import Path

from _platform.detect import detect_os
from _platform.protocol import OsName

CONNECT_TIMEOUT_S = 0.5
_LISTEN = "0A"
Runner = Callable[..., subprocess.CompletedProcess]


def port_in_use(port: int, *, host: str = "127.0.0.1") -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(CONNECT_TIMEOUT_S)
        return sock.connect_ex((host, port)) == 0


def _linux_listen_inodes(port: int, proc_root: Path) -> set[str]:
    inodes: set[str] = set()
    for table in ("tcp", "tcp6"):
        try:
            lines = (proc_root / "net" / table).read_text().splitlines()[1:]
        except OSError:
            continue
        for line in lines:
            fields = line.split()
            if len(fields) > 9 and fields[3] == _LISTEN and int(fields[1].rsplit(":", 1)[1], 16) == port:
                inodes.add(fields[9])
    return inodes


def _linux_owner(port: int, proc_root: Path) -> str:
    targets = {f"socket:[{inode}]" for inode in _linux_listen_inodes(port, proc_root)}
    for entry in proc_root.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            fds = list((entry / "fd").iterdir())
        except OSError:
            continue  # 别的用户的进程读不了 fd
        for fd in fds:
            try:
                if os.readlink(fd) in targets:
                    name = (entry / "comm").read_text().strip()
                    return f"PID {entry.name}（{name}）"
            except OSError:
                continue
    return "未能识别占用进程（可能属于其他用户）"


def _macos_owner(port: int, run: Runner) -> str:
    proc = run(["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-Fpc"], capture_output=True, text=True, timeout=10, check=False)
    pid = next((line[1:] for line in proc.stdout.splitlines() if line.startswith("p")), None)
    name = next((line[1:] for line in proc.stdout.splitlines() if line.startswith("c")), None)
    return f"PID {pid}（{name}）" if pid else f"未能识别占用进程（lsof 退出码 {proc.returncode}）"


def _windows_owner(port: int, run: Runner) -> str:
    proc = run(["netstat", "-ano", "-p", "TCP"], capture_output=True, text=True, timeout=10, check=False)
    pid = None
    for line in proc.stdout.splitlines():
        fields = line.split()
        if len(fields) == 5 and fields[3] == "LISTENING" and fields[1].rsplit(":", 1)[-1] == str(port):
            pid = fields[4]
            break
    if pid is None:
        return f"未能识别占用进程（netstat 退出码 {proc.returncode}）"
    listing = run(["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"], capture_output=True, text=True, timeout=10, check=False)
    row = next(csv.reader(io.StringIO(listing.stdout)), None)
    return f"PID {pid}（{row[0]}）" if row else f"PID {pid}"


def port_owner(
    port: int,
    *,
    os_name: OsName | None = None,
    run: Runner = subprocess.run,
    in_use: Callable[[int], bool] = port_in_use,
    proc_root: Path = Path("/proc"),
) -> str | None:
    """端口空闲 → None；被占用 → 占用者描述（「PID x（名称）」或未能识别的原因）。"""
    if not in_use(port):
        return None
    os_name = os_name or detect_os()
    if os_name == "linux":
        return _linux_owner(port, proc_root)
    if os_name == "macos":
        return _macos_owner(port, run)
    return _windows_owner(port, run)


__all__ = ["port_in_use", "port_owner"]
