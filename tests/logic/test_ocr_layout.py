"""_ocr_layout 排版提取、12px 坐标还原、分列合段与 2~5px 环形背景色采样单测。"""
from __future__ import annotations

import io

import numpy as np
import pytest
from PIL import Image, ImageDraw

from _contracts import ErrorCode, LogicError
from _ocr import OCR_BORDER_PX
from _ocr_layout import extract_ocr_layout, sample_ring_colors


def _png_bytes(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def test_coordinate_restoration_column_split_paragraph_merge_and_noise_filter():
    # 原图 320x200：上半区深底 #1e293b，下半区浅底 #f1f5f9
    img = Image.new("RGB", (320, 200), (241, 245, 249))
    draw = ImageDraw.Draw(img)
    draw.rectangle([0, 0, 320, 100], fill=(30, 41, 59))
    # 在第一个文字框内部及 1px 紧贴边缘故意画白色像素，验证 2~5px 环形采样不受字形边缘污染
    draw.rectangle([19, 19, 101, 65], fill=(255, 255, 255))

    pad = OCR_BORDER_PX  # 12px

    def fake_data_runner(padded_img: Image.Image, lang: str, timeout: int = 0) -> dict:
        assert timeout == 15
        assert padded_img.size == (320 + 2 * pad, 200 + 2 * pad)
        # 模拟：
        # 1) 段落 1 第 1、2 行相邻中文（应合并为同一块，且去汉字间空格）
        # 2) 同一行右侧大间距（x=220）独立列按钮文字（应分列拆开）
        # 3) 低置信度噪点（conf=8 与单符号噪点 '|' conf=30，应被过滤）
        # 4) 下半区浅底段落 2 的两行英文（应合并为带空格的英文块）
        return {
            "level": [5, 5, 5, 5, 5, 5, 5, 5],
            "page_num": [1] * 8,
            "block_num": [1, 1, 1, 1, 1, 1, 2, 2],
            "par_num": [1, 1, 1, 1, 1, 1, 1, 1],
            "line_num": [1, 1, 1, 2, 2, 3, 1, 2],
            "left": [
                20 + pad,   # "你"
                42 + pad,   # "好"
                220 + pad,  # 同行右侧大间距列 "Settings"
                20 + pad,   # 第 2 行 "世界"
                150 + pad,  # 低置信度噪点
                5 + pad,    # 单符号低置信度边框噪点
                24 + pad,   # 段落 2 行 1 "Hello"
                24 + pad,   # 段落 2 行 2 "world"
            ],
            "top": [
                20 + pad,
                20 + pad,
                20 + pad,
                44 + pad,
                44 + pad,
                70 + pad,
                120 + pad,
                142 + pad,
            ],
            "width": [18, 18, 70, 80, 20, 4, 50, 52],
            "height": [18, 18, 18, 20, 18, 18, 18, 18],
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
    assert len(blocks) == 3

    # 块 0：合并后的两行中文，坐标已扣减 12px 留白，深底采样 #1e293b + 浅色前景 #f8fafc
    b0 = blocks[0]
    assert b0.ocr_text == "你好世界"
    assert b0.bbox == (20, 20, 80, 44)
    assert b0.bg_color == "#1e293b"
    assert b0.fg_color == "#f8fafc"

    # 块 1：同行大间距拆出的右侧独立列
    b1 = blocks[1]
    assert b1.ocr_text == "Settings"
    assert b1.bbox == (220, 20, 70, 18)
    assert b1.bg_color == "#1e293b"
    assert b1.fg_color == "#f8fafc"

    # 块 2：下半区浅底两行英文合并，保留英文词间空格，浅底 #f1f5f9 + 深色前景 #0f172a
    b2 = blocks[2]
    assert b2.ocr_text == "Hello world"
    assert b2.bbox == (24, 120, 52, 40)
    assert b2.bg_color == "#f1f5f9"
    assert b2.fg_color == "#0f172a"

    assert "noise" not in full_text
    assert "|" not in full_text
    assert full_text == "你好世界\nSettings\nHello world"


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
