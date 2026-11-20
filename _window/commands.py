"""窗口进程 stdin 协议：每行一个 JSON。{"cmd": "show", "route": "/setup"} → 导航并前置；{"cmd": "quit"} 或 EOF → 退出。

其它输入（空行、坏 JSON、未知命令、非 / 开头的路由）一律忽略：stdin 只由应用主进程写。
"""
from __future__ import annotations

import json
from collections.abc import Callable, Iterable
from dataclasses import dataclass


@dataclass(frozen=True)
class Show:
    route: str


@dataclass(frozen=True)
class Quit:
    pass


Command = Show | Quit


def parse_command(line: str) -> Command | None:
    try:
        data = json.loads(line)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    if data.get("cmd") == "quit":
        return Quit()
    route = data.get("route")
    if data.get("cmd") == "show" and isinstance(route, str) and route.startswith("/") and not route.startswith("//"):
        return Show(route)
    return None


def pump_commands(lines: Iterable[str], on_show: Callable[[str], None], on_quit: Callable[[], None]) -> None:
    """逐行分派，quit 或输入结束（应用关掉管道/退出）都调用 on_quit 一次后返回。"""
    for line in lines:
        command = parse_command(line)
        if isinstance(command, Quit):
            break
        if isinstance(command, Show):
            on_show(command.route)
    on_quit()


def encode(command: Command) -> bytes:
    payload = {"cmd": "quit"} if isinstance(command, Quit) else {"cmd": "show", "route": command.route}
    return (json.dumps(payload) + "\n").encode("utf-8")


__all__ = ["Command", "Quit", "Show", "encode", "parse_command", "pump_commands"]
