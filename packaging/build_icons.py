#!/usr/bin/env python3
"""单源矢量图标构建器：由 `resources/ollama-voice-input.svg` 确定性生成 Windows `.ico` 与 macOS `.icns`。

用法：
  python3 packaging/build_icons.py          # 重新生成 resources/ollama-voice-input.{ico,icns}
  python3 packaging/build_icons.py --check  # 校验磁盘产物与 SVG 单源派生字节完全一致（不一致返回非 0）
"""
from __future__ import annotations

import argparse
import os
import struct
import sys
import zlib
from collections.abc import Mapping, Sequence
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from _atomic_file import atomic_write_bytes  # noqa: E402

DEFAULT_SVG = ROOT / "resources" / "ollama-voice-input.svg"
DEFAULT_ICO = ROOT / "resources" / "ollama-voice-input.ico"
DEFAULT_ICNS = ROOT / "resources" / "ollama-voice-input.icns"

ICO_DIB_SIZES: tuple[int, ...] = (16, 20, 24, 32, 48, 64, 128)
ICO_PNG_SIZES: tuple[int, ...] = (256,)
ICO_SIZES: tuple[int, ...] = (*ICO_DIB_SIZES, *ICO_PNG_SIZES)

ICNS_CHUNKS: tuple[tuple[bytes, int], ...] = (
    (b"icp4", 16),
    (b"icp5", 32),
    (b"icp6", 64),
    (b"ic07", 128),
    (b"ic08", 256),
    (b"ic09", 512),
    (b"ic10", 1024),
    (b"ic11", 32),
    (b"ic12", 64),
    (b"ic13", 256),
    (b"ic14", 512),
)


def _png_chunk(tag: bytes, payload: bytes) -> bytes:
    return struct.pack(">I", len(payload)) + tag + payload + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF)


def encode_deterministic_png(rgba: bytes, size: int) -> bytes:
    """将未预乘 RGBA 像素（size × size × 4 字节）编码为确定性 8-bit RGBA PNG（无时间戳/元数据）。"""
    stride = size * 4
    expected = stride * size
    if len(rgba) != expected:
        raise ValueError(f"RGBA 缓冲区长度异常：期望 {expected}，收到 {len(rgba)}")
    raw = bytearray((stride + 1) * size)
    for y in range(size):
        dst = y * (stride + 1)
        src = y * stride
        raw[dst] = 0
        raw[dst + 1 : dst + 1 + stride] = rgba[src : src + stride]
    ihdr = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)
    idat = zlib.compress(bytes(raw), level=9)
    return b"\x89PNG\r\n\x1a\n" + _png_chunk(b"IHDR", ihdr) + _png_chunk(b"IDAT", idat) + _png_chunk(b"IEND", b"")


def encode_ico_dib(rgba: bytes, size: int) -> bytes:
    """编码 Windows ICO 32-bit 自底向上未预乘 BGRA DIB（biHeight = size * 2 + 4 字节对齐 1-bit AND 掩码）。"""
    stride = size * 4
    if len(rgba) != stride * size:
        raise ValueError(f"RGBA 缓冲区长度异常：期望 {stride * size}，收到 {len(rgba)}")
    xor_buf = bytearray(stride * size)
    and_stride = ((size + 31) // 32) * 4
    and_buf = bytearray(and_stride * size)

    for dst_row, src_y in enumerate(range(size - 1, -1, -1)):
        src_row_off = src_y * stride
        dst_row_off = dst_row * stride
        and_row_off = dst_row * and_stride
        for x in range(size):
            px = src_row_off + x * 4
            r, g, b, a = rgba[px], rgba[px + 1], rgba[px + 2], rgba[px + 3]
            out = dst_row_off + x * 4
            if a == 0:
                xor_buf[out : out + 4] = b"\x00\x00\x00\x00"
                and_buf[and_row_off + (x // 8)] |= 1 << (7 - (x % 8))
            else:
                xor_buf[out] = b
                xor_buf[out + 1] = g
                xor_buf[out + 2] = r
                xor_buf[out + 3] = a

    header = struct.pack(
        "<IiiHHIIiiII",
        40,
        size,
        size * 2,
        1,
        32,
        0,
        len(xor_buf) + len(and_buf),
        0,
        0,
        0,
        0,
    )
    return header + bytes(xor_buf) + bytes(and_buf)


def build_ico_bytes(rgba_frames: Mapping[int, bytes]) -> bytes:
    """打包多尺寸 Windows `.ico`：16..128 写入 32-bit BGRA DIB，256 写入确定性 PNG。"""
    payloads: list[tuple[int, bytes]] = []
    for size in ICO_SIZES:
        rgba = rgba_frames[size]
        payload = encode_deterministic_png(rgba, size) if size in ICO_PNG_SIZES else encode_ico_dib(rgba, size)
        payloads.append((size, payload))

    header = struct.pack("<HHH", 0, 1, len(payloads))
    directory = bytearray()
    offset = 6 + 16 * len(payloads)
    for size, payload in payloads:
        dim = 0 if size >= 256 else size
        directory.extend(struct.pack("<BBBBHHII", dim, dim, 0, 0, 1, 32, len(payload), offset))
        offset += len(payload)
    return header + bytes(directory) + b"".join(payload for _, payload in payloads)


def build_icns_bytes(rgba_frames: Mapping[int, bytes]) -> bytes:
    """打包多尺寸 macOS `.icns`（icp4/icp5/icp6/ic07..ic14 确定性 RGBA PNG 块）。"""
    png_cache: dict[int, bytes] = {}
    chunks: list[bytes] = []
    for tag, size in ICNS_CHUNKS:
        png = png_cache.get(size)
        if png is None:
            png = encode_deterministic_png(rgba_frames[size], size)
            png_cache[size] = png
        chunks.append(struct.pack(">4sI", tag, 8 + len(png)) + png)
    body = b"".join(chunks)
    return struct.pack(">4sI", b"icns", 8 + len(body)) + body


def render_svg_rgba_frames(svg_path: Path, sizes: Sequence[int]) -> dict[int, bytes]:
    """在 headless Qt（默认 QT_QPA_PLATFORM=offscreen）下用 QSvgRenderer 渲染未预乘 RGBA 帧。"""
    if not svg_path.is_file():
        raise FileNotFoundError(f"缺少矢量图标源：{svg_path}")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

    from PySide6.QtCore import QRectF
    from PySide6.QtGui import QGuiApplication, QImage, QPainter
    from PySide6.QtSvg import QSvgRenderer

    app = QGuiApplication.instance() or QGuiApplication([])
    del app
    renderer = QSvgRenderer(str(svg_path))
    if not renderer.isValid():
        raise RuntimeError(f"无效的 SVG 图标源：{svg_path}")

    frames: dict[int, bytes] = {}
    for size in sorted(set(sizes)):
        img = QImage(size, size, QImage.Format.Format_ARGB32)
        img.fill(0)
        painter = QPainter(img)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        renderer.render(painter, QRectF(0.0, 0.0, float(size), float(size)))
        painter.end()

        rgba_img = img.convertToFormat(QImage.Format.Format_RGBA8888)
        row_bytes = size * 4
        bpl = rgba_img.bytesPerLine()
        raw_view = memoryview(rgba_img.constBits()).cast("B")
        if bpl == row_bytes:
            frames[size] = bytes(raw_view[: row_bytes * size])
        else:
            out = bytearray(row_bytes * size)
            for y in range(size):
                out[y * row_bytes : (y + 1) * row_bytes] = raw_view[y * bpl : y * bpl + row_bytes]
            frames[size] = bytes(out)
    return frames


def generate_icon_bytes(svg_path: Path = DEFAULT_SVG) -> tuple[bytes, bytes]:
    all_sizes = sorted({*ICO_SIZES, *(size for _, size in ICNS_CHUNKS)})
    frames = render_svg_rgba_frames(svg_path, all_sizes)
    return build_ico_bytes(frames), build_icns_bytes(frames)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="从 resources/ollama-voice-input.svg 确定性生成 .ico 与 .icns")
    parser.add_argument("--svg", type=Path, default=DEFAULT_SVG)
    parser.add_argument("--ico", type=Path, default=DEFAULT_ICO)
    parser.add_argument("--icns", type=Path, default=DEFAULT_ICNS)
    parser.add_argument("--check", action="store_true", help="仅校验磁盘产物与 SVG 生成结果逐字节一致")
    args = parser.parse_args(argv)

    ico_bytes, icns_bytes = generate_icon_bytes(args.svg)
    if args.check:
        mismatches: list[str] = []
        for label, path, expected in (("ICO", args.ico, ico_bytes), ("ICNS", args.icns, icns_bytes)):
            if not path.is_file():
                mismatches.append(f"{label} 缺失：{path}")
            elif path.read_bytes() != expected:
                mismatches.append(f"{label} 与 SVG 源派生结果不一致：{path}（请运行 python3 packaging/build_icons.py 重新生成）")
        if mismatches:
            for msg in mismatches:
                print(msg, file=sys.stderr)
            return 1
        print(f"OK: {args.ico.name} ({len(ico_bytes)} B), {args.icns.name} ({len(icns_bytes)} B)")
        return 0

    atomic_write_bytes(args.ico, ico_bytes, 0o644)
    atomic_write_bytes(args.icns, icns_bytes, 0o644)
    print(f"Generated {args.ico} ({len(ico_bytes)} B) and {args.icns} ({len(icns_bytes)} B)")
    return 0


__all__ = [
    "DEFAULT_ICNS",
    "DEFAULT_ICO",
    "DEFAULT_SVG",
    "ICNS_CHUNKS",
    "ICO_DIB_SIZES",
    "ICO_PNG_SIZES",
    "ICO_SIZES",
    "build_icns_bytes",
    "build_ico_bytes",
    "encode_deterministic_png",
    "encode_ico_dib",
    "generate_icon_bytes",
    "main",
    "render_svg_rgba_frames",
]


if __name__ == "__main__":
    raise SystemExit(main())
