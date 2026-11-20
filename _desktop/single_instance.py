"""单例唤起（应用主进程，业务层）：持锁实例监听本地套接字；第二个实例把「打开主窗口」交给它后退出。

地址：POSIX 为 runtime_dir 下的全路径（不落 /tmp），Windows 为按用户区分的命名管道。
协议：每条连接一行 UTF-8 JSON，回一行：{"cmd": "show", "route": "/…"} → ok；{"cmd": "quit"} → ok 后走托盘「退出」同一路径；
其它 → error unknown-command。.deb 的 prerm 也经这里通知运行中的实例退出。
"""
from __future__ import annotations

import getpass
import json
from collections.abc import Callable

from PySide6.QtCore import QObject
from PySide6.QtNetwork import QLocalServer, QLocalSocket

from _desktop.singleton import read_lock_owner
from _paths import APP_ID, AppPaths
from _window.commands import Quit, Show, parse_command

CONNECT_TIMEOUT_MS = 3000
REPLY_OK = b"ok\n"
REPLY_UNKNOWN = b"error unknown-command\n"


def socket_name(paths: AppPaths) -> str:
    if paths.os_name == "windows":
        return f"{APP_ID}-{getpass.getuser()}"
    return str(paths.runtime_dir / f"{APP_ID}.sock")


class SingleInstanceServer(QObject):
    """在 Qt 主线程里监听；命令回调也在主线程执行。"""

    def __init__(self, name: str, *, on_show: Callable[[str], None], on_quit: Callable[[], None]) -> None:
        super().__init__()
        self._name = name
        self._on_show = on_show
        self._on_quit = on_quit
        self._server = QLocalServer(self)
        self._server.newConnection.connect(self._accept)

    def listen(self) -> None:
        """持有单例锁后调用：先清掉上次崩溃留下的同名套接字文件，再监听；失败直接报错。"""
        QLocalServer.removeServer(self._name)
        if not self._server.listen(self._name):
            raise RuntimeError(f"单例套接字 {self._name} 监听失败：{self._server.errorString()}")

    def close(self) -> None:
        self._server.close()

    def _accept(self) -> None:
        while self._server.hasPendingConnections():
            sock = self._server.nextPendingConnection()
            sock.readyRead.connect(lambda s=sock: self._handle(s))
            sock.disconnected.connect(sock.deleteLater)

    def _handle(self, sock: QLocalSocket) -> None:
        if not sock.canReadLine():
            return
        command = parse_command(bytes(sock.readLine()).decode("utf-8", errors="replace"))
        sock.write(REPLY_OK if command is not None else REPLY_UNKNOWN)
        sock.flush()
        sock.disconnectFromServer()
        if isinstance(command, Show):
            self._on_show(command.route)
        elif isinstance(command, Quit):
            self._on_quit()


def send_command(name: str, command: dict, *, timeout_ms: int = CONNECT_TIMEOUT_MS) -> bool:
    """第二个实例 / prerm 用：连上、写一行命令、等回 ok。连不上或超时 → False。"""
    sock = QLocalSocket()
    sock.connectToServer(name)
    if not sock.waitForConnected(timeout_ms):
        return False
    sock.write((json.dumps(command) + "\n").encode("utf-8"))
    sock.flush()
    got = b""
    while not got.endswith(b"\n") and sock.waitForReadyRead(timeout_ms):
        got += bytes(sock.readAll())
    sock.disconnectFromServer()
    return got == REPLY_OK


def run_as_second_instance(paths: AppPaths, route: str = "/", *, timeout_ms: int = CONNECT_TIMEOUT_MS) -> int:
    """拿不到单例锁时调用：让已运行的实例打开主窗口。返回本进程退出码：0 已唤起，1 已有实例未响应。"""
    if send_command(socket_name(paths), {"cmd": "show", "route": route}, timeout_ms=timeout_ms):
        return 0
    print(f"已有实例未响应（PID {read_lock_owner(paths.lock)}）", flush=True)
    return 1


__all__ = ["SingleInstanceServer", "run_as_second_instance", "send_command", "socket_name"]
