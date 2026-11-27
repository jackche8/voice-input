"""OCR 原位排版提取、顺序质量早停识别与局部环形背景色采样（逻辑层原子能力）。

基于 Tesseract `image_to_data` 提取词级框，扣减预处理留白、除以放大倍数还原原图坐标，
过滤低置信度噪点，同行水平大间距分列成可见行；行分块与软/硬换行交给 `_ocr_segment`，
块的文字与全文由 `_ocr_text` 派生；支持单次草稿试识别与 1~3 条启用预设的顺序质量早停 +
全未达标补跑原图择优；并仅对最终胜出结果采样原图 `orig_rgb` 外围 2~5px 环形底色。
"""
from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

import numpy as np

from _contracts import (
    OCR_PREPROCESS_MAX_ENABLED,
    ErrorCode,
    LogicError,
    OcrAttemptSummary,
    OcrPreprocessParams,
    OcrPreprocessPreset,
    OcrRunMeta,
    OcrRunMode,
    OcrTextBlock,
    OcrTextLine,
)
from _ocr import (
    DEFAULT_OCR_LANG,
    OCR_TIMEOUT_S,
    default_data_runner,
    ensure_ocr_languages,
    invoke_ocr_runner,
    raise_ocr_error,
)
from _ocr_preprocess import (
    OCR_BORDER_PX,
    OcrQualityScore,
    PreparedOcrBase,
    QualityWord,
    build_ocr_input_image,
    evaluate_ocr_quality,
    prepare_ocr_base,
)
from _ocr_segment import LineSpan, segment_lines
from _ocr_text import block_display_text, clean_cjk_spaces, is_cjk_or_fullwidth, join_block_texts

MIN_WORD_CONF = 20.0
MIN_NOISE_SYMBOL_CONF = 38.0
RING_INNER_PX = 2
RING_OUTER_PX = 5
DARK_FG_COLOR = "#0f172a"
LIGHT_FG_COLOR = "#f8fafc"
LUMA_THRESHOLD = 140.0
# 含中日韩字的行里，Tesseract 对部分词与标点（含被认成半角的逗号）给出约 1.5 倍高的异常词框（实测 16px 字：
# 正常 16px、异常 24–25.5px，上下各超出 2–7px）；这类行的行框纵向范围只取不超过中日韩词高中位数这一倍数的词。
# 纯拉丁文行不过滤：有无上伸/下伸字母的词高本来就差约 1.3 倍
CJK_BOX_HEIGHT_MAX_RATIO = 1.25

_ORIGINAL_LABEL = "默认原图"
_DRAFT_LABEL = "当前调参草稿"


@dataclass(frozen=True)
class OcrLayoutResult:
    """原位排版 OCR 完整结果（含坐标分块与执行链路元数据）。"""

    image_width: int
    image_height: int
    ocr_text: str
    blocks: list[OcrTextBlock]
    meta: OcrRunMeta


@dataclass(frozen=True)
class OcrTextResult:
    """纯文本 OCR 完整结果（复用顺序早停与质量评分引擎，跳过原位采色）。"""

    text: str
    meta: OcrRunMeta


@dataclass(frozen=True)
class _AttemptOutcome:
    preset_id: str | None
    label: str
    rank: int | None
    block_lines: list[list[OcrTextLine]]
    ocr_text: str
    quality: OcrQualityScore

    @property
    def has_text(self) -> bool:
        return bool(self.block_lines) and bool(self.ocr_text)

    @property
    def selection_key(self) -> tuple[float, int, float]:
        return (self.quality.score, self.quality.valid_chars, self.quality.weighted_conf)


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


def _is_single_symbol_noise(word: str) -> bool:
    if len(word) != 1:
        return False
    ch = word[0]
    return not (ch.isalnum() or is_cjk_or_fullwidth(ch))


def _parse_words(
    data: dict[str, list[Any]],
    *,
    orig_w: int,
    orig_h: int,
    border_px: int,
    scale: int,
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
            left, top = int(lefts[i]) - border_px, int(tops[i]) - border_px
            raw_x0, raw_y0 = round(left / scale), round(top / scale)
            raw_x1, raw_y1 = round((left + int(widths[i])) / scale), round((top + int(heights[i])) / scale)
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


def _vertical_extent(group: list[_WordBox]) -> tuple[int, int]:
    cjk_heights = [w.height for w in group if any(is_cjk_or_fullwidth(ch) for ch in w.text)]
    limit = CJK_BOX_HEIGHT_MAX_RATIO * float(np.median(cjk_heights)) if cjk_heights else float("inf")
    core = [w for w in group if w.height <= limit]
    return min(w.y0 for w in core), max(w.y1 for w in core)


def _split_line_columns(words: list[_WordBox]) -> list[LineSpan]:
    """将同一行内水平大间距的词拆分为独立列片段（每段一条可见行，带词框）。"""
    by_line: dict[tuple[int, int, int, int], list[_WordBox]] = {}
    for w in words:
        key = (w.page_num, w.block_num, w.par_num, w.line_num)
        by_line.setdefault(key, []).append(w)

    spans: list[LineSpan] = []
    for line_words in by_line.values():
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
            y0, y1 = _vertical_extent(grp)
            spans.append(
                LineSpan(
                    x0=min(item.x0 for item in grp),
                    y0=y0,
                    x1=max(item.x1 for item in grp),
                    y1=y1,
                    text=text,
                    words=tuple((item.x0, item.x1, item.text) for item in grp),
                )
            )
    return spans


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


def _confidence_int(quality: OcrQualityScore) -> int:
    return max(0, min(100, int(round(quality.weighted_conf))))


def _to_attempt_summary(outcome: _AttemptOutcome, *, accepted: bool) -> OcrAttemptSummary:
    return OcrAttemptSummary(
        preset_id=outcome.preset_id,
        label=outcome.label,
        rank=outcome.rank,
        confidence=_confidence_int(outcome.quality),
        score=outcome.quality.score,
        valid_chars=outcome.quality.valid_chars,
        accepted=accepted,
    )


def _run_attempt(
    base: PreparedOcrBase,
    *,
    params: OcrPreprocessParams | None,
    preset_id: str | None,
    label: str,
    rank: int | None,
    lang: str,
    runner: Callable[..., dict[str, list[Any]]],
) -> _AttemptOutcome:
    ocr_img, _otsu = build_ocr_input_image(base, params, border_px=OCR_BORDER_PX)
    try:
        raw_data = invoke_ocr_runner(runner, ocr_img, lang=lang, timeout_s=OCR_TIMEOUT_S)
    except Exception as exc:
        raise_ocr_error(exc)

    if not isinstance(raw_data, dict):
        raise LogicError(ErrorCode.ocr_failed, "OCR image_to_data 返回结构非法")

    orig_w, orig_h = base.orig_rgb.size
    words = _parse_words(raw_data, orig_w=orig_w, orig_h=orig_h, border_px=OCR_BORDER_PX, scale=base.scale)
    block_lines = segment_lines(_split_line_columns(words))
    if not block_lines:
        return _AttemptOutcome(
            preset_id=preset_id,
            label=label,
            rank=rank,
            block_lines=[],
            ocr_text="",
            quality=evaluate_ocr_quality((), ""),
        )

    ocr_text = join_block_texts([block_display_text(lines) for lines in block_lines])
    quality = evaluate_ocr_quality([QualityWord(w.text, w.conf) for w in words], ocr_text)
    return _AttemptOutcome(
        preset_id=preset_id,
        label=label,
        rank=rank,
        block_lines=block_lines,
        ocr_text=ocr_text,
        quality=quality,
    )


def extract_ocr_layout_with_meta(
    image_bytes: bytes,
    *,
    lang: str = DEFAULT_OCR_LANG,
    runner_data: Callable[..., dict[str, list[Any]]] | None = None,
    languages_fn: Callable[[], list[str]] | None = None,
    presets: Sequence[OcrPreprocessPreset] = (),
    preprocess_params: OcrPreprocessParams | None = None,
    clock: Callable[[], float] = time.monotonic,
    _sample_colors: bool = True,
) -> OcrLayoutResult:
    """唯一顺序质量早停与排版提取引擎。

    - 传入 `preprocess_params`：仅按该草稿参数执行 1 次试识别。
    - 0 条启用预设：严格只执行 1 次默认原图识别（零额外开销）。
    - 1~3 条启用预设：按顺位 1→N 预处理识别并评分，达标即早停；全未达标时补跑 1 次默认原图，
      按 `(score, valid_chars, weighted_conf)` 择优（同分保先序），全空抛 `ocr_failed`。
    - 硬错误（超时、缺语言包、Tesseract 缺失、非法图、启用超限）立即显式失败，不吞错继续。
    """
    if not image_bytes:
        raise LogicError(ErrorCode.invalid_request, "OCR 图像不能为空")

    enabled_presets = [preset for preset in presets if preset.enabled]
    if len(enabled_presets) > OCR_PREPROCESS_MAX_ENABLED:
        raise LogicError(
            ErrorCode.invalid_request,
            f"最多同时启用 {OCR_PREPROCESS_MAX_ENABLED} 个预处理预设，收到 {len(enabled_presets)} 个",
        )

    runner = runner_data if runner_data is not None else default_data_runner()
    ensure_ocr_languages(lang, languages_fn)

    started_at = clock()
    base = prepare_ocr_base(image_bytes)

    if preprocess_params is not None:
        outcome = _run_attempt(
            base,
            params=preprocess_params,
            preset_id=None,
            label=_DRAFT_LABEL,
            rank=None,
            lang=lang,
            runner=runner,
        )
        if not outcome.has_text:
            raise LogicError(ErrorCode.ocr_failed, "没有识别到文字")
        winner = outcome
        attempts = [_to_attempt_summary(outcome, accepted=True)]
        run_mode = OcrRunMode.draft_params
        early_stopped = False
        fallback_to_original = False
    elif not enabled_presets:
        outcome = _run_attempt(
            base,
            params=None,
            preset_id=None,
            label=_ORIGINAL_LABEL,
            rank=None,
            lang=lang,
            runner=runner,
        )
        if not outcome.has_text:
            raise LogicError(ErrorCode.ocr_failed, "没有识别到文字")
        winner = outcome
        attempts = [_to_attempt_summary(outcome, accepted=True)]
        run_mode = OcrRunMode.original
        early_stopped = False
        fallback_to_original = False
    else:
        outcomes: list[_AttemptOutcome] = []
        winner_idx: int | None = None
        early_stopped = False
        fallback_to_original = False

        for rank, preset in enumerate(enabled_presets, start=1):
            outcome = _run_attempt(
                base,
                params=preset,
                preset_id=preset.id,
                label=preset.name,
                rank=rank,
                lang=lang,
                runner=runner,
            )
            outcomes.append(outcome)
            if outcome.has_text and outcome.quality.passed:
                winner_idx = len(outcomes) - 1
                early_stopped = True
                break

        if winner_idx is None:
            fallback_to_original = True
            outcomes.append(
                _run_attempt(
                    base,
                    params=None,
                    preset_id=None,
                    label=_ORIGINAL_LABEL,
                    rank=None,
                    lang=lang,
                    runner=runner,
                )
            )
            best_key: tuple[float, int, float] | None = None
            for idx, item in enumerate(outcomes):
                if not item.has_text:
                    continue
                key = item.selection_key
                if best_key is None or key > best_key:
                    best_key = key
                    winner_idx = idx
            if winner_idx is None:
                raise LogicError(ErrorCode.ocr_failed, "没有识别到文字")

        winner = outcomes[winner_idx]
        attempts = [
            _to_attempt_summary(item, accepted=(idx == winner_idx))
            for idx, item in enumerate(outcomes)
        ]
        run_mode = OcrRunMode.preset_chain

    elapsed_ms = max(0, int(round((clock() - started_at) * 1000.0)))
    meta = OcrRunMeta(
        run_mode=run_mode,
        matched_preset_id=winner.preset_id,
        matched_preset_name=winner.label,
        matched_rank=winner.rank,
        confidence=_confidence_int(winner.quality),
        elapsed_ms=elapsed_ms,
        early_stopped=early_stopped,
        fallback_to_original=fallback_to_original,
        attempts=attempts,
    )

    orig_w, orig_h = base.orig_rgb.size
    if _sample_colors:
        rgb_arr = np.asarray(base.orig_rgb, dtype=np.uint8)
        blocks = [_block(lines, rgb_arr) for lines in winner.block_lines]
    else:
        blocks = []

    return OcrLayoutResult(
        image_width=orig_w,
        image_height=orig_h,
        ocr_text=winner.ocr_text,
        blocks=blocks,
        meta=meta,
    )


def extract_ocr_text_with_meta(
    image_bytes: bytes,
    *,
    lang: str = DEFAULT_OCR_LANG,
    runner_data: Callable[..., dict[str, list[Any]]] | None = None,
    languages_fn: Callable[[], list[str]] | None = None,
    presets: Sequence[OcrPreprocessPreset] = (),
    preprocess_params: OcrPreprocessParams | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> OcrTextResult:
    """复走同一顺序早停引擎提取全文与元数据（跳过原位贴片采色）。"""
    result = extract_ocr_layout_with_meta(
        image_bytes,
        lang=lang,
        runner_data=runner_data,
        languages_fn=languages_fn,
        presets=presets,
        preprocess_params=preprocess_params,
        clock=clock,
        _sample_colors=False,
    )
    return OcrTextResult(text=result.ocr_text, meta=result.meta)


def extract_ocr_layout(
    image_bytes: bytes,
    *,
    lang: str = DEFAULT_OCR_LANG,
    runner_data: Callable[..., dict[str, list[Any]]] | None = None,
    languages_fn: Callable[[], list[str]] | None = None,
    presets: Sequence[OcrPreprocessPreset] = (),
    preprocess_params: OcrPreprocessParams | None = None,
) -> tuple[int, int, str, list[OcrTextBlock]]:
    """提取原图宽高、全文 OCR 文本（块间空一行）及带行列表与局部背景/前景色的分块列表。"""
    result = extract_ocr_layout_with_meta(
        image_bytes,
        lang=lang,
        runner_data=runner_data,
        languages_fn=languages_fn,
        presets=presets,
        preprocess_params=preprocess_params,
    )
    return result.image_width, result.image_height, result.ocr_text, result.blocks


def _block(lines: list[OcrTextLine], rgb_arr: np.ndarray) -> OcrTextBlock:
    x0 = min(line.bbox[0] for line in lines)
    y0 = min(line.bbox[1] for line in lines)
    x1 = max(line.bbox[0] + line.bbox[2] for line in lines)
    y1 = max(line.bbox[1] + line.bbox[3] for line in lines)
    bbox = (x0, y0, x1 - x0, y1 - y0)
    bg_color, fg_color = sample_ring_colors(rgb_arr, bbox)
    return OcrTextBlock(bbox=bbox, lines=lines, ocr_text=block_display_text(lines), bg_color=bg_color, fg_color=fg_color)


__all__ = [
    "DARK_FG_COLOR",
    "LIGHT_FG_COLOR",
    "LUMA_THRESHOLD",
    "MIN_WORD_CONF",
    "RING_INNER_PX",
    "RING_OUTER_PX",
    "OcrLayoutResult",
    "OcrTextResult",
    "extract_ocr_layout",
    "extract_ocr_layout_with_meta",
    "extract_ocr_text_with_meta",
    "sample_ring_colors",
]
