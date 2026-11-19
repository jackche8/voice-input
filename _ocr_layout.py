"""OCR 原位排版提取与局部环形背景色采样（逻辑层原子能力）。

基于 Tesseract `image_to_data` 提取词级框，扣减 12px 预处理留白还原原图坐标，
过滤低置信度噪点，同行水平大间距分列、同段相邻行合并为语义连贯文字块，
并采样每块外围 2~5px 环形像素各通道中位数计算不透明背景色与高对比度前景色。
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import numpy as np
from PIL import Image

from _contracts import ErrorCode, LogicError, OcrTextBlock
from _ocr import (
    DEFAULT_OCR_LANG,
    OCR_BORDER_PX,
    OCR_TIMEOUT_S,
    clean_cjk_spaces,
    ensure_ocr_languages,
    invoke_ocr_runner,
    prepare_ocr_image,
    raise_ocr_error,
)

MIN_WORD_CONF = 20.0
MIN_NOISE_SYMBOL_CONF = 38.0
RING_INNER_PX = 2
RING_OUTER_PX = 5
DARK_FG_COLOR = "#0f172a"
LIGHT_FG_COLOR = "#f8fafc"
LUMA_THRESHOLD = 140.0


@dataclass(frozen=True)
class _WordBox:
    page_num: int
    block_num: int
    par_num: int
    line_num: int
    x0: int
    y0: int
    x1: int
    y1: int
    conf: float
    text: str

    @property
    def height(self) -> int:
        return max(1, self.y1 - self.y0)


@dataclass
class _LineSpan:
    page_num: int
    block_num: int
    par_num: int
    x0: int
    y0: int
    x1: int
    y1: int
    line_h: float
    text: str


def _is_cjk_or_fullwidth(ch: str) -> bool:
    if not ch:
        return False
    code = ord(ch)
    return (
        0x4E00 <= code <= 0x9FFF
        or 0x3400 <= code <= 0x4DBF
        or 0x3000 <= code <= 0x303F
        or 0xFF00 <= code <= 0xFFEF
    )


def _is_single_symbol_noise(word: str) -> bool:
    if len(word) != 1:
        return False
    ch = word[0]
    return not (ch.isalnum() or _is_cjk_or_fullwidth(ch))


def _join_adjacent_lines(upper: str, lower: str) -> str:
    if not upper:
        return lower
    if not lower:
        return upper
    if _is_cjk_or_fullwidth(upper[-1]) or _is_cjk_or_fullwidth(lower[0]):
        return clean_cjk_spaces(upper + lower)
    return clean_cjk_spaces(f"{upper} {lower}")


def _parse_words(
    data: dict[str, list[Any]],
    *,
    orig_w: int,
    orig_h: int,
    border_px: int,
) -> list[_WordBox]:
    texts = data.get("text") or []
    n = len(texts)
    if n == 0:
        return []
    confs = data.get("conf") or [-1] * n
    lefts = data.get("left") or [0] * n
    tops = data.get("top") or [0] * n
    widths = data.get("width") or [0] * n
    heights = data.get("height") or [0] * n
    pages = data.get("page_num") or [1] * n
    blocks = data.get("block_num") or [1] * n
    pars = data.get("par_num") or [1] * n
    lines = data.get("line_num") or [1] * n
    levels = data.get("level")

    words: list[_WordBox] = []
    for i in range(n):
        if levels is not None and i < len(levels):
            try:
                if int(levels[i]) != 5:
                    continue
            except (TypeError, ValueError):
                pass
        raw_word = str(texts[i] or "").strip()
        if not raw_word:
            continue
        try:
            conf = float(confs[i])
        except (TypeError, ValueError):
            conf = -1.0
        if conf < MIN_WORD_CONF:
            continue
        if _is_single_symbol_noise(raw_word) and conf < MIN_NOISE_SYMBOL_CONF:
            continue
        try:
            raw_x0 = int(lefts[i]) - border_px
            raw_y0 = int(tops[i]) - border_px
            raw_x1 = raw_x0 + int(widths[i])
            raw_y1 = raw_y0 + int(heights[i])
        except (TypeError, ValueError):
            continue
        x0 = max(0, min(orig_w, raw_x0))
        y0 = max(0, min(orig_h, raw_y0))
        x1 = max(0, min(orig_w, raw_x1))
        y1 = max(0, min(orig_h, raw_y1))
        if x1 <= x0 or y1 <= y0:
            continue
        words.append(
            _WordBox(
                page_num=int(pages[i]) if i < len(pages) else 1,
                block_num=int(blocks[i]) if i < len(blocks) else 1,
                par_num=int(pars[i]) if i < len(pars) else 1,
                line_num=int(lines[i]) if i < len(lines) else 1,
                x0=x0,
                y0=y0,
                x1=x1,
                y1=y1,
                conf=conf,
                text=raw_word,
            )
        )
    return words


def _split_line_columns(words: list[_WordBox]) -> list[_LineSpan]:
    """将同一行内水平大间距的词拆分为独立列片段。"""
    by_line: dict[tuple[int, int, int, int], list[_WordBox]] = {}
    for w in words:
        key = (w.page_num, w.block_num, w.par_num, w.line_num)
        by_line.setdefault(key, []).append(w)

    spans: list[_LineSpan] = []
    for (page_n, block_n, par_n, _line_n), line_words in by_line.items():
        ordered = sorted(line_words, key=lambda item: (item.x0, item.y0))
        groups: list[list[_WordBox]] = [[ordered[0]]]
        for w in ordered[1:]:
            prev = groups[-1][-1]
            gap = w.x0 - prev.x1
            ref_h = max(prev.height, w.height, 8)
            if gap > max(ref_h * 2.2, 28.0):
                groups.append([w])
            else:
                groups[-1].append(w)
        for grp in groups:
            text = clean_cjk_spaces(" ".join(item.text for item in grp))
            if not text:
                continue
            x0 = min(item.x0 for item in grp)
            y0 = min(item.y0 for item in grp)
            x1 = max(item.x1 for item in grp)
            y1 = max(item.y1 for item in grp)
            avg_h = float(np.median([item.height for item in grp]))
            spans.append(
                _LineSpan(
                    page_num=page_n,
                    block_num=block_n,
                    par_num=par_n,
                    x0=x0,
                    y0=y0,
                    x1=x1,
                    y1=y1,
                    line_h=max(1.0, avg_h),
                    text=text,
                )
            )
    return spans


def _can_merge_spans(upper: _LineSpan, lower: _LineSpan) -> bool:
    if (upper.page_num, upper.block_num, upper.par_num) != (
        lower.page_num,
        lower.block_num,
        lower.par_num,
    ):
        return False
    max_h = max(upper.line_h, lower.line_h)
    min_h = max(1.0, min(upper.line_h, lower.line_h))
    if max_h > min_h * 1.8:
        return False
    v_gap = lower.y0 - upper.y1
    if v_gap < -0.5 * max_h or v_gap > max(max_h * 1.25, 16.0):
        return False
    h_overlap = min(upper.x1, lower.x1) - max(upper.x0, lower.x0)
    left_aligned = abs(lower.x0 - upper.x0) <= max_h * 2.5
    return h_overlap > 0 or left_aligned


def _merge_paragraph_lines(spans: list[_LineSpan]) -> list[_LineSpan]:
    """将同段落内垂直紧邻、同列对齐的相邻行合并为语义连贯文字块。"""
    if not spans:
        return []
    ordered = sorted(
        spans,
        key=lambda s: (s.page_num, s.block_num, s.par_num, s.y0, s.x0),
    )
    merged: list[_LineSpan] = []
    for span in ordered:
        target: _LineSpan | None = None
        for candidate in reversed(merged):
            if (candidate.page_num, candidate.block_num, candidate.par_num) != (
                span.page_num,
                span.block_num,
                span.par_num,
            ):
                break
            if _can_merge_spans(candidate, span):
                target = candidate
                break
        if target is None:
            merged.append(span)
        else:
            target.x0 = min(target.x0, span.x0)
            target.y0 = min(target.y0, span.y0)
            target.x1 = max(target.x1, span.x1)
            target.y1 = max(target.y1, span.y1)
            target.line_h = (target.line_h + span.line_h) / 2.0
            target.text = _join_adjacent_lines(target.text, span.text)
    merged.sort(key=lambda s: (s.y0, s.x0))
    return merged


def sample_ring_colors(
    rgb_arr: np.ndarray,
    bbox: tuple[int, int, int, int],
) -> tuple[str, str]:
    """采样 bbox 外围 2~5px 环形像素各通道中位数得 bg_color，按感知亮度 L<140 选 fg_color。"""
    img_h, img_w = rgb_arr.shape[:2]
    x, y, w, h = bbox
    x1 = x + w
    y1 = y + h

    ox0 = max(0, x - RING_OUTER_PX)
    oy0 = max(0, y - RING_OUTER_PX)
    ox1 = min(img_w, x1 + RING_OUTER_PX)
    oy1 = min(img_h, y1 + RING_OUTER_PX)

    ix0 = max(0, x - RING_INNER_PX)
    iy0 = max(0, y - RING_INNER_PX)
    ix1 = min(img_w, x1 + RING_INNER_PX)
    iy1 = min(img_h, y1 + RING_INNER_PX)

    patch = rgb_arr[oy0:oy1, ox0:ox1]
    mask = np.ones((oy1 - oy0, ox1 - ox0), dtype=bool)
    mask[iy0 - oy0 : iy1 - oy0, ix0 - ox0 : ix1 - ox0] = False
    ring = patch[mask]

    if ring.shape[0] == 0:
        # 紧贴全图四边裁剪导致 2~5px 外环为空时，退取外圈 1px 边缘像素中位数
        top = patch[0:1, :, :].reshape(-1, 3)
        bottom = patch[-1:, :, :].reshape(-1, 3)
        left = patch[:, 0:1, :].reshape(-1, 3)
        right = patch[:, -1:, :].reshape(-1, 3)
        ring = np.concatenate([top, bottom, left, right], axis=0)

    med = np.median(ring, axis=0)
    r = max(0, min(255, int(round(float(med[0])))))
    g = max(0, min(255, int(round(float(med[1])))))
    b = max(0, min(255, int(round(float(med[2])))))
    bg_color = f"#{r:02x}{g:02x}{b:02x}"
    luma = 0.299 * r + 0.587 * g + 0.114 * b
    fg_color = LIGHT_FG_COLOR if luma < LUMA_THRESHOLD else DARK_FG_COLOR
    return bg_color, fg_color


def extract_ocr_layout(
    image_bytes: bytes,
    *,
    lang: str = DEFAULT_OCR_LANG,
    runner_data: Callable[..., dict[str, list[Any]]] | None = None,
    languages_fn: Callable[[], list[str]] | None = None,
) -> tuple[int, int, str, list[OcrTextBlock]]:
    """提取原图宽高、全文 OCR 文本及带局部背景/前景色的分块坐标列表。"""
    if not image_bytes:
        raise LogicError(ErrorCode.invalid_request, "OCR 图像不能为空")

    runner = runner_data
    if runner is None:
        try:
            import pytesseract
        except ImportError as exc:
            raise LogicError(ErrorCode.ocr_failed, f"OCR 依赖不可用: {exc}") from exc

        def _default_data_runner(
            img: Image.Image, lang: str, timeout: int = OCR_TIMEOUT_S
        ) -> dict[str, list[Any]]:
            return pytesseract.image_to_data(
                img,
                lang=lang,
                output_type=pytesseract.Output.DICT,
                timeout=timeout,
            )

        runner = _default_data_runner

    ensure_ocr_languages(lang, languages_fn)
    orig_rgb, padded_rgb = prepare_ocr_image(image_bytes, border_px=OCR_BORDER_PX)
    orig_w, orig_h = orig_rgb.size

    try:
        raw_data = invoke_ocr_runner(runner, padded_rgb, lang=lang, timeout_s=OCR_TIMEOUT_S)
    except Exception as exc:
        raise_ocr_error(exc)

    if not isinstance(raw_data, dict):
        raise LogicError(ErrorCode.ocr_failed, "OCR image_to_data 返回结构非法")

    words = _parse_words(raw_data, orig_w=orig_w, orig_h=orig_h, border_px=OCR_BORDER_PX)
    spans = _merge_paragraph_lines(_split_line_columns(words))
    if not spans:
        raise LogicError(ErrorCode.ocr_failed, "没有识别到文字")

    rgb_arr = np.asarray(orig_rgb, dtype=np.uint8)
    blocks: list[OcrTextBlock] = []
    for span in spans:
        bbox = (span.x0, span.y0, max(1, span.x1 - span.x0), max(1, span.y1 - span.y0))
        bg_color, fg_color = sample_ring_colors(rgb_arr, bbox)
        blocks.append(
            OcrTextBlock(
                bbox=bbox,
                ocr_text=span.text,
                translated_text="",
                bg_color=bg_color,
                fg_color=fg_color,
            )
        )

    full_text = "\n".join(block.ocr_text for block in blocks).strip()
    if not full_text:
        raise LogicError(ErrorCode.ocr_failed, "没有识别到文字")
    return orig_w, orig_h, full_text, blocks


__all__ = [
    "DARK_FG_COLOR",
    "LIGHT_FG_COLOR",
    "LUMA_THRESHOLD",
    "MIN_WORD_CONF",
    "RING_INNER_PX",
    "RING_OUTER_PX",
    "extract_ocr_layout",
    "sample_ring_colors",
]
