"""OCR 行分块与软/硬换行判定（逻辑层）：同列紧邻的行成一块，块内逐行标明是否为自动折行续行。

规则（docs/plans/v3.3-overlay-voice-hotkey-plan.md 4.2）：
- 成块：先把横向范围重叠（同列）、行高相近、上下紧邻的行连成列；「行距正常」以该列自己最紧的行距为准，
  明显大于它的空白（段间空行）把列切成块。表格各列、双栏因此各自成块，字号明显不同的标题另起一块；
  菜单、表格的行距再宽，只要整列均匀就同属一块。
- 软换行：下一行续入上一行所在段须同时满足——左缘对齐（或悬挂缩进：段首词是项目符号/编号时对齐上一行
  第二个词）；下一行首词放不进上一行剩余宽度（宽度以块自身最右缘为准，不以选区为准）；上一行至少 3 个词或
  12 个中文字。否则为硬换行（代码行、菜单项因此各自成行）。
- 等宽块（O1 执行中裁定）：成块后，块内长度≥4 的非中日韩词至少 3 个且其字宽中位偏差很小的块是代码、终端或日志，
  整块逐行硬换行（只看几何的软换行规则会把「最长行 + 同缩进下一行」误判为折行）。
已知限制：居中排版的多行标题按硬换行逐行排；等宽字体里的自然段逐行翻译。
"""
from __future__ import annotations

import re
import statistics
from collections.abc import Sequence
from dataclasses import dataclass

from _contracts import OcrTextLine
from _ocr_text import is_cjk_or_fullwidth

HEIGHT_RATIO_MAX = 1.4  # 同列两行行框高度比上限（大于即字号不同，如标题与正文）
# 同列上下两行的行间空白上限（较高行框高度的倍数）：正文行距约 0.5–0.9、菜单与表格行约 1.3–1.9、
# 段间空一行约 1.8–2.1；超过即不同的版面区域
COLUMN_GAP_MAX_RATIO = 2.5
OVERLAP_MIN_RATIO = -0.5  # 行间空白下限（负值 = 行框可轻微重叠；再低则是同一行）
# 段间空白：大于列内最紧行间空白的这一倍数、且至少多出较高行框高度的这一比例，才算分段
BREAK_GAP_RATIO = 1.5
BREAK_GAP_SLACK = 0.3
ALIGN_TOLERANCE_CHARS = 0.6  # 左缘对齐容差（字宽倍数）
MIN_WRAP_WORDS = 3
MIN_WRAP_CJK_CHARS = 12
# 等宽块判定：每词字宽 =（词框宽度）/ 字符数。实测（PIL，DejaVu 14px，长度≥4 的词）中位相对偏差：
# 等宽字体 0.2–0.3%（最大 0.8%），比例字体正文 12.8%、比例字体代码词 7.4%；OCR 词框 ±1px 抖动下仍可分开
MONO_MIN_WORD_LEN = 4
MONO_MIN_WORDS = 3
MONO_MAX_DEVIATION = 0.04

_BULLET_RE = re.compile(r"^(?:[-*+•·◦▪‣●○■□–—]|\(?\d{1,3}[.)、]|\(?[A-Za-z][.)]|[一二三四五六七八九十]+、)$")


@dataclass(frozen=True)
class LineSpan:
    """一条可见行（同一 Tesseract 行内按大间距分列后的一段），坐标为原图像素。"""

    x0: int
    y0: int
    x1: int
    y1: int
    text: str
    words: tuple[tuple[int, int, str], ...]  # (x0, x1, 词)，判定「首词放不放得进」用

    @property
    def height(self) -> int:
        return max(1, self.y1 - self.y0)

    @property
    def char_w(self) -> float:
        return max(1.0, (self.x1 - self.x0) / max(1, len(self.text)))


def _heights_similar(a: LineSpan, b: LineSpan) -> bool:
    return max(a.height, b.height) <= HEIGHT_RATIO_MAX * min(a.height, b.height)


def _gap(upper: LineSpan, lower: LineSpan) -> int:
    return lower.y0 - upper.y1


def _stacked(upper: LineSpan, lower: LineSpan) -> bool:
    gap = _gap(upper, lower)
    return OVERLAP_MIN_RATIO * min(upper.height, lower.height) <= gap <= COLUMN_GAP_MAX_RATIO * max(upper.height, lower.height)


def _overlap(x0: int, x1: int, line: LineSpan) -> int:
    return min(x1, line.x1) - max(x0, line.x0)


def _columns(spans: Sequence[LineSpan]) -> list[list[LineSpan]]:
    """按从上到下的顺序把每行接到「末行在其正上方、行高相近」且横向重叠最多的列；没有就另起一列。"""
    columns: list[list[LineSpan]] = []
    ranges: list[tuple[int, int]] = []
    for line in sorted(spans, key=lambda s: (s.y0, s.x0)):
        best, best_overlap = None, 0
        for index, column in enumerate(columns):
            last = column[-1]
            overlap = _overlap(*ranges[index], line)
            if overlap > best_overlap and _heights_similar(last, line) and _stacked(last, line):
                best, best_overlap = index, overlap
        if best is None:
            columns.append([line])
            ranges.append((line.x0, line.x1))
        else:
            columns[best].append(line)
            x0, x1 = ranges[best]
            ranges[best] = (min(x0, line.x0), max(x1, line.x1))
    return columns


def _split_paragraph_gaps(column: list[LineSpan]) -> list[list[LineSpan]]:
    """以列内最紧的行间空白为正常行距，明显更大的空白处切块。"""
    if len(column) < 2:
        return [column]
    normal = max(0, min(_gap(a, b) for a, b in zip(column, column[1:])))
    blocks = [[column[0]]]
    for upper, lower in zip(column, column[1:]):
        limit = BREAK_GAP_RATIO * normal + BREAK_GAP_SLACK * max(upper.height, lower.height)
        if _gap(upper, lower) > limit:
            blocks.append([lower])
        else:
            blocks[-1].append(lower)
    return blocks


def _group_blocks(spans: Sequence[LineSpan]) -> list[list[LineSpan]]:
    blocks = [block for column in _columns(spans) for block in _split_paragraph_gaps(column)]
    return sorted(blocks, key=lambda block: (block[0].y0, block[0].x0))


def _has_cjk(text: str) -> bool:
    return any(is_cjk_or_fullwidth(ch) for ch in text)


def _long_enough(line: LineSpan) -> bool:
    latin_words = sum(1 for _x0, _x1, word in line.words if not _has_cjk(word))
    cjk_chars = sum(1 for ch in line.text if is_cjk_or_fullwidth(ch))
    return latin_words >= MIN_WRAP_WORDS or cjk_chars >= MIN_WRAP_CJK_CHARS


def _monospace(block: Sequence[LineSpan]) -> bool:
    widths = [
        (x1 - x0) / len(word)
        for line in block
        for x0, x1, word in line.words
        if len(word) >= MONO_MIN_WORD_LEN and not _has_cjk(word)
    ]
    if len(widths) < MONO_MIN_WORDS:
        return False
    median = statistics.median(widths)
    return statistics.median(abs(width - median) / median for width in widths) <= MONO_MAX_DEVIATION


def _aligned(segment_first: LineSpan, upper: LineSpan, lower: LineSpan) -> bool:
    tolerance = ALIGN_TOLERANCE_CHARS * upper.char_w
    if abs(lower.x0 - upper.x0) <= tolerance:
        return True
    hanging = upper is segment_first and len(upper.words) >= 2 and _BULLET_RE.match(upper.words[0][2]) is not None
    return hanging and abs(lower.x0 - upper.words[1][0]) <= tolerance


def _first_word_fits(upper: LineSpan, lower: LineSpan, right: int) -> bool:
    """下一行首词（中日韩取首字）连同分隔空格能否放进上一行到块右缘的剩余宽度。"""
    w_x0, w_x1, word = lower.words[0]
    width = (w_x1 - w_x0) / len(word) if is_cjk_or_fullwidth(word[0]) else w_x1 - w_x0
    seam_cjk = is_cjk_or_fullwidth(upper.text[-1]) or is_cjk_or_fullwidth(word[0])
    space = 0.0 if seam_cjk else upper.char_w
    return width + space <= right - upper.x1


def _to_lines(block: list[LineSpan]) -> list[OcrTextLine]:
    right = max(line.x1 for line in block)
    preformatted = _monospace(block)
    lines: list[OcrTextLine] = []
    segment_first = block[0]
    for index, line in enumerate(block):
        soft = False
        if index > 0 and not preformatted:
            upper = block[index - 1]
            soft = (
                _long_enough(upper)
                and _aligned(segment_first, upper, line)
                and not _first_word_fits(upper, line, right)
            )
        if not soft:
            segment_first = line
        lines.append(
            OcrTextLine(bbox=(line.x0, line.y0, line.x1 - line.x0, line.height), text=line.text, soft_wrap=soft)
        )
    return lines


def segment_lines(spans: Sequence[LineSpan]) -> list[list[OcrTextLine]]:
    """行分块 + 逐行软/硬换行；块按首行位置从上到下、从左到右排列。"""
    return [_to_lines(block) for block in _group_blocks(spans)]


__all__ = ["LineSpan", "segment_lines"]
