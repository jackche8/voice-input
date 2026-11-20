"""原位识别 / 翻译排版（UI 验收标准 docs/design/v3.3/overlay-a.html「排版规则」，方案 8.6-D）。

纯计算：输入一个 `OcrTextBlock`，输出要画的行（位置、字号、基线、字距）与贴片外框，坐标为图片像素。
- 原字号：按行内文字的实际墨迹换算——参考字号下这行文字的紧框高与行框高之比 × 参考字号，块内取中位数
  （一块字号统一）；基线逐行由紧框上沿换算（行框是墨迹框，高度随有无下行字母、括号、中文而变）。
- OCR 模式：每条可见行画回自己的行框；文字与原行相同，按原行宽调字距，字落在原字上。
- 翻译模式：`translated_text` 按 "\\n" 切出的第 k 行对应第 k 个硬行段（`lines` 中第 k 条非 soft_wrap 行），
  从该段首行槽位的 x 起排（行首缩进空格不画，槽位 x 已带缩进），原字号、原行距、块宽内折行；
  本段槽位放不下先缩字号（≥ OCR_MIN_FONT_PX），仍放不下才按原行距向下延伸，其后各段随之下移；
  行数与段数不等抛 ValueError（契约违例）。
不绘制、不派生全文。
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from PySide6.QtCore import QRectF
from PySide6.QtGui import QFont, QFontMetricsF

from _contracts import OcrTextBlock, OcrTextLine
from _ui_desktop import theme
from _ui_desktop.caption_bar import wrap_lines


@dataclass(frozen=True)
class LayoutRow:
    text: str
    x: float
    top: float  # 槽位顶
    height: float  # 槽位高
    baseline: float
    font_px: int
    letter_spacing: float  # 与原行文字相同的行按原行宽调整的字距（px），否则 0
    width: float  # 绘制宽度（含字距）


@dataclass(frozen=True)
class BlockLayout:
    rows: tuple[LayoutRow, ...]
    extent: QRectF  # 块框 ∪ 全部行；贴片在此基础上加内边距


def _reference_metrics() -> QFontMetricsF:
    return QFontMetricsF(theme.make_font(pixel_size=theme.OCR_REFERENCE_FONT_PX))


def block_font_px(lines: Sequence[OcrTextLine], reference: QFontMetricsF) -> int:
    """块的原字号：每行「行框高 / 参考字号下紧框高 × 参考字号」的中位数，不小于 OCR_MIN_FONT_PX。"""
    sizes = sorted(
        line.bbox[3] / ink.height() * theme.OCR_REFERENCE_FONT_PX
        for line in lines
        for ink in (reference.tightBoundingRect(line.text),)
        if ink.height() > 0
    )
    if not sizes:
        raise ValueError(f"块内各行都没有可测量的墨迹：{[line.text for line in lines]!r}")
    mid = len(sizes) // 2
    median = sizes[mid] if len(sizes) % 2 else (sizes[mid - 1] + sizes[mid]) / 2
    return max(theme.OCR_MIN_FONT_PX, round(median))


def _line_baseline(line: OcrTextLine, font_px: int, reference: QFontMetricsF) -> float:
    """该行基线：让这行文字在 font_px 下的紧框上沿对齐行框上沿。"""
    top = reference.tightBoundingRect(line.text).top()
    return line.bbox[1] - top * font_px / theme.OCR_REFERENCE_FONT_PX


def row_font(row: LayoutRow) -> QFont:
    """绘制该行所用字体（含字距）。"""
    font = theme.make_font(pixel_size=row.font_px)
    if row.letter_spacing:
        font.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, row.letter_spacing)
    return font


def _row(
    text: str,
    x: float,
    top: float,
    height: float,
    baseline: float,
    font_px: int,
    fit_width: float | None,
) -> LayoutRow:
    advance = QFontMetricsF(theme.make_font(pixel_size=font_px)).horizontalAdvance(text)
    gaps = len(text) - 1
    spacing = 0.0
    if fit_width is not None and gaps > 0:
        low, high = theme.OCR_FIT_SPACING_MIN * font_px, theme.OCR_FIT_SPACING_MAX * font_px
        spacing = max(low, min(high, (fit_width - advance) / gaps))
    return LayoutRow(text, x, top, height, baseline, font_px, spacing, advance + spacing * max(0, gaps))


def _extent(block: OcrTextBlock, rows: Sequence[LayoutRow]) -> QRectF:
    x, y, w, h = block.bbox
    rect = QRectF(x, y, w, h)
    for row in rows:
        rect = rect.united(QRectF(row.x, row.top, row.width, row.height))
    return rect


def layout_ocr_block(block: OcrTextBlock) -> BlockLayout:
    """OCR 模式：每条可见行画回原行框，全块同一原字号。"""
    reference = _reference_metrics()
    px = block_font_px(block.lines, reference)
    rows = tuple(
        _row(line.text, x, y, h, _line_baseline(line, px, reference), px, float(w))
        for line in block.lines
        for x, y, w, h in (line.bbox,)
    )
    return BlockLayout(rows, _extent(block, rows))


def _pitch(lines: Sequence[OcrTextLine]) -> float:
    """原行距：相邻行顶差的中位数；单行块取行高 × OCR_SINGLE_LINE_PITCH_RATIO。"""
    tops = [line.bbox[1] for line in lines]
    gaps = sorted(b - a for a, b in zip(tops, tops[1:]))
    if not gaps:
        return lines[0].bbox[3] * theme.OCR_SINGLE_LINE_PITCH_RATIO
    return float(gaps[len(gaps) // 2])


def _fit(text: str, width: float, start_px: int, slots: int) -> tuple[int, list[tuple[int, int]]]:
    """从原字号起逐级缩小，直到折行数不超过本段槽位数；到 OCR_MIN_FONT_PX 仍放不下则按最小字号返回。"""
    px = start_px
    while True:
        spans = wrap_lines(text, theme.make_font(pixel_size=px), width)
        if len(spans) <= slots or px <= theme.OCR_MIN_FONT_PX:
            return px, spans
        px -= 1


def _layout_segment(
    text: str,
    own: Sequence[OcrTextLine],
    baselines: Sequence[float],
    font_px: int,
    right: float,
    pitch: float,
    shift: float,
) -> tuple[list[LayoutRow], float]:
    """一个硬行段（own 为它的槽位，baselines 为各槽位原基线）：返回各行与它向下延伸的高度。"""
    head = own[0]
    x0, top0, w0, h0 = head.bbox
    if text == head.text and len(own) == 1:
        return [_row(text, x0, top0 + shift, h0, baselines[0] + shift, font_px, float(w0))], 0.0
    width = max(1.0, right - max(line.bbox[0] for line in own))
    px, spans = _fit(text, width, font_px, len(own))
    rows: list[LayoutRow] = []
    for j, (start, end) in enumerate(spans):
        slot = min(j, len(own) - 1)
        extra = max(0, j - slot) * pitch
        top = own[slot].bbox[1] + extra + shift
        baseline = baselines[slot] + extra + shift
        x = x0 if j == 0 else own[slot].bbox[0]
        rows.append(_row(text[start:end].strip(), x, top, own[slot].bbox[3], baseline, px, None))
    return rows, max(0, len(spans) - len(own)) * pitch


def layout_translated_block(block: OcrTextBlock) -> BlockLayout:
    """翻译模式：第 k 行译文从第 k 个硬行段的首行槽位起排；行数与段数不等抛 ValueError。"""
    lines = block.lines
    heads = [i for i, line in enumerate(lines) if not line.soft_wrap]
    if not heads or heads[0] != 0:
        raise ValueError(f"OcrTextBlock.lines 首行必须是硬行（soft_wrap=False），收到 {lines[0]!r}")
    texts = block.translated_text.split("\n")
    if len(texts) != len(heads):
        raise ValueError(f"译文 {len(texts)} 行与硬行段 {len(heads)} 段不符：{block.translated_text!r}")
    reference = _reference_metrics()
    px = block_font_px(lines, reference)
    baselines = [_line_baseline(line, px, reference) for line in lines]
    right = float(block.bbox[0] + block.bbox[2])
    pitch = _pitch(lines)
    rows: list[LayoutRow] = []
    shift = 0.0
    bounds = [*heads, len(lines)]
    for k, text in enumerate(texts):
        segment_text = text.lstrip(" ")
        if not segment_text:
            continue
        own = slice(bounds[k], bounds[k + 1])
        segment_rows, extra = _layout_segment(segment_text, lines[own], baselines[own], px, right, pitch, shift)
        rows.extend(segment_rows)
        shift += extra
    return BlockLayout(tuple(rows), _extent(block, rows))
