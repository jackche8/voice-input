"""_ocr_preprocess 单元测试：无环依赖、非法图拒绝、参数化像素管线、Otsu 零方差、留白重算、同源预览与质量评分。"""
from __future__ import annotations

import ast
import base64
import io
from pathlib import Path

import numpy as np
import pytest
from PIL import Image, ImageDraw

import _ocr_preprocess
from _contracts import (
    ErrorCode,
    LogicError,
    OCR_PREPROCESS_STARTER_TEMPLATES,
    OcrBinarizeMode,
    OcrPreprocessParams,
)
from _ocr_preprocess import (
    OCR_BORDER_PX,
    OCR_PREVIEW_MAX_EDGE,
    QualityWord,
    apply_ocr_preprocess,
    build_ocr_input_image,
    compute_otsu_threshold,
    evaluate_ocr_quality,
    is_identity_preprocess,
    prepare_ocr_base,
    render_ocr_preprocess_preview,
)


def _png_bytes(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def test_ocr_preprocess_module_has_zero_dependency_on_ocr_or_pytesseract():
    tree = ast.parse(Path(_ocr_preprocess.__file__).read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert "_ocr" not in imported
    assert "_ocr_layout" not in imported
    assert "pytesseract" not in imported


def test_prepare_ocr_base_rejects_empty_corrupt_and_oversized_images(monkeypatch):
    for bad in (b"", b"not-a-valid-image-payload"):
        with pytest.raises(LogicError) as ei:
            prepare_ocr_base(bad)
        assert ei.value.code == ErrorCode.invalid_request

    valid_png = _png_bytes(Image.new("RGB", (32, 32), (10, 20, 30)))
    truncated = valid_png[: len(valid_png) // 2]
    with pytest.raises(LogicError) as trunc_err:
        prepare_ocr_base(truncated)
    assert trunc_err.value.code == ErrorCode.invalid_request

    monkeypatch.setattr(_ocr_preprocess, "OCR_MAX_IMAGE_PIXELS", 100)
    with pytest.raises(LogicError) as big_err:
        prepare_ocr_base(valid_png)
    assert big_err.value.code == ErrorCode.invalid_request
    assert "超过上限" in big_err.value.message

    def raise_bomb(*_a, **_kw):
        raise Image.DecompressionBombError("bomb")

    monkeypatch.setattr(_ocr_preprocess.Image, "open", raise_bomb)
    with pytest.raises(LogicError) as bomb_err:
        prepare_ocr_base(valid_png)
    assert bomb_err.value.code == ErrorCode.invalid_request


def test_identity_and_ordered_preprocess_transforms_including_otsu_zero_variance():
    assert is_identity_preprocess(None) is True
    assert is_identity_preprocess(OcrPreprocessParams()) is True
    # binarize_mode=off 时仅调 threshold 仍属恒等直通
    assert is_identity_preprocess(OcrPreprocessParams(threshold=42)) is True
    assert is_identity_preprocess(OcrPreprocessParams(invert=True)) is False
    assert is_identity_preprocess(OcrPreprocessParams(contrast=1.5)) is False
    assert is_identity_preprocess(OcrPreprocessParams(brightness=1.2)) is False
    assert is_identity_preprocess(OcrPreprocessParams(sharpen=0.5)) is False
    assert is_identity_preprocess(OcrPreprocessParams(binarize_mode=OcrBinarizeMode.otsu)) is False

    # 构造双峰带边界的灰度测试图
    arr = np.full((24, 48), 40, dtype=np.uint8)
    arr[:, 24:] = 210
    gray = Image.fromarray(arr, mode="L")

    passthrough, otsu_none = apply_ocr_preprocess(gray, OcrPreprocessParams())
    assert otsu_none is None
    assert np.array_equal(np.asarray(passthrough), arr)

    inv, _ = apply_ocr_preprocess(gray, OcrPreprocessParams(invert=True))
    assert np.array_equal(np.asarray(inv), 255 - arr)

    # 顺序校验：先 invert 再 brightness(1.5) → 40→215→255, 210→45→67/68；与先亮后反色结果截然不同
    inv_then_bright, _ = apply_ocr_preprocess(gray, OcrPreprocessParams(invert=True, brightness=1.5))
    inv_bright_arr = np.asarray(inv_then_bright)
    assert int(inv_bright_arr[0, 0]) == 255
    assert 60 <= int(inv_bright_arr[0, 40]) <= 75

    # contrast 与 sharpen 产生明确变化
    contrasted, _ = apply_ocr_preprocess(gray, OcrPreprocessParams(contrast=2.0))
    assert int(np.asarray(contrasted)[0, 0]) < 40 and int(np.asarray(contrasted)[0, 40]) > 210
    sharpened, _ = apply_ocr_preprocess(gray, OcrPreprocessParams(sharpen=2.0))
    assert not np.array_equal(np.asarray(sharpened), arr)

    # manual 二值化边界：127 → 0，128 → 255
    edge_gray = Image.fromarray(np.array([[127, 128]], dtype=np.uint8), mode="L")
    manual_img, manual_otsu = apply_ocr_preprocess(
        edge_gray,
        OcrPreprocessParams(binarize_mode=OcrBinarizeMode.manual, threshold=128),
    )
    assert manual_otsu is None
    assert np.asarray(manual_img).tolist() == [[0, 255]]

    # Otsu 双峰分离
    otsu_img, otsu_thr = apply_ocr_preprocess(gray, OcrPreprocessParams(binarize_mode=OcrBinarizeMode.otsu))
    assert otsu_thr is not None and 40 < otsu_thr <= 210
    assert compute_otsu_threshold(arr) == otsu_thr
    otsu_arr = np.asarray(otsu_img)
    assert int(otsu_arr[0, 0]) == 0 and int(otsu_arr[0, 40]) == 255

    # Otsu 零方差纯色图确定性规则：v < 128 → 0，v >= 128 → 255，且返回像素值
    for val, expected_fill in ((0, 0), (60, 0), (127, 0), (128, 255), (220, 255)):
        flat = Image.fromarray(np.full((12, 12), val, dtype=np.uint8), mode="L")
        out_img, thr = apply_ocr_preprocess(flat, OcrPreprocessParams(binarize_mode=OcrBinarizeMode.otsu))
        assert thr == val
        assert np.all(np.asarray(out_img) == expected_fill)


def test_build_ocr_input_image_dimension_invariance_and_border_recompute():
    dark_img = Image.new("RGB", (64, 32), (17, 24, 39))
    ImageDraw.Draw(dark_img).rectangle([16, 8, 48, 24], fill=(220, 224, 232))
    base = prepare_ocr_base(_png_bytes(dark_img))
    expected_size = (64 * 2 + 2 * OCR_BORDER_PX, 32 * 2 + 2 * OCR_BORDER_PX)

    # 恒等参数保留 default_border_fill
    orig_ocr, otsu0 = build_ocr_input_image(base, None)
    assert otsu0 is None and orig_ocr.size == expected_size
    assert orig_ocr.getpixel((0, 0)) == base.default_border_fill

    # 深底图反色后边缘变亮，留白必须重算为 255（而非原暗底留白）
    inverted_ocr, _ = build_ocr_input_image(base, OCR_PREPROCESS_STARTER_TEMPLATES[0].params)
    assert inverted_ocr.size == expected_size
    assert inverted_ocr.getpixel((0, 0)) == 255

    # 浅底图反色后边缘变暗：灰度模式留白用 23，二值化模式留白用 0
    light_base = prepare_ocr_base(_png_bytes(Image.new("RGB", (40, 20), (245, 245, 245))))
    dark_gray_ocr, _ = build_ocr_input_image(light_base, OcrPreprocessParams(invert=True))
    assert dark_gray_ocr.getpixel((0, 0)) == 23
    dark_bin_ocr, _ = build_ocr_input_image(
        light_base,
        OcrPreprocessParams(invert=True, binarize_mode=OcrBinarizeMode.manual, threshold=128),
    )
    assert dark_bin_ocr.getpixel((0, 0)) == 0


def test_render_ocr_preprocess_preview_matches_ocr_pixels_and_respects_max_edge():
    # 小图（放大 2× 后长边 <= 1200）：预览图必须与正式 OCR 输入图去留白区域逐像素完全一致
    src = Image.new("RGB", (120, 60), (24, 30, 44))
    ImageDraw.Draw(src).rectangle([20, 18, 100, 42], fill=(90, 102, 118))
    raw_bytes = _png_bytes(src)
    params = OcrPreprocessParams(
        invert=True,
        contrast=1.8,
        brightness=1.05,
        sharpen=1.2,
        binarize_mode=OcrBinarizeMode.otsu,
    )

    preview = render_ocr_preprocess_preview(raw_bytes, params)
    assert preview.ok is True
    assert preview.media_type == "image/png"
    assert (preview.image_width, preview.image_height) == (120, 60)
    assert (preview.preview_width, preview.preview_height) == (240, 120)

    preview_img = Image.open(io.BytesIO(base64.b64decode(preview.image_base64)))
    base = prepare_ocr_base(raw_bytes)
    ocr_img, ocr_otsu = build_ocr_input_image(base, params, border_px=OCR_BORDER_PX)
    inner = np.asarray(ocr_img)[OCR_BORDER_PX:-OCR_BORDER_PX, OCR_BORDER_PX:-OCR_BORDER_PX]
    assert preview.otsu_threshold == ocr_otsu
    assert np.array_equal(np.asarray(preview_img), inner)

    # 大图（800×400 → 2× = 1600×800 > 1200）：等比缩放到长边 1200；二值化用 NEAREST 保持纯黑白
    big = Image.new("RGB", (800, 400), (255, 255, 255))
    ImageDraw.Draw(big).rectangle([100, 100, 700, 300], fill=(30, 30, 30))
    big_bytes = _png_bytes(big)

    gray_preview = render_ocr_preprocess_preview(big_bytes, OcrPreprocessParams(contrast=1.5))
    assert (gray_preview.preview_width, gray_preview.preview_height) == (OCR_PREVIEW_MAX_EDGE, 600)

    bin_preview = render_ocr_preprocess_preview(
        big_bytes,
        OcrPreprocessParams(binarize_mode=OcrBinarizeMode.otsu),
    )
    assert (bin_preview.preview_width, bin_preview.preview_height) == (OCR_PREVIEW_MAX_EDGE, 600)
    bin_arr = np.asarray(Image.open(io.BytesIO(base64.b64decode(bin_preview.image_base64))))
    assert set(np.unique(bin_arr).tolist()).issubset({0, 255})


def test_evaluate_ocr_quality_formula_and_threshold_boundaries():
    empty = evaluate_ocr_quality((), "")
    assert empty.valid_chars == 0 and empty.score == 0.0 and empty.passed is False

    # 单字符即使高置信度也不达标（valid_chars < 2）
    single = evaluate_ocr_quality([QualityWord("A", 99.0)], "A")
    assert single.valid_chars == 1 and single.score == 49.5 and single.passed is False

    # 门限边界：valid_chars=2, valid_ratio=1.0, weighted_conf=75.0 恰好通过；74.9 不通过
    exact_pass = evaluate_ocr_quality([QualityWord("AB", 75.0)], "AB")
    assert exact_pass.passed is True and exact_pass.score == 75.0
    below_conf = evaluate_ocr_quality([QualityWord("AB", 74.9)], "AB")
    assert below_conf.passed is False

    # 有效字符占比边界：13/20 = 0.65 达标，12/20 = 0.60 不达标
    ratio_ok = evaluate_ocr_quality([QualityWord("a" * 13 + "!" * 7, 90.0)], "a" * 13 + "!" * 7)
    ratio_low = evaluate_ocr_quality([QualityWord("a" * 12 + "!" * 8, 90.0)], "a" * 12 + "!" * 8)
    assert ratio_ok.valid_ratio == pytest.approx(0.65) and ratio_ok.passed is True
    assert ratio_low.valid_ratio == pytest.approx(0.60) and ratio_low.passed is False

    # 中文有效字符计入 valid_chars，加权置信度按词字符长度加权
    cjk = evaluate_ocr_quality([QualityWord("你", 20.0), QualityWord("世界测试", 90.0)], "你 世界测试")
    expected_conf = (20.0 * 1 + 90.0 * 4) / 5.0
    assert cjk.valid_chars == 5 and cjk.valid_ratio == 1.0
    assert cjk.weighted_conf == pytest.approx(expected_conf)
    assert cjk.score == round(expected_conf, 2) and cjk.passed is True
