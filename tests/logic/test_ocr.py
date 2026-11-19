from __future__ import annotations

import io
import subprocess
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import pytest
from PIL import Image, ImageDraw, ImageFont

import _ocr
from _contracts import ErrorCode, LogicError
from _ocr import (
    DEFAULT_OCR_LANG,
    OCR_BORDER_PX,
    clean_cjk_spaces,
    clear_language_cache,
    get_cached_languages,
    image_to_text,
)


def test_default_ocr_lang_is_chi_sim_plus_eng():
    assert DEFAULT_OCR_LANG == "chi_sim+eng"


def test_empty_image_rejected():
    with pytest.raises(LogicError) as ei:
        image_to_text(b"", get_languages=lambda: ["eng"], image_to_string=lambda *a, **k: "x")
    assert ei.value.code.value == "invalid_request"


def test_missing_language_pack_is_explicit():
    with pytest.raises(LogicError) as ei:
        image_to_text(
            b"fakepng",
            lang="eng+jpn",
            get_languages=lambda: ["eng", "chi_sim", "osd"],
            image_to_string=lambda *a, **k: "should not run",
        )
    assert ei.value.code.value == "ocr_language_missing"
    assert "jpn" in ei.value.message
    assert "不会自动安装" in ei.value.message


def test_success_and_empty_result_with_mocks():
    out = image_to_text(
        _tiny_png(),
        lang="eng",
        get_languages=lambda: ["eng"],
        image_to_string=lambda img, lang: "HELLO",
    )
    assert out == "HELLO"
    with pytest.raises(LogicError) as ei:
        image_to_text(
            _tiny_png(),
            lang="eng",
            get_languages=lambda: ["eng"],
            image_to_string=lambda img, lang: "   ",
        )
    assert ei.value.code.value == "ocr_failed"


def test_cjk_adjacent_spaces_removed_preserving_english_and_newlines():
    raw = "你 好  世\t界\n使 用 Python 3 编 程\n第 二 段 文 字"
    cleaned = clean_cjk_spaces(raw)
    assert cleaned == "你好世界\n使用 Python 3 编程\n第二段文字"

    out = image_to_text(
        _tiny_png(),
        lang="chi_sim+eng",
        get_languages=lambda: ["chi_sim", "eng"],
        image_to_string=lambda img, lang: "人 工 智 能   AI 助 手",
    )
    assert out == "人工智能   AI 助手"


def test_transparent_rgba_compositing_and_12px_border():
    # 浅色前景字 + 透明底 → 自动合成深底 #111827 并加四周 12px 留白
    light_fg = Image.new("RGBA", (60, 30), (0, 0, 0, 0))
    draw1 = ImageDraw.Draw(light_fg)
    draw1.rectangle([10, 8, 50, 22], fill=(248, 250, 252, 255))
    buf1 = io.BytesIO()
    light_fg.save(buf1, format="PNG")

    seen_imgs: list[Image.Image] = []

    def spy_runner(img: Image.Image, lang: str, timeout: int = 0) -> str:
        assert timeout == 15
        seen_imgs.append(img.copy())
        return "LIGHT"

    assert (
        image_to_text(
            buf1.getvalue(),
            lang="eng",
            get_languages=lambda: ["eng"],
            image_to_string=spy_runner,
        )
        == "LIGHT"
    )
    padded_dark = seen_imgs[0]
    assert padded_dark.mode == "RGB"
    assert padded_dark.size == (60 + 2 * OCR_BORDER_PX, 30 + 2 * OCR_BORDER_PX)
    assert padded_dark.getpixel((0, 0)) == (17, 24, 39)  # #111827
    assert padded_dark.getpixel((OCR_BORDER_PX + 2, OCR_BORDER_PX + 2)) == (17, 24, 39)

    # 深色前景字 + 透明底 → 自动合成白底 #ffffff 并加四周 12px 留白
    dark_fg = Image.new("RGBA", (60, 30), (0, 0, 0, 0))
    draw2 = ImageDraw.Draw(dark_fg)
    draw2.rectangle([10, 8, 50, 22], fill=(15, 23, 42, 255))
    buf2 = io.BytesIO()
    dark_fg.save(buf2, format="PNG")
    seen_imgs.clear()
    image_to_text(
        buf2.getvalue(),
        lang="eng",
        get_languages=lambda: ["eng"],
        image_to_string=spy_runner,
    )
    padded_light = seen_imgs[0]
    assert padded_light.size == (84, 54)
    assert padded_light.getpixel((0, 0)) == (255, 255, 255)
    assert padded_light.getpixel((OCR_BORDER_PX + 2, OCR_BORDER_PX + 2)) == (255, 255, 255)


def test_tesseract_timeout_maps_to_error_code_timeout():
    def slow_runtime_err(img, lang, timeout=15):
        raise RuntimeError("Tesseract process timeout")

    with pytest.raises(LogicError) as ei:
        image_to_text(
            _tiny_png(),
            lang="eng",
            get_languages=lambda: ["eng"],
            image_to_string=slow_runtime_err,
        )
    assert ei.value.code == ErrorCode.timeout

    def subproc_timeout(img, lang, timeout=15):
        raise subprocess.TimeoutExpired(cmd="tesseract", timeout=timeout)

    with pytest.raises(LogicError) as ei2:
        image_to_text(
            _tiny_png(),
            lang="eng",
            get_languages=lambda: ["eng"],
            image_to_string=subproc_timeout,
        )
    assert ei2.value.code == ErrorCode.timeout


def test_thread_safe_language_cache_and_clear(monkeypatch):
    pytesseract = pytest.importorskip("pytesseract")
    calls = {"count": 0}

    def fake_get_languages(config: str = ""):
        calls["count"] += 1
        return ["chi_sim", "eng"]

    clear_language_cache()
    monkeypatch.setattr(pytesseract, "get_languages", fake_get_languages)
    try:
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _: get_cached_languages(), range(16)))
        assert all(r == ["chi_sim", "eng"] for r in results)
        assert calls["count"] == 1
        clear_language_cache()
        assert get_cached_languages() == ["chi_sim", "eng"]
        assert calls["count"] == 2
    finally:
        clear_language_cache()


def test_tesseract_error_maps_language_missing():
    def boom(img, lang):
        raise RuntimeError("Failed loading language 'jpn'")

    with pytest.raises(LogicError) as ei:
        image_to_text(
            _tiny_png(),
            lang="eng",
            get_languages=lambda: ["eng"],
            image_to_string=boom,
        )
    assert ei.value.code.value == "ocr_language_missing"


def test_live_tesseract_synthetic_hello_if_eng_present():
    """真实 tesseract + 合成图（含透明白字合成验证），不是私人截图。"""
    pytesseract = pytest.importorskip("pytesseract")
    clear_language_cache()
    try:
        langs = set(pytesseract.get_languages(config=""))
    except Exception as exc:
        pytest.skip(f"无法查询 tesseract 语言包: {exc}")
    if "eng" not in langs:
        pytest.skip("本机未安装 tesseract eng 语言包")
    text = image_to_text(_hello_image(), lang="eng")
    assert "HELLO" in text.replace(" ", "").upper()


def _tiny_png() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (8, 8), "white").save(buf, format="PNG")
    return buf.getvalue()


def _hello_image() -> bytes:
    img = Image.new("RGB", (240, 80), "white")
    draw = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 36)
    except OSError:
        font = ImageFont.load_default()
    draw.text((10, 20), "HELLO", fill="black", font=font)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()
