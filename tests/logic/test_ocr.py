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
    OCR_TIMEOUT_S,
    OCR_UPSCALE_MAX_PIXELS,
    clear_language_cache,
    default_ocr_languages,
    get_cached_languages,
    image_to_text,
    list_tesseract_languages,
    prepare_ocr_image,
)
from _ocr_text import clean_cjk_spaces
from _platform import child_process as cp_mod


def test_default_languages_and_their_capability_labels_come_from_one_constant():
    assert DEFAULT_OCR_LANG == "chi_sim+chi_tra+eng"
    assert default_ocr_languages() == [
        ("chi_sim", "OCR 简体中文"),
        ("chi_tra", "OCR 繁体中文"),
        ("eng", "OCR 英文"),
    ]


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
    raw = "你 好  世\t界\n使 用 Python 3 编 程\n第 二 段 文 字\n权 重 ， 并 核 对 。\n權重 , 並 核 對 .\nHello , world ."
    cleaned = clean_cjk_spaces(raw)
    # 紧挨中日韩字的半角标点两侧空白也去掉；英文里的半角标点与空格原样
    assert cleaned == "你好世界\n使用 Python 3 编程\n第二段文字\n权重，并核对。\n權重,並核對.\nHello , world ."

    out = image_to_text(
        _tiny_png(),
        lang="chi_sim+eng",
        get_languages=lambda: ["chi_sim", "eng"],
        image_to_string=lambda img, lang: "人 工 智 能   AI 助 手",
    )
    assert out == "人工智能   AI 助手"


def test_preprocess_composites_transparency_then_grays_upscales_and_pads():
    # 浅色前景字 + 透明底 → 合成深底 #111827；深色前景字 + 透明底 → 合成白底；再转灰度、放大 2×、四周留白
    for fg, canvas in (((248, 250, 252, 255), (17, 24, 39)), ((15, 23, 42, 255), (255, 255, 255))):
        src = Image.new("RGBA", (60, 30), (0, 0, 0, 0))
        ImageDraw.Draw(src).rectangle([10, 8, 50, 22], fill=fg)
        buf = io.BytesIO()
        src.save(buf, format="PNG")
        orig, ocr_img, scale = prepare_ocr_image(buf.getvalue())
        canvas_l = Image.new("RGB", (1, 1), canvas).convert("L").getpixel((0, 0))
        assert orig.mode == "RGB" and orig.size == (60, 30) and orig.getpixel((2, 2)) == canvas
        assert scale == 2 and ocr_img.mode == "L"
        assert ocr_img.size == (120 + 2 * OCR_BORDER_PX, 60 + 2 * OCR_BORDER_PX)
        assert ocr_img.getpixel((0, 0)) == canvas_l  # 留白与合成底色一致
        assert ocr_img.getpixel((OCR_BORDER_PX + 4, OCR_BORDER_PX + 4)) == canvas_l
        # 原图 (30, 15) 的前景像素在 OCR 图里位于 (留白 + 2×30, 留白 + 2×15)
        fg_l = Image.new("RGB", (1, 1), fg[:3]).convert("L").getpixel((0, 0))
        assert ocr_img.getpixel((OCR_BORDER_PX + 60, OCR_BORDER_PX + 30)) == fg_l


def test_selection_too_large_for_the_ocr_budget_is_not_upscaled():
    assert OCR_UPSCALE_MAX_PIXELS == 1920 * 1080 * 4
    assert OCR_TIMEOUT_S == 30

    # 1920×1080 刚好在上限内保持 2×；3440×1440 超出上限降为 1×
    for (w, h), expected_scale in (((1920, 1080), 2), ((3440, 1440), 1)):
        buf = io.BytesIO()
        Image.new("RGB", (w, h), "white").save(buf, format="PNG")
        _orig, ocr_img, scale = prepare_ocr_image(buf.getvalue())
        assert scale == expected_scale
        assert ocr_img.size == (w * expected_scale + 2 * OCR_BORDER_PX, h * expected_scale + 2 * OCR_BORDER_PX)

    seen: list[tuple[int, int]] = []

    def spy_runner(img: Image.Image, lang: str, timeout: int = 0) -> str:
        assert timeout == OCR_TIMEOUT_S
        seen.append(img.size)
        return "OK"

    assert image_to_text(_tiny_png(), lang="eng", get_languages=lambda: ["eng"], image_to_string=spy_runner) == "OK"
    assert seen == [(16 + 2 * OCR_BORDER_PX, 16 + 2 * OCR_BORDER_PX)]  # image_to_text 走同一预处理（8×8 → 2×）


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
    calls = {"count": 0}

    def fake_list_languages():
        calls["count"] += 1
        return ["chi_sim", "eng"]

    clear_language_cache()
    monkeypatch.setattr(_ocr, "list_tesseract_languages", fake_list_languages)
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


def test_list_tesseract_languages_runs_hidden_and_parses_headers_and_errors(monkeypatch):
    seen_kw: list[dict] = []

    def fake_run(cmd, **kw):
        seen_kw.append({"cmd": list(cmd), **kw})
        out = 'List of available languages in "/usr/share/tesseract-ocr/5/tessdata/" (4):\nchi_sim\nchi_tra\neng\nchi_sim\nosd\n'
        return subprocess.CompletedProcess(cmd, 0, out, "")

    monkeypatch.setattr(cp_mod.os, "name", "nt")
    langs = list_tesseract_languages(run=fake_run, tesseract_cmd="tesseract.exe")
    assert langs == ["chi_sim", "chi_tra", "eng", "osd"]
    assert seen_kw[0]["cmd"] == ["tesseract.exe", "--list-langs"]
    assert seen_kw[0]["creationflags"] == cp_mod.CREATE_NO_WINDOW
    assert seen_kw[0]["encoding"] == "utf-8" and seen_kw[0]["errors"] == "replace"

    # Tesseract 3 风格输出到 stderr 也能解析
    stderr_only = lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, "", "List of available languages (2):\neng\nchi_sim\n")  # noqa: E731
    assert list_tesseract_languages(run=stderr_only, tesseract_cmd="tesseract") == ["eng", "chi_sim"]

    # 非零退出与命令缺失均抛 LogicError(ocr_failed)
    with pytest.raises(LogicError) as bad_rc:
        list_tesseract_languages(run=lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, "", "boom"), tesseract_cmd="tesseract")
    assert bad_rc.value.code == ErrorCode.ocr_failed and "boom" in bad_rc.value.message

    def missing_bin(cmd, **kw):
        raise FileNotFoundError("tesseract")

    with pytest.raises(LogicError) as missing:
        list_tesseract_languages(run=missing_bin, tesseract_cmd="tesseract")
    assert missing.value.code == ErrorCode.ocr_failed


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
