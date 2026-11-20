"""主窗口进程入口：python -m _window --url https://127.0.0.1:<服务端端口><route> --cert <证书> --title <标题> [--backend …]。

退出码：0 = 正常关闭（关窗、quit、stdin 结束）；2 = 参数错误；3 = 本机缺少所需网页内核（原因写 stderr）。
"""
from __future__ import annotations

import argparse
import sys
from collections.abc import Callable, Mapping, Sequence
from urllib.parse import urlsplit

from _window import BackendMissing
from _window.tls_policy import TRUSTED_HOST

EXIT_BACKEND_MISSING = 3
Backend = Callable[[argparse.Namespace], int]


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="python -m _window")
    parser.add_argument("--url", required=True)
    parser.add_argument("--cert", required=True)
    parser.add_argument("--title", default="Ollama Voice Input")
    parser.add_argument("--backend", choices=("auto", "webkit_gtk", "pywebview"), default="auto")
    args = parser.parse_args(argv)
    parts = urlsplit(args.url)
    if parts.scheme != "https" or parts.hostname != TRUSTED_HOST:
        parser.error(f"--url 只允许 https://{TRUSTED_HOST}（收到 {args.url}）")
    return args


def choose_backend(requested: str, platform: str) -> str:
    if requested != "auto":
        return requested
    return "webkit_gtk" if platform.startswith("linux") else "pywebview"


def _run_webkit_gtk(args: argparse.Namespace) -> int:
    from _window import webkit_gtk

    return webkit_gtk.run(args)


def _run_pywebview(args: argparse.Namespace) -> int:
    from _window import pywebview_host

    return pywebview_host.run(args)


BACKENDS: Mapping[str, Backend] = {"webkit_gtk": _run_webkit_gtk, "pywebview": _run_pywebview}


def main(argv: Sequence[str] | None = None, *, backends: Mapping[str, Backend] = BACKENDS) -> int:
    args = parse_args(argv)
    backend = choose_backend(args.backend, sys.platform)
    try:
        return backends[backend](args)
    except BackendMissing as exc:
        print(f"主界面无法打开：{exc}", file=sys.stderr, flush=True)
        return EXIT_BACKEND_MISSING


if __name__ == "__main__":
    raise SystemExit(main())
