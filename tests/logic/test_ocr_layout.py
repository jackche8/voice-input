"""_ocr_layout 排版提取：留白与放大倍数的坐标还原、分列成行、块带行列表，及 2~5px 环形背景色采样单测。"""
from __future__ import annotations

import io
import os
import sys
from types import SimpleNamespace

import numpy as np
import pytest
from PIL import Image, ImageDraw

import _ocr
from _contracts import ErrorCode, LogicError
from _ocr import OCR_BORDER_PX, OCR_TIMEOUT_S
from _ocr_layout import extract_ocr_layout, sample_ring_colors


def _png_bytes(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def test_coordinates_undo_padding_and_upscale_and_blocks_carry_lines():
    # 原图 320x200：上半区深底 #1e293b，下半区浅底 #f1f5f9
    img = Image.new("RGB", (320, 200), (241, 245, 249))
    draw = ImageDraw.Draw(img)
    draw.rectangle([0, 0, 320, 100], fill=(30, 41, 59))
    # 在第一个文字框内部及 1px 紧贴边缘故意画白色像素，验证 2~5px 环形采样不受字形边缘污染
    draw.rectangle([19, 19, 101, 65], fill=(255, 255, 255))

    pad, scale = OCR_BORDER_PX, 2

    def at(v: int) -> int:  # 原图坐标 → OCR 输入图坐标（放大后再加留白）
        return pad + scale * v

    def fake_data_runner(ocr_img: Image.Image, lang: str, timeout: int = 0) -> dict:
        assert timeout == OCR_TIMEOUT_S
        assert ocr_img.mode == "L" and ocr_img.size == (320 * scale + 2 * pad, 200 * scale + 2 * pad)
        # 模拟：
        # 1) 第 1、2 行相邻中文（同一块、逐行保留，去汉字间空格）
        # 2) 同一行右侧大间距（x=220）独立列 "Settings"（分列拆开、自成一块）
        # 3) 低置信度噪点（conf=8 与单符号噪点 '|' conf=30，应被过滤）
        # 4) 下半区浅底两行英文（同一块、逐行保留）
        xs, ys = [20, 42, 220, 20, 150, 5, 24, 24], [20, 20, 20, 44, 44, 70, 120, 142]
        ws, hs = [18, 18, 70, 80, 20, 4, 50, 52], [18, 18, 18, 20, 18, 18, 18, 18]
        return {
            "level": [5] * 8,
            "page_num": [1] * 8,
            "block_num": [1, 1, 1, 1, 1, 1, 2, 2],
            "par_num": [1] * 8,
            "line_num": [1, 1, 1, 2, 2, 3, 1, 2],
            "left": [at(x) for x in xs],
            "top": [at(y) for y in ys],
            "width": [scale * w for w in ws],
            "height": [scale * h for h in hs],
            "conf": [92, 90, 88, 95, 8, 30, 91, 89],
            "text": ["你", "好", "Settings", "世 界", "noise", "|", "Hello", "world"],
        }

    w, h, full_text, blocks = extract_ocr_layout(
        _png_bytes(img),
        lang="chi_sim+eng",
        runner_data=fake_data_runner,
        languages_fn=lambda: ["chi_sim", "eng"],
    )
    assert (w, h) == (320, 200)
    assert [[(line.bbox, line.text, line.soft_wrap) for line in b.lines] for b in blocks] == [
        [((20, 20, 40, 18), "你好", False), ((20, 44, 80, 20), "世界", False)],
        [((220, 20, 70, 18), "Settings", False)],
        [((24, 120, 50, 18), "Hello", False), ((24, 142, 52, 18), "world", False)],
    ]
    assert [(b.bbox, b.ocr_text, b.bg_color, b.fg_color) for b in blocks] == [
        ((20, 20, 80, 44), "你好\n世界", "#1e293b", "#f8fafc"),  # 深底采样 + 浅色前景
        ((220, 20, 70, 18), "Settings", "#1e293b", "#f8fafc"),
        ((24, 120, 52, 40), "Hello\nworld", "#f1f5f9", "#0f172a"),  # 浅底采样 + 深色前景
    ]
    assert full_text == "你好\n世界\n\nSettings\n\nHello\nworld"  # 块间空一行，噪点已滤掉


def test_line_box_ignores_abnormally_tall_word_boxes_only_in_lines_with_cjk():
    """含中日韩字的行：Tesseract 给出的约 1.5 倍高异常词框（含被认成半角的逗号）不撑高行框；纯拉丁文行不过滤。"""
    pad, scale = OCR_BORDER_PX, 2
    # (x, y, w, h, 词, 行号)：中文行正常字高 16，逗号与「並」是 25 高的异常框；拉丁文行词高 18 与 10 并存
    words = [
        (20, 20, 32, 16, "權重", 1), (54, 17, 4, 25, ",", 1), (60, 17, 16, 25, "並", 1), (78, 20, 32, 16, "核對", 1),
        (20, 70, 80, 18, "config.json", 2), (104, 76, 8, 10, "a", 2), (116, 76, 14, 10, "in", 2),
    ]

    def fake_data_runner(_img: Image.Image, lang: str, timeout: int = 0) -> dict:
        return {
            "level": [5] * len(words),
            "line_num": [w[5] for w in words],
            "left": [pad + scale * w[0] for w in words],
            "top": [pad + scale * w[1] for w in words],
            "width": [scale * w[2] for w in words],
            "height": [scale * w[3] for w in words],
            "conf": [90] * len(words),
            "text": [w[4] for w in words],
        }

    _w, _h, _text, blocks = extract_ocr_layout(
        _png_bytes(Image.new("RGB", (400, 120), "white")),
        lang="chi_tra+eng",
        runner_data=fake_data_runner,
        languages_fn=lambda: ["chi_tra", "eng"],
    )
    lines = [(line.text, line.bbox) for block in blocks for line in block.lines]
    assert lines == [("權重,並核對", (20, 20, 90, 16)), ("config.json a in", (20, 70, 110, 18))]


def test_ring_color_sampling_luma_boundary_and_tight_crop_fallback():
    # L = 0.299*139 + 0.587*139 + 0.114*139 = 139.0 < 140 → #f8fafc
    dark_arr = np.full((40, 40, 3), 139, dtype=np.uint8)
    bg1, fg1 = sample_ring_colors(dark_arr, (10, 10, 15, 15))
    assert bg1 == "#8b8b8b"
    assert fg1 == "#f8fafc"

    # L = 140.0 >= 140 → #0f172a
    light_arr = np.full((40, 40, 3), 140, dtype=np.uint8)
    bg2, fg2 = sample_ring_colors(light_arr, (10, 10, 15, 15))
    assert bg2 == "#8c8c8c"
    assert fg2 == "#0f172a"

    # 紧贴全图四边（外环无 2~5px 余量）时仍能稳定回退采样不抛错
    tight_arr = np.full((12, 12, 3), (20, 30, 40), dtype=np.uint8)
    bg3, fg3 = sample_ring_colors(tight_arr, (0, 0, 12, 12))
    assert bg3 == "#141e28"
    assert fg3 == "#f8fafc"


def test_extract_ocr_layout_error_paths():
    with pytest.raises(LogicError) as empty_err:
        extract_ocr_layout(b"", languages_fn=lambda: ["chi_sim", "eng"])
    assert empty_err.value.code == ErrorCode.invalid_request

    img_bytes = _png_bytes(Image.new("RGB", (40, 20), "white"))
    with pytest.raises(LogicError) as lang_err:
        extract_ocr_layout(img_bytes, lang="jpn", languages_fn=lambda: ["chi_sim", "eng"])
    assert lang_err.value.code == ErrorCode.ocr_language_missing

    with pytest.raises(LogicError) as blank_err:
        extract_ocr_layout(
            img_bytes,
            lang="eng",
            languages_fn=lambda: ["eng"],
            runner_data=lambda img, lang: {"level": [5], "text": ["   "], "conf": [-1]},
        )
    assert blank_err.value.code == ErrorCode.ocr_failed

    def timeout_runner(img, lang, timeout=15):
        raise RuntimeError("Tesseract process timeout")

    with pytest.raises(LogicError) as to_err:
        extract_ocr_layout(
            img_bytes,
            lang="eng",
            languages_fn=lambda: ["eng"],
            runner_data=timeout_runner,
        )
    assert to_err.value.code == ErrorCode.timeout


def test_extract_ocr_layout_uses_bundled_tesseract_via_tessdata_prefix_and_restores(tmp_path, monkeypatch):
    bundled_root = tmp_path / "Ollama Voice Input" / "_internal"
    bundled_bin = bundled_root / "tesseract" / "tesseract.exe"
    bundled_td = bundled_root / "tesseract" / "tessdata"
    bundled_td.mkdir(parents=True)
    bundled_bin.write_bytes(b"")

    pad, scale = OCR_BORDER_PX, 2
    fake_pt_inner = SimpleNamespace(tesseract_cmd="tesseract")
    observed: list[dict] = []

    def fake_image_to_data(img, *, lang, output_type, timeout=0, **kw):
        observed.append({
            "cmd": fake_pt_inner.tesseract_cmd,
            "tessdata_prefix": os.environ.get("TESSDATA_PREFIX"),
            "extra_kw": dict(kw),
        })
        return {
            "level": [5],
            "page_num": [1],
            "block_num": [1],
            "par_num": [1],
            "line_num": [1],
            "left": [pad + scale * 4],
            "top": [pad + scale * 4],
            "width": [scale * 20],
            "height": [scale * 10],
            "conf": [95],
            "text": ["Hello"],
        }

    fake_pt = SimpleNamespace(
        pytesseract=fake_pt_inner,
        Output=SimpleNamespace(DICT="dict"),
        image_to_data=fake_image_to_data,
    )
    monkeypatch.setitem(sys.modules, "pytesseract", fake_pt)
    monkeypatch.setattr(_ocr, "PROGRAM_ROOT", bundled_root)
    monkeypatch.setattr(_ocr, "detect_os", lambda: "windows")
    monkeypatch.delenv("TESSDATA_PREFIX", raising=False)

    _w, _h, text, blocks = extract_ocr_layout(
        _png_bytes(Image.new("RGB", (80, 40), "white")),
        lang="eng",
        languages_fn=lambda: ["eng"],
    )
    assert text == "Hello" and len(blocks) == 1
    assert observed == [{
        "cmd": str(bundled_bin),
        "tessdata_prefix": str(bundled_td),
        "extra_kw": {},
    }]
    assert fake_pt_inner.tesseract_cmd == "tesseract"
    assert "TESSDATA_PREFIX" not in os.environ
