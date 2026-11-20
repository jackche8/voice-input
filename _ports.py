"""三个本地组件回环端口的唯一来源（只用标准库：识别服务的隔离环境也导入它）。

默认端口在这里定义，其它模块的默认地址都由它派生；环境变量 OVI_PORT_SERVER / OVI_PORT_ASR / OVI_PORT_OLLAMA
可整体改端口（真实链路测试用来避开正在使用的服务）。看护层、服务端装配与应用入口都经 local_ports 取，
环境变量随看护层传给子进程。"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

DEFAULT_SERVER_PORT = 17945  # 服务端（https，设置页/工作台/手机局域网入口）
DEFAULT_ASR_PORT = 17946  # 识别服务 sidecar（仅回环）
DEFAULT_OLLAMA_PORT = 17947  # 本应用自带的 Ollama（仅回环；不用 Ollama 官方默认的 11434，免得和用户自装的冲突）

ENV_SERVER, ENV_ASR, ENV_OLLAMA = "OVI_PORT_SERVER", "OVI_PORT_ASR", "OVI_PORT_OLLAMA"


@dataclass(frozen=True)
class LocalPorts:
    server: int
    asr: int
    ollama: int

    @property
    def server_origin(self) -> str:
        return f"https://127.0.0.1:{self.server}"

    @property
    def asr_url(self) -> str:
        return f"http://127.0.0.1:{self.asr}"

    @property
    def ollama_url(self) -> str:
        return f"http://127.0.0.1:{self.ollama}"


def _port(environ: Mapping[str, str], name: str, default: int) -> int:
    raw = environ.get(name)
    if raw is None:
        return default
    if not raw.isdigit() or not 0 < int(raw) < 65536:
        raise ValueError(f"环境变量 {name} 不是有效端口：{raw!r}")
    return int(raw)


def local_ports(environ: Mapping[str, str]) -> LocalPorts:
    return LocalPorts(
        server=_port(environ, ENV_SERVER, DEFAULT_SERVER_PORT),
        asr=_port(environ, ENV_ASR, DEFAULT_ASR_PORT),
        ollama=_port(environ, ENV_OLLAMA, DEFAULT_OLLAMA_PORT),
    )


__all__ = [
    "DEFAULT_ASR_PORT",
    "DEFAULT_OLLAMA_PORT",
    "DEFAULT_SERVER_PORT",
    "ENV_ASR",
    "ENV_OLLAMA",
    "ENV_SERVER",
    "LocalPorts",
    "local_ports",
]
