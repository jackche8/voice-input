"""语音一体底栏的字幕断行纯函数与几何常量（UI 验收标准 docs/design/v3.3/voice-a.html）。

单一职责：
1. `wrap_lines`：用 `QTextLayout` 对任意文本做像素级断行，并把 Qt UTF-16 code unit 偏移
   精确映射回 Python Unicode 码点下标（兼容 Emoji 与 CJK 扩展区代理对），供字幕与原位排版共用；
2. `compute_trailing_two_lines`：对 `stable_text + tail_text` 断行后只取最新末尾两行，
   并按 stable / tail 分界切开每行；
3. 被动悬浮窗的窗口标志与底栏几何常量。不含窗口类与绘制。
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont, QGuiApplication, QTextLayout, QTextOption

HUD_BOTTOM_MARGIN_PX = 28
CAPTION_MIN_WIDTH_PX = 480
CAPTION_MAX_WIDTH_PX = 760
CAPTION_PAD_X_PX = 22
CAPTION_PAD_Y_PX = 12
CAPTION_LINE_GAP_PX = 6
CAPTION_AUTO_HIDE_MS = 1500

PASSIVE_WINDOW_FLAGS = (
    Qt.WindowType.FramelessWindowHint
    | Qt.WindowType.WindowStaysOnTopHint
    | Qt.WindowType.Tool
    | Qt.WindowType.WindowDoesNotAcceptFocus
    | Qt.WindowType.WindowTransparentForInput
)


def passive_overlay_flags() -> Qt.WindowType:
    """被动浮窗标志：Linux xcb 下启用 BypassWindowManagerHint，杜绝 Mutter 堆叠闪烁与坐标漂移。"""
    flags = PASSIVE_WINDOW_FLAGS
    if (QGuiApplication.platformName() or "").lower() == "xcb":
        flags |= Qt.WindowType.BypassWindowManagerHint
    return flags


def normalize_single_line(text: str) -> str:
    """将换行符归一化为空格，使单次语音流式文本按字幕栏宽度连续自动断行。"""
    if not text:
        return ""
    return text.replace("\r\n", " ").replace("\n", " ").replace("\r", " ")


def _build_utf16_to_codepoint_map(text: str) -> list[int]:
    """构建 Qt QString UTF-16 code unit 偏移到 Python str 码点下标的精确映射表。"""
    u16_to_py: list[int] = []
    for py_idx, ch in enumerate(text):
        u16_to_py.append(py_idx)
        if ord(ch) > 0xFFFF:
            # 补充平面字符在 UTF-16 中占高低代理对（2 个 code unit）
            u16_to_py.append(py_idx)
    u16_to_py.append(len(text))
    return u16_to_py


def wrap_lines(text: str, font: QFont, width: float) -> list[tuple[int, int]]:
    """按像素宽度断行（词边界优先、必要时任意处），返回非空白行的 Python 码点区间 [start, end)。"""
    if not text.strip():
        return []
    u16_to_py = _build_utf16_to_codepoint_map(text)
    max_u16 = len(u16_to_py) - 1

    option = QTextOption()
    option.setWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
    layout = QTextLayout(text, font)
    layout.setTextOption(option)
    layout.beginLayout()
    spans: list[tuple[int, int]] = []
    while True:
        line = layout.createLine()
        if not line.isValid():
            break
        line.setLineWidth(max(1.0, float(width)))
        u16_start = max(0, min(line.textStart(), max_u16))
        u16_end = max(u16_start, min(line.textStart() + line.textLength(), max_u16))
        py_start = u16_to_py[u16_start]
        py_end = u16_to_py[u16_end]
        if py_end > py_start and text[py_start:py_end].strip():
            spans.append((py_start, py_end))
    layout.endLayout()
    return spans


def compute_trailing_two_lines(
    stable_text: str,
    tail_text: str,
    max_line_width: int,
    font: QFont,
) -> list[tuple[str, str]]:
    """对 stable_text + tail_text 执行像素级断行，严格只返回最新末尾两行 (stable_seg, tail_seg)。"""
    stable_norm = normalize_single_line(stable_text)
    combined = stable_norm + normalize_single_line(tail_text)
    stable_end = len(stable_norm)
    result: list[tuple[str, str]] = []
    for start, end in wrap_lines(combined, font, max(40, int(max_line_width)))[-2:]:
        s_end = min(end, stable_end)
        t_start = max(start, stable_end)
        result.append((combined[min(start, stable_end):s_end], combined[t_start:max(end, stable_end)]))
    return result
