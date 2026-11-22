"""Tesseract OCR 能力封装。缺语言包必须显式失败，不在本层安装或下载语言包。"""
from __future__ import annotations

import inspect
import io
import re
import subprocess
import threading
from collections.abc import Callable
from typing import Any, NoReturn

import numpy as np
from PIL import Image, ImageOps

from _contracts import ErrorCode, LogicError
from _ocr_text import clean_cjk_spaces
from _platform.child_process import windows_subprocess_kwargs

DEFAULT_OCR_LANG = "chi_sim+chi_tra+eng"
DEFAULT_OCR_LANGS = DEFAULT_OCR_LANG
# 默认识别语言的能力项标签：设置页与各平台能力自检都经 default_ocr_languages() 从 DEFAULT_OCR_LANG 派生
OCR_LANG_LABELS = {"chi_sim": "OCR 简体中文", "chi_tra": "OCR 繁体中文", "eng": "OCR 英文"}
OCR_BORDER_PX = 12
OCR_TIMEOUT_S = 30
OCR_UPSCALE = 2
# 放大后像素上限：实测（默认 chi_sim+chi_tra+eng，tests/logic/ocr_samples.py）：
# 全部 11 项精度样本最大 1280×860（2× = 4,403,200 px，4.17s 全过）、1920×1080 整屏 2× = 8,294,400 px（2.53–5.76s）；
# 3440×1440 密集整屏 2× 需 10.25–10.40s（高负载易撞 15s 超时），降 1× 后 7.22s（-30.6%）且与 OCR_TIMEOUT_S=30 留足裕度。
OCR_UPSCALE_MAX_PIXELS = 1920 * 1080 * 4

_LIGHT_CANVAS_RGB = (255, 255, 255)  # #ffffff
_DARK_CANVAS_RGB = (17, 24, 39)      # #111827

_lang_cache_lock = threading.Lock()
_cached_languages: tuple[str, ...] | None = None
_LANG_CODE_RE = re.compile(r"^[A-Za-z0-9_/-]+$")


def default_ocr_languages() -> list[tuple[str, str]]:
    """默认识别语言逐项（语言包代码, 能力项标签），顺序同 DEFAULT_OCR_LANG。"""
    return [(code, OCR_LANG_LABELS[code]) for code in DEFAULT_OCR_LANG.split("+")]


def clear_language_cache() -> None:
    """清空已缓存的 Tesseract 语言包列表（供测试或环境更新后重探测）。"""
    global _cached_languages
    with _lang_cache_lock:
        _cached_languages = None


def _resolve_tesseract_cmd(tesseract_cmd: str | None) -> str:
    if tesseract_cmd:
        return tesseract_cmd
    try:
        import pytesseract

        configured = getattr(pytesseract.pytesseract, "tesseract_cmd", None)
        if isinstance(configured, str) and configured.strip():
            return configured.strip()
    except ImportError:
        pass
    return "tesseract"


def list_tesseract_languages(
    *,
    run: Callable[..., subprocess.CompletedProcess] = subprocess.run,
    tesseract_cmd: str | None = None,
) -> list[str]:
    """直接以隐藏控制台方式执行 `tesseract --list-langs` 并解析语言代码列表（规避 pytesseract.get_languages 闪黑框）。"""
    cmd = _resolve_tesseract_cmd(tesseract_cmd)
    try:
        proc = run(
            [cmd, "--list-langs"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
            **windows_subprocess_kwargs(encoding="utf-8", errors="replace"),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise LogicError(ErrorCode.ocr_failed, f"无法查询 Tesseract 语言包: {exc}") from exc
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        raise LogicError(
            ErrorCode.ocr_failed,
            f"无法查询 Tesseract 语言包（退出码 {proc.returncode}）: {detail}",
        )
    raw = proc.stdout if (proc.stdout or "").strip() else (proc.stderr or "")
    lines = [line.strip() for line in raw.splitlines() if line.strip()]
    if lines and (":" in lines[0] or lines[0].lower().startswith("list of available languages")):
        lines = lines[1:]
    langs: list[str] = []
    for line in lines:
        if _LANG_CODE_RE.match(line) and line not in langs:
            langs.append(line)
    return langs


def get_cached_languages() -> list[str]:
    """线程安全地查询并缓存系统已安装的 Tesseract 语言包。"""
    global _cached_languages
    with _lang_cache_lock:
        if _cached_languages is not None:
            return list(_cached_languages)
        langs = tuple(list_tesseract_languages())
        _cached_languages = langs
        return list(langs)


def _missing_language(message: str) -> bool:
    lower = message.lower()
    return (
        "failed loading language" in lower
        or "error opening data file" in lower
        or ("tessdata" in lower and "not find" in lower)
        or "please make sure the tessdata" in lower
    )


def _is_timeout_error(exc: BaseException, message: str) -> bool:
    if isinstance(exc, (TimeoutError, subprocess.TimeoutExpired)):
        return True
    lower = message.lower()
    return "timeout" in lower or "timed out" in lower


def raise_ocr_error(exc: Exception) -> NoReturn:
    if isinstance(exc, LogicError):
        raise exc
    msg = str(exc)
    if _is_timeout_error(exc, msg):
        raise LogicError(ErrorCode.timeout, f"OCR 识别超时: {msg}") from exc
    if _missing_language(msg):
        raise LogicError(
            ErrorCode.ocr_language_missing,
            f"Tesseract 语言包不可用: {msg}",
        ) from exc
    raise LogicError(ErrorCode.ocr_failed, f"OCR 失败: {msg}") from exc


def ensure_ocr_languages(
    lang: str,
    languages_fn: Callable[[], list[str]] | None = None,
) -> None:
    if not lang or not lang.strip():
        raise LogicError(ErrorCode.invalid_request, "OCR 语言参数不能为空")
    provider = languages_fn or get_cached_languages
    requested = [part for part in lang.split("+") if part and part != "osd"]
    available = set(provider())
    missing = [name for name in requested if name not in available]
    if missing:
        raise LogicError(
            ErrorCode.ocr_language_missing,
            "缺少 Tesseract 语言包: " + ", ".join(missing) + "。本层不会自动安装（含日文包）。",
            details={"missing": missing, "available": sorted(available)},
        )


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
    top = arr[0, :, :]
    bottom = arr[-1, :, :]
    left = arr[:, 0, :]
    right = arr[:, -1, :]
    edges = np.concatenate([top, bottom, left, right], axis=0)
    luma = _perceptual_luma_rgb(edges)
    return _DARK_CANVAS_RGB if luma < 140.0 else _LIGHT_CANVAS_RGB


def prepare_ocr_image(
    image_bytes: bytes,
    *,
    border_px: int = OCR_BORDER_PX,
) -> tuple[Image.Image, Image.Image, int]:
    """返回 (合成后不透明原尺寸 RGB 图（供采色）, 灰度 + 放大 + 四周 border_px 留白的 OCR 输入图, 实际倍数)。

    透明图（RGBA / LA / P）按前景像素明度自动合成到白底 #ffffff 或深底 #111827，
    避免浅色前景字在白底消失或深色字在黑底消失。屏幕小字与深底浅字放大后识别明显更好；
    放大后像素超过 OCR_UPSCALE_MAX_PIXELS 的大选区用 1×（否则超出识别超时预算）。
    OCR 坐标还原：x_原图 = (x_ocr - border_px) / 倍数。
    """
    try:
        raw_img = Image.open(io.BytesIO(image_bytes))
        raw_img.load()
    except Exception as exc:
        raise_ocr_error(exc)

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

    width, height = orig_rgb.size
    scale = OCR_UPSCALE if (width * OCR_UPSCALE) * (height * OCR_UPSCALE) <= OCR_UPSCALE_MAX_PIXELS else 1
    gray = orig_rgb.convert("L")
    if scale > 1:
        gray = gray.resize((width * scale, height * scale), Image.Resampling.BICUBIC)
    gray_fill = Image.new("RGB", (1, 1), border_fill).convert("L").getpixel((0, 0))
    ocr_img = ImageOps.expand(gray, border=border_px, fill=gray_fill) if border_px > 0 else gray
    return orig_rgb, ocr_img, scale


def invoke_ocr_runner(
    runner: Callable[..., Any],
    img: Image.Image,
    *,
    lang: str,
    timeout_s: int = OCR_TIMEOUT_S,
) -> Any:
    """调用 OCR 执行器；自动兼容仅接收 (img, lang) 的测试替身与支持 timeout 的真实引擎。"""
    try:
        sig = inspect.signature(runner)
        params = sig.parameters
        accepts_timeout = "timeout" in params or any(
            p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values()
        )
    except (TypeError, ValueError):
        accepts_timeout = False
    if accepts_timeout:
        return runner(img, lang=lang, timeout=timeout_s)
    return runner(img, lang=lang)


def image_to_text(
    image_bytes: bytes,
    *,
    lang: str = DEFAULT_OCR_LANG,
    image_to_string: Callable[..., str] | None = None,
    get_languages: Callable[[], list[str]] | None = None,
) -> str:
    """从图片字节识别文本。默认 DEFAULT_OCR_LANG，缺语言包显式失败。"""
    if not image_bytes:
        raise LogicError(ErrorCode.invalid_request, "OCR 图像不能为空")

    runner = image_to_string
    if runner is None:
        try:
            import pytesseract
        except ImportError as exc:
            raise LogicError(ErrorCode.ocr_failed, f"OCR 依赖不可用: {exc}") from exc

        def _default_runner(img: Image.Image, lang: str, timeout: int = OCR_TIMEOUT_S) -> str:
            return pytesseract.image_to_string(img, lang=lang, timeout=timeout)

        runner = _default_runner

    ensure_ocr_languages(lang, get_languages)
    _orig_rgb, ocr_img, _scale = prepare_ocr_image(image_bytes, border_px=OCR_BORDER_PX)
    try:
        text = invoke_ocr_runner(runner, ocr_img, lang=lang, timeout_s=OCR_TIMEOUT_S)
    except Exception as exc:
        raise_ocr_error(exc)

    cleaned = clean_cjk_spaces(text or "")
    if not cleaned:
        raise LogicError(ErrorCode.ocr_failed, "没有识别到文字")
    return cleaned


__all__ = [
    "DEFAULT_OCR_LANG",
    "DEFAULT_OCR_LANGS",
    "OCR_BORDER_PX",
    "OCR_LANG_LABELS",
    "OCR_TIMEOUT_S",
    "OCR_UPSCALE",
    "OCR_UPSCALE_MAX_PIXELS",
    "clear_language_cache",
    "default_ocr_languages",
    "ensure_ocr_languages",
    "get_cached_languages",
    "image_to_text",
    "invoke_ocr_runner",
    "list_tesseract_languages",
    "prepare_ocr_image",
    "raise_ocr_error",
]
