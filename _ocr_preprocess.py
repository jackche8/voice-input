"""OCR 图像预处理、同源预览渲染与词级质量评分（纯 Pillow/numpy 逻辑层）。

严格零依赖 `_ocr` 或 Tesseract 运行时，供 `_ocr`、`_ocr_layout` 与 `/api/v1/ocr/preview` 共用唯一像素管线与评分规则。
"""
from __future__ import annotations

import base64
import io
from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np
from PIL import Image, ImageEnhance, ImageFilter, ImageOps

from _contracts import (
    ErrorCode,
    LogicError,
    OcrBinarizeMode,
    OcrPreprocessParams,
    OcrPreprocessPreviewResponse,
)

OCR_BORDER_PX = 12
OCR_UPSCALE = 2
# 放大后像素上限：实测（默认 chi_sim+chi_tra+eng，tests/logic/ocr_samples.py）：
# 全部 11 项精度样本最大 1280×860（2× = 4,403,200 px，4.17s 全过）、1920×1080 整屏 2× = 8,294,400 px（2.53–5.76s）；
# 3440×1440 密集整屏 2× 需 10.25–10.40s（高负载易撞 15s 超时），降 1× 后 7.22s（-30.6%）且与 OCR_TIMEOUT_S=30 留足裕度。
OCR_UPSCALE_MAX_PIXELS = 1920 * 1080 * 4
OCR_MAX_IMAGE_PIXELS = 40_000_000
OCR_PREVIEW_MAX_EDGE = 1200

OCR_QUALITY_MIN_VALID_CHARS = 2
OCR_QUALITY_MIN_VALID_RATIO = 0.65
OCR_QUALITY_MIN_CONF = 75.0

_LIGHT_CANVAS_RGB = (255, 255, 255)  # #ffffff
_DARK_CANVAS_RGB = (17, 24, 39)      # #111827
_DARK_GRAY_FILL = 23
_FLOAT_EPS = 1e-6


@dataclass(frozen=True)
class PreparedOcrBase:
    """一次解码/合成/放大后的共享基图：orig_rgb 供原位采色，gray_scaled 供预览与多预设复用。"""

    orig_rgb: Image.Image
    gray_scaled: Image.Image
    default_border_fill: int
    scale: int


@dataclass(frozen=True)
class QualityWord:
    """参与质量评分的单个已过滤词条。"""

    text: str
    conf: float


@dataclass(frozen=True)
class OcrQualityScore:
    """单次识别输出的质量评分与早停达标判定。"""

    weighted_conf: float
    valid_chars: int
    non_ws_chars: int
    valid_ratio: float
    score: float
    passed: bool


def _perceptual_luma_rgb(rgb: np.ndarray) -> float:
    return float(
        0.299 * float(np.mean(rgb[:, 0]))
        + 0.587 * float(np.mean(rgb[:, 1]))
        + 0.114 * float(np.mean(rgb[:, 2]))
    )


def _edge_border_fill(rgb_img: Image.Image) -> tuple[int, int, int]:
    arr = np.asarray(rgb_img, dtype=np.uint8)
    if arr.size == 0:
        return _LIGHT_CANVAS_RGB
    edges = np.concatenate([arr[0, :, :], arr[-1, :, :], arr[:, 0, :], arr[:, -1, :]], axis=0)
    luma = _perceptual_luma_rgb(edges)
    return _DARK_CANVAS_RGB if luma < 140.0 else _LIGHT_CANVAS_RGB


def _gray_edge_mean(gray_img: Image.Image) -> float:
    arr = np.asarray(gray_img, dtype=np.uint8)
    if arr.size == 0:
        return 255.0
    edges = np.concatenate([arr[0, :], arr[-1, :], arr[:, 0], arr[:, -1]], axis=0)
    return float(np.mean(edges))


def prepare_ocr_base(image_bytes: bytes) -> PreparedOcrBase:
    """解码图像字节并完成透明底合成、灰度转换与 2×/1× 放大（不加留白）。

    空字节、损坏/非图像数据或像素超 OCR_MAX_IMAGE_PIXELS 一律抛 LogicError(invalid_request)。
    """
    if not image_bytes:
        raise LogicError(ErrorCode.invalid_request, "OCR 图像不能为空")

    try:
        raw_img = Image.open(io.BytesIO(image_bytes))
    except (Image.DecompressionBombError, Image.UnidentifiedImageError, OSError, ValueError, SyntaxError) as exc:
        raise LogicError(ErrorCode.invalid_request, f"无效的图像数据: {exc}") from exc

    width, height = raw_img.size
    if width <= 0 or height <= 0:
        raise LogicError(ErrorCode.invalid_request, f"图像尺寸非法: {width}x{height}")
    if width * height > OCR_MAX_IMAGE_PIXELS:
        raise LogicError(
            ErrorCode.invalid_request,
            f"图像像素尺寸超过上限（{width}x{height} > {OCR_MAX_IMAGE_PIXELS}）",
        )

    try:
        raw_img.load()
    except (Image.DecompressionBombError, Image.UnidentifiedImageError, OSError, ValueError, SyntaxError) as exc:
        raise LogicError(ErrorCode.invalid_request, f"图像解码失败: {exc}") from exc

    has_alpha = raw_img.mode in ("RGBA", "LA") or (
        raw_img.mode == "P" and "transparency" in raw_img.info
    )
    if has_alpha or raw_img.mode == "P":
        rgba = raw_img.convert("RGBA")
        arr = np.asarray(rgba, dtype=np.uint8)
        alpha = arr[:, :, 3]
        if np.any(alpha < 255):
            fg_mask = alpha > 24
            if np.any(fg_mask):
                fg_luma = _perceptual_luma_rgb(arr[:, :, :3][fg_mask])
                bg_fill = _DARK_CANVAS_RGB if fg_luma >= 140.0 else _LIGHT_CANVAS_RGB
            else:
                bg_fill = _LIGHT_CANVAS_RGB
            canvas = Image.new("RGBA", rgba.size, (*bg_fill, 255))
            orig_rgb = Image.alpha_composite(canvas, rgba).convert("RGB")
            border_fill = bg_fill
        else:
            orig_rgb = rgba.convert("RGB")
            border_fill = _edge_border_fill(orig_rgb)
    else:
        orig_rgb = raw_img.convert("RGB")
        border_fill = _edge_border_fill(orig_rgb)

    scale = OCR_UPSCALE if (width * OCR_UPSCALE) * (height * OCR_UPSCALE) <= OCR_UPSCALE_MAX_PIXELS else 1
    gray = orig_rgb.convert("L")
    if scale > 1:
        gray = gray.resize((width * scale, height * scale), Image.Resampling.BICUBIC)
    default_border_fill = int(Image.new("RGB", (1, 1), border_fill).convert("L").getpixel((0, 0)))
    return PreparedOcrBase(
        orig_rgb=orig_rgb,
        gray_scaled=gray,
        default_border_fill=default_border_fill,
        scale=scale,
    )


def is_identity_preprocess(params: OcrPreprocessParams | None) -> bool:
    """判定预处理参数是否为零变换恒等配置。"""
    if params is None:
        return True
    return (
        not params.invert
        and abs(params.contrast - 1.0) < _FLOAT_EPS
        and abs(params.brightness - 1.0) < _FLOAT_EPS
        and params.sharpen <= _FLOAT_EPS
        and params.binarize_mode == OcrBinarizeMode.off
    )


def compute_otsu_threshold(gray_arr: np.ndarray) -> int:
    """对 8 位灰度数组计算 Otsu 自动二值化阈值（0..255）。

    零方差纯色图直接返回该像素值，避免除零；一般图取最大类间方差区间中点 + 1，适配 arr >= thr 二值化。
    """
    arr = np.asarray(gray_arr, dtype=np.uint8)
    if arr.size == 0:
        return 128
    hist = np.bincount(arr.ravel(), minlength=256).astype(np.float64)
    nonzero = np.flatnonzero(hist)
    if nonzero.size <= 1:
        return int(nonzero[0]) if nonzero.size == 1 else 128

    total = float(arr.size)
    weights_b = np.cumsum(hist)[:-1]
    weights_f = total - weights_b
    values = np.arange(256, dtype=np.float64)
    cum_sum = np.cumsum(hist * values)
    sum_total = float(cum_sum[-1])
    sum_b = cum_sum[:-1]

    valid = (weights_b > 0) & (weights_f > 0)
    between = np.full(255, -1.0, dtype=np.float64)
    mean_b = sum_b[valid] / weights_b[valid]
    mean_f = (sum_total - sum_b[valid]) / weights_f[valid]
    between[valid] = weights_b[valid] * weights_f[valid] * (mean_b - mean_f) ** 2

    max_var = float(np.max(between))
    best_ks = np.flatnonzero(np.isclose(between, max_var, rtol=1e-12, atol=1e-12))
    mid_k = 0.5 * float(best_ks[0] + best_ks[-1])
    return max(0, min(255, int(round(mid_k)) + 1))


def apply_ocr_preprocess(
    gray_scaled: Image.Image,
    params: OcrPreprocessParams | None,
) -> tuple[Image.Image, int | None]:
    """对灰度基图按固定顺序执行高对比度校正：invert → contrast → brightness → sharpen → binarize。

    返回 (处理后 L 图像, binarize_mode=otsu 时实际计算的阈值或 None)。
    恒等或 None 参数零变换直通。
    """
    gray = gray_scaled if gray_scaled.mode == "L" else gray_scaled.convert("L")
    if is_identity_preprocess(params):
        return gray, None

    assert params is not None
    img = ImageOps.invert(gray) if params.invert else gray
    if abs(params.contrast - 1.0) >= _FLOAT_EPS:
        img = ImageEnhance.Contrast(img).enhance(params.contrast)
    if abs(params.brightness - 1.0) >= _FLOAT_EPS:
        img = ImageEnhance.Brightness(img).enhance(params.brightness)
    if params.sharpen > _FLOAT_EPS:
        img = img.filter(
            ImageFilter.UnsharpMask(
                radius=1.2,
                percent=int(round(params.sharpen * 100)),
                threshold=2,
            )
        )

    if params.binarize_mode == OcrBinarizeMode.off:
        return img, None

    arr = np.asarray(img, dtype=np.uint8)
    if params.binarize_mode == OcrBinarizeMode.manual:
        out = np.where(arr >= params.threshold, 255, 0).astype(np.uint8)
        return Image.fromarray(out, mode="L"), None

    # Otsu 自动二值化：零方差纯色图确定性置 255(c>=128) / 0(c<128)
    if arr.size > 0 and int(arr.min()) == int(arr.max()):
        c = int(arr.flat[0])
        fill_val = 255 if c >= 128 else 0
        out = np.full(arr.shape, fill_val, dtype=np.uint8)
        return Image.fromarray(out, mode="L"), c

    otsu_thr = compute_otsu_threshold(arr)
    out = np.where(arr >= otsu_thr, 255, 0).astype(np.uint8)
    return Image.fromarray(out, mode="L"), otsu_thr


def build_ocr_input_image(
    base: PreparedOcrBase,
    params: OcrPreprocessParams | None,
    *,
    border_px: int = OCR_BORDER_PX,
) -> tuple[Image.Image, int | None]:
    """构造带 border_px 留白的最终 OCR 输入图，保证各预设与原图尺寸严格一致。

    恒等/无参时保持原 default_border_fill；非恒等时按处理后四边均值明度重算留白，避免反色后出现反向黑框。
    """
    if is_identity_preprocess(params):
        ocr_img = (
            ImageOps.expand(base.gray_scaled, border=border_px, fill=base.default_border_fill)
            if border_px > 0
            else base.gray_scaled
        )
        return ocr_img, None

    processed, otsu_thr = apply_ocr_preprocess(base.gray_scaled, params)
    if border_px <= 0:
        return processed, otsu_thr

    edge_luma = _gray_edge_mean(processed)
    is_binary = params is not None and params.binarize_mode != OcrBinarizeMode.off
    border_fill = (0 if is_binary else _DARK_GRAY_FILL) if edge_luma < 140.0 else 255
    return ImageOps.expand(processed, border=border_px, fill=border_fill), otsu_thr


def render_ocr_preprocess_preview(
    image_bytes: bytes,
    params: OcrPreprocessParams | None = None,
    *,
    max_edge: int = OCR_PREVIEW_MAX_EDGE,
) -> OcrPreprocessPreviewResponse:
    """后端同源预处理预览渲染：复用 prepare_ocr_base + apply_ocr_preprocess（不加 12px 留白）。

    长边 <= max_edge 时原尺寸输出（与正式 OCR 输入图去留白区域逐像素一致）；超限等比缩放后编码 PNG Base64。
    """
    effective = params if params is not None else OcrPreprocessParams()
    base = prepare_ocr_base(image_bytes)
    processed, otsu_thr = apply_ocr_preprocess(base.gray_scaled, effective)

    orig_w, orig_h = base.orig_rgb.size
    pw, ph = processed.size
    if max(pw, ph) > max_edge:
        ratio = float(max_edge) / float(max(pw, ph))
        new_w = max(1, int(round(pw * ratio)))
        new_h = max(1, int(round(ph * ratio)))
        resample = (
            Image.Resampling.NEAREST
            if effective.binarize_mode != OcrBinarizeMode.off
            else Image.Resampling.LANCZOS
        )
        preview_img = processed.resize((new_w, new_h), resample)
    else:
        preview_img = processed

    buf = io.BytesIO()
    preview_img.save(buf, format="PNG")
    encoded = base64.b64encode(buf.getvalue()).decode("ascii")
    return OcrPreprocessPreviewResponse(
        image_base64=encoded,
        image_width=orig_w,
        image_height=orig_h,
        preview_width=preview_img.size[0],
        preview_height=preview_img.size[1],
        otsu_threshold=otsu_thr,
    )


def evaluate_ocr_quality(
    words: Sequence[QualityWord],
    ocr_text: str,
) -> OcrQualityScore:
    """基于已过滤词置信度与最终文本有效字符占比计算 OCR 质量分并判定是否达标早停。"""
    non_ws = [ch for ch in (ocr_text or "") if not ch.isspace()]
    non_ws_chars = len(non_ws)
    valid_chars = sum(1 for ch in non_ws if ch.isalnum())
    valid_ratio = (valid_chars / non_ws_chars) if non_ws_chars > 0 else 0.0

    total_weight = 0
    weighted_sum = 0.0
    for word in words:
        wlen = len(word.text)
        if wlen <= 0:
            continue
        clamped_conf = max(0.0, min(100.0, float(word.conf)))
        weighted_sum += clamped_conf * wlen
        total_weight += wlen
    weighted_conf = (weighted_sum / total_weight) if total_weight > 0 else 0.0

    score = round(weighted_conf * valid_ratio * min(1.0, valid_chars / 2.0), 2)
    passed = (
        valid_chars >= OCR_QUALITY_MIN_VALID_CHARS
        and valid_ratio >= OCR_QUALITY_MIN_VALID_RATIO
        and weighted_conf >= OCR_QUALITY_MIN_CONF
    )
    return OcrQualityScore(
        weighted_conf=weighted_conf,
        valid_chars=valid_chars,
        non_ws_chars=non_ws_chars,
        valid_ratio=valid_ratio,
        score=score,
        passed=passed,
    )


__all__ = [
    "OCR_BORDER_PX",
    "OCR_MAX_IMAGE_PIXELS",
    "OCR_PREVIEW_MAX_EDGE",
    "OCR_QUALITY_MIN_CONF",
    "OCR_QUALITY_MIN_VALID_CHARS",
    "OCR_QUALITY_MIN_VALID_RATIO",
    "OCR_UPSCALE",
    "OCR_UPSCALE_MAX_PIXELS",
    "OcrQualityScore",
    "PreparedOcrBase",
    "QualityWord",
    "apply_ocr_preprocess",
    "build_ocr_input_image",
    "compute_otsu_threshold",
    "evaluate_ocr_quality",
    "is_identity_preprocess",
    "prepare_ocr_base",
    "render_ocr_preprocess_preview",
]
