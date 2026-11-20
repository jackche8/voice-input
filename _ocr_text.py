"""OCR 行数据的唯一文本派生（逻辑层）：显示/复制原文、翻译项（硬行段）、块译文、全文。

一份行数据（`OcrTextLine` 列表）派生全部文本，`_ocr_layout`、`_translate`、路由与 sandbox 只调用这里，
UI 不派生。规则见 docs/plans/v3.3-overlay-voice-hotkey-plan.md 4.2 与 8.6-B。
"""
from __future__ import annotations

import re
import statistics
from collections.abc import Sequence
from dataclasses import dataclass

from _contracts import ErrorCode, LogicError, OcrTextLine


# 中日韩统一表意文字（含扩展 A）、中日韩标点与全角字符：接缝两侧有它时不加空格，OCR 文本里它们之间的空白去掉
_CJK_CHAR_CLASS = "\u4e00-\u9fff\u3400-\u4dbf\u3000-\u303f\uff00-\uffef"
_CJK_RE = re.compile(f"[{_CJK_CHAR_CLASS}]")
# 紧挨中日韩字的半角标点（Tesseract 在中文行里常认成半角并在两侧补空格，如「權重 , 並」）：它与中日韩字之间的空白也去掉
_ASCII_PUNCT_CLASS = re.escape(",.:;!?")
_CJK_SPACE_RE = re.compile(
    rf"(?<=[{_CJK_CHAR_CLASS}])\s+(?=[{_CJK_CHAR_CLASS}{_ASCII_PUNCT_CLASS}])"
    rf"|(?<=[{_ASCII_PUNCT_CLASS}])\s+(?=[{_CJK_CHAR_CLASS}])"
)


def is_cjk_or_fullwidth(ch: str) -> bool:
    return bool(ch) and _CJK_RE.match(ch) is not None


def clean_cjk_spaces(text: str) -> str:
    """去掉中日韩字之间、以及中日韩字与紧挨的半角标点之间的空白；保留英文词间空格与换行结构。"""
    if not text:
        return ""
    lines = [_CJK_SPACE_RE.sub("", line).strip() for line in text.splitlines()]
    return "\n".join(lines).strip()


@dataclass(frozen=True)
class HardSegment:
    """硬行段 = 一条非 soft_wrap 行 + 其后连续的 soft_wrap 行；一个硬行段是一个翻译项。"""

    first_line: int  # 首行在 lines 中的下标（翻译排版的起排槽位）
    indent: int  # 首行缩进空格数（相对块左缘）
    text: str  # 软换行拼接，不含缩进


def _indents(lines: Sequence[OcrTextLine]) -> list[int]:
    """每行相对块左缘的缩进空格数：x 偏移 / 块内字宽中位数（字宽 = 行宽 / 字符数）。"""
    left = min(line.bbox[0] for line in lines)
    char_w = statistics.median(line.bbox[2] / len(line.text) for line in lines)
    return [round((line.bbox[0] - left) / char_w) for line in lines]


def _soft_join(upper: str, lower: str) -> str:
    if is_cjk_or_fullwidth(upper[-1]) or is_cjk_or_fullwidth(lower[0]):
        return upper + lower
    return f"{upper} {lower}"


def block_display_text(lines: Sequence[OcrTextLine]) -> str:
    """显示/复制原文：每条可见行一行，行首补相对块左缘的缩进。"""
    return "\n".join(" " * indent + line.text for indent, line in zip(_indents(lines), lines))


def block_segments(lines: Sequence[OcrTextLine]) -> list[HardSegment]:
    indents = _indents(lines)
    segments: list[HardSegment] = []
    for index, line in enumerate(lines):
        if line.soft_wrap and segments:
            last = segments[-1]
            segments[-1] = HardSegment(last.first_line, last.indent, _soft_join(last.text, line.text))
        else:
            segments.append(HardSegment(index, indents[index], line.text))
    return segments


def block_translated_text(lines: Sequence[OcrTextLine], translations: Sequence[str]) -> str:
    """块译文：第 k 条译文前补第 k 个硬行段的缩进，按段序逐行拼回（复制译文与屏上所见一致）。"""
    segments = block_segments(lines)
    if len(translations) != len(segments):
        raise LogicError(
            ErrorCode.translate_failed,
            f"译文条数 {len(translations)} 与硬行段数 {len(segments)} 不一致",
        )
    return "\n".join(" " * segment.indent + text for segment, text in zip(segments, translations))


def join_block_texts(texts: Sequence[str]) -> str:
    """全文：非空块文本之间空一行；原文全文与译文全文共用。"""
    return "\n\n".join(text for text in texts if text.strip())


__all__ = [
    "HardSegment",
    "block_display_text",
    "block_segments",
    "block_translated_text",
    "clean_cjk_spaces",
    "is_cjk_or_fullwidth",
    "join_block_texts",
]
