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
import _ocr_layout
from _contracts import (
    ErrorCode,
    LogicError,
    OCR_PREPROCESS_STARTER_TEMPLATES,
    OcrBinarizeMode,
    OcrPreprocessParams,
    OcrPreprocessPreset,
    OcrRunMode,
)
from _ocr import OCR_BORDER_PX, OCR_TIMEOUT_S, clear_language_cache, list_tesseract_languages
from _ocr_layout import (
    extract_ocr_layout,
    extract_ocr_layout_with_meta,
    extract_ocr_text_with_meta,
    sample_ring_colors,
)
from tests.logic.ocr_samples import bbox_close, dark_low_contrast_sample, fonts_missing, text_similar


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


def _word_data(word: str, conf: float, *, x: int = 12, y: int = 12, w: int = 60, h: int = 16) -> dict:
    if not word:
        return {"level": [5], "text": [""], "conf": [-1]}
    pad, scale = OCR_BORDER_PX, 2
    return {
        "level": [5],
        "page_num": [1],
        "block_num": [1],
        "par_num": [1],
        "line_num": [1],
        "left": [pad + scale * x],
        "top": [pad + scale * y],
        "width": [scale * w],
        "height": [scale * h],
        "conf": [conf],
        "text": [word],
    }


def _make_preset(pid: str, name: str, *, enabled: bool = True, **params) -> OcrPreprocessPreset:
    return OcrPreprocessPreset(id=pid, name=name, enabled=enabled, **params)


def test_sequential_engine_all_five_run_meta_states_and_tie_breaker(monkeypatch):
    img_bytes = _png_bytes(Image.new("RGB", (120, 60), (255, 255, 255)))
    ticks = iter([10.0, 10.042] * 12)
    clock = lambda: next(ticks)  # noqa: E731

    # 态 1：0 启用预设（含停用预设）→ 仅跑 1 次默认原图
    calls_1: list[int] = []

    def runner_1(_img, lang, timeout=0):
        calls_1.append(1)
        return _word_data("Original", 88.0)

    r1 = extract_ocr_layout_with_meta(
        img_bytes,
        lang="eng",
        runner_data=runner_1,
        languages_fn=lambda: ["eng"],
        presets=[_make_preset("off1", "已停用", enabled=False, invert=True)],
        clock=clock,
    )
    assert len(calls_1) == 1
    assert r1.ocr_text == "Original"
    assert r1.meta.run_mode == OcrRunMode.original
    assert r1.meta.matched_preset_id is None
    assert r1.meta.matched_preset_name == "默认原图"
    assert r1.meta.matched_rank is None
    assert r1.meta.early_stopped is False and r1.meta.fallback_to_original is False
    assert r1.meta.elapsed_ms == 42 and r1.meta.confidence == 88
    assert [(a.label, a.rank, a.accepted) for a in r1.meta.attempts] == [("默认原图", None, True)]

    # 态 2：草稿试识别 → 仅跑 1 次草稿，且 extract_ocr_text_with_meta 不采环形底色
    color_calls = {"n": 0}
    orig_sample = _ocr_layout.sample_ring_colors

    def spy_color(rgb_arr, bbox):
        color_calls["n"] += 1
        return orig_sample(rgb_arr, bbox)

    monkeypatch.setattr(_ocr_layout, "sample_ring_colors", spy_color)
    calls_2: list[int] = []

    def runner_2(_img, lang, timeout=0):
        calls_2.append(1)
        return _word_data("DraftText", 91.0)

    r2 = extract_ocr_text_with_meta(
        img_bytes,
        lang="eng",
        runner_data=runner_2,
        languages_fn=lambda: ["eng"],
        presets=[_make_preset("p1", "预设一", enabled=True, invert=True)],
        preprocess_params=OcrPreprocessParams(contrast=1.8),
        clock=clock,
    )
    assert len(calls_2) == 1 and color_calls["n"] == 0
    assert r2.text == "DraftText"
    assert r2.meta.run_mode == OcrRunMode.draft_params
    assert r2.meta.matched_preset_id is None
    assert r2.meta.matched_preset_name == "当前调参草稿"
    assert r2.meta.matched_rank is None
    assert r2.meta.early_stopped is False and r2.meta.fallback_to_original is False
    assert [(a.label, a.rank, a.accepted) for a in r2.meta.attempts] == [("当前调参草稿", None, True)]

    # 态 3：3 条启用预设中顺位 2 达标 → 早停，仅跑 2 次（不跑顺位 3 与原图）
    presets_3 = [
        _make_preset("p1", "预设一", enabled=True, invert=True),
        _make_preset("p2", "预设二", enabled=True, contrast=2.0),
        _make_preset("p3", "预设三", enabled=True, binarize_mode=OcrBinarizeMode.otsu),
    ]
    seq_3 = iter([_word_data("a!", 40.0), _word_data("Success", 86.0), _word_data("ShouldNotRun", 99.0)])
    calls_3: list[int] = []

    def runner_3(_img, lang, timeout=0):
        calls_3.append(1)
        return next(seq_3)

    r3 = extract_ocr_layout_with_meta(
        img_bytes,
        lang="eng",
        runner_data=runner_3,
        languages_fn=lambda: ["eng"],
        presets=presets_3,
        clock=clock,
    )
    assert len(calls_3) == 2
    assert r3.ocr_text == "Success"
    assert r3.meta.run_mode == OcrRunMode.preset_chain
    assert r3.meta.matched_preset_id == "p2" and r3.meta.matched_preset_name == "预设二"
    assert r3.meta.matched_rank == 2
    assert r3.meta.early_stopped is True and r3.meta.fallback_to_original is False
    assert [(a.preset_id, a.rank, a.accepted) for a in r3.meta.attempts] == [
        ("p1", 1, False),
        ("p2", 2, True),
    ]

    # 态 4：全部预设未达标 → 补跑原图 → 原图得分更高胜出
    presets_2 = presets_3[:2]
    seq_4 = iter([_word_data("weak", 55.0), _word_data("", -1), _word_data("BetterOrig", 72.0)])
    r4 = extract_ocr_layout_with_meta(
        img_bytes,
        lang="eng",
        runner_data=lambda _img, lang, timeout=0: next(seq_4),
        languages_fn=lambda: ["eng"],
        presets=presets_2,
        clock=clock,
    )
    assert r4.ocr_text == "BetterOrig"
    assert r4.meta.run_mode == OcrRunMode.preset_chain
    assert r4.meta.matched_preset_id is None and r4.meta.matched_preset_name == "默认原图"
    assert r4.meta.matched_rank is None
    assert r4.meta.early_stopped is False and r4.meta.fallback_to_original is True
    assert [(a.preset_id, a.rank, a.accepted) for a in r4.meta.attempts] == [
        ("p1", 1, False),
        ("p2", 2, False),
        (None, None, True),
    ]

    # 态 5：全部预设未达标 → 补跑原图 → 顺位 1 预设得分胜出（含同分保先序校验）
    seq_5 = iter([_word_data("FirstWin", 70.0), _word_data("SameTie!", 70.0), _word_data("FirstWin", 70.0)])
    r5 = extract_ocr_layout_with_meta(
        img_bytes,
        lang="eng",
        runner_data=lambda _img, lang, timeout=0: next(seq_5),
        languages_fn=lambda: ["eng"],
        presets=presets_2,
        clock=clock,
    )
    assert r5.ocr_text == "FirstWin"
    assert r5.meta.run_mode == OcrRunMode.preset_chain
    assert r5.meta.matched_preset_id == "p1" and r5.meta.matched_preset_name == "预设一"
    assert r5.meta.matched_rank == 1
    assert r5.meta.early_stopped is False and r5.meta.fallback_to_original is True
    assert [(a.preset_id, a.rank, a.accepted) for a in r5.meta.attempts] == [
        ("p1", 1, True),
        ("p2", 2, False),
        (None, None, False),
    ]

    # 全部为空（含补跑原图）→ 显式抛 ocr_failed
    with pytest.raises(LogicError) as all_empty:
        extract_ocr_layout_with_meta(
            img_bytes,
            lang="eng",
            runner_data=lambda _img, lang, timeout=0: _word_data("", -1),
            languages_fn=lambda: ["eng"],
            presets=presets_2,
        )
    assert all_empty.value.code == ErrorCode.ocr_failed


def test_sequential_engine_rejects_over_limit_presets_and_propagates_hard_errors():
    img_bytes = _png_bytes(Image.new("RGB", (80, 40), "white"))
    four_enabled = [_make_preset(f"p{i}", f"预设{i}", enabled=True) for i in range(4)]

    called = {"n": 0}

    def never_called(_img, lang, timeout=0):
        called["n"] += 1
        return _word_data("OK", 95.0)

    with pytest.raises(LogicError) as over:
        extract_ocr_layout_with_meta(
            img_bytes,
            lang="eng",
            runner_data=never_called,
            languages_fn=lambda: ["eng"],
            presets=four_enabled,
        )
    assert over.value.code == ErrorCode.invalid_request and called["n"] == 0

    with pytest.raises(LogicError) as corrupt:
        extract_ocr_layout_with_meta(
            b"corrupt-bytes",
            lang="eng",
            runner_data=never_called,
            languages_fn=lambda: ["eng"],
        )
    assert corrupt.value.code == ErrorCode.invalid_request and called["n"] == 0

    presets_2 = four_enabled[:2]
    for exc, expected_code in (
        (RuntimeError("Tesseract process timeout"), ErrorCode.timeout),
        (RuntimeError("Failed loading language 'jpn'"), ErrorCode.ocr_language_missing),
        (FileNotFoundError("tesseract"), ErrorCode.ocr_failed),
    ):
        attempts = {"n": 0}

        def failing_runner(_img, lang, timeout=0, _exc=exc):
            attempts["n"] += 1
            raise _exc

        with pytest.raises(LogicError) as hard:
            extract_ocr_layout_with_meta(
                img_bytes,
                lang="eng",
                runner_data=failing_runner,
                languages_fn=lambda: ["eng"],
                presets=presets_2,
            )
        assert hard.value.code == expected_code
        assert attempts["n"] == 1  # 硬错误立即终止，绝不吞错继续下一预设或原图


def test_preset_invert_preserves_original_rgb_color_sampling_and_coordinates(monkeypatch):
    # 原图深底 #181e2c；预设 #1（反色+二值化）未达标，预设 #2（反色）达标
    img = Image.new("RGB", (140, 60), (24, 30, 44))
    ImageDraw.Draw(img).rectangle([20, 18, 90, 36], fill=(210, 215, 224))
    img_bytes = _png_bytes(img)

    presets = [
        _make_preset("p1", "反色二值", enabled=True, invert=True, binarize_mode=OcrBinarizeMode.otsu),
        _make_preset("p2", "深底反色", enabled=True, invert=True, contrast=1.6),
    ]
    seen_inputs: list[tuple[tuple[int, int], int]] = []
    responses = iter([_word_data("x", 40.0), _word_data("Recovered", 92.0)])

    def spy_runner(ocr_img: Image.Image, lang: str, timeout: int = 0) -> dict:
        seen_inputs.append((ocr_img.size, int(ocr_img.getpixel((0, 0)))))
        return next(responses)

    color_calls = {"n": 0}
    orig_sample = _ocr_layout.sample_ring_colors

    def spy_sample(rgb_arr, bbox):
        color_calls["n"] += 1
        return orig_sample(rgb_arr, bbox)

    monkeypatch.setattr(_ocr_layout, "sample_ring_colors", spy_sample)

    res = extract_ocr_layout_with_meta(
        img_bytes,
        lang="eng",
        runner_data=spy_runner,
        languages_fn=lambda: ["eng"],
        presets=presets,
    )
    expected_size = (140 * 2 + 2 * OCR_BORDER_PX, 60 * 2 + 2 * OCR_BORDER_PX)
    # 两次尝试尺寸恒定且反色后四周留白均已重算为 255
    assert seen_inputs == [(expected_size, 255), (expected_size, 255)]
    # 仅对最终胜出结果采一次原图环形底色，且底色仍是原图深蓝 #181e2c（未受反色污染）
    assert color_calls["n"] == 1
    assert len(res.blocks) == 1
    assert res.blocks[0].bbox == (12, 12, 60, 16)
    assert res.blocks[0].bg_color == "#181e2c"
    assert res.blocks[0].fg_color == "#f8fafc"


def test_live_tesseract_dark_low_contrast_sample_early_stops_and_preserves_orig_colors():
    reason = fonts_missing()
    if reason:
        pytest.skip(reason)
    clear_language_cache()
    try:
        installed = set(list_tesseract_languages())
    except LogicError as exc:
        pytest.skip(f"Tesseract 不可用: {exc.message}")
    if not {"chi_sim", "chi_tra", "eng"}.issubset(installed):
        pytest.skip("本机缺少默认 OCR 语言包")

    sample = dark_low_contrast_sample()
    # 0 启用预设：默认原图在深底弱对比度图上识别失败
    with pytest.raises(LogicError) as raw_err:
        extract_ocr_layout_with_meta(sample.png)
    assert raw_err.value.code == ErrorCode.ocr_failed

    # 启用 3 套内置起步预设：顺位 2（低对比灰字增强）命中早停、坐标对齐、原图深底采色准确
    starter_presets = [
        OcrPreprocessPreset(id=f"starter-{t.key}", name=t.suggested_name, enabled=True, **t.params.model_dump())
        for t in OCR_PREPROCESS_STARTER_TEMPLATES
    ]
    layout_res = extract_ocr_layout_with_meta(sample.png, presets=starter_presets)
    assert layout_res.meta.run_mode == OcrRunMode.preset_chain
    assert layout_res.meta.early_stopped is True
    assert layout_res.meta.fallback_to_original is False
    assert layout_res.meta.matched_rank == 2
    assert len(layout_res.meta.attempts) == 2
    assert layout_res.blocks and text_similar(layout_res.blocks[0].lines[0].text, sample.lines[0].text)
    assert bbox_close(layout_res.blocks[0].lines[0].bbox, sample.lines[0].bbox)
    assert layout_res.blocks[0].bg_color == "#181e2c"
    assert layout_res.blocks[0].fg_color == "#f8fafc"

    text_res = extract_ocr_text_with_meta(sample.png, presets=starter_presets)
    assert text_res.text == layout_res.ocr_text
    assert text_res.meta.matched_preset_id == layout_res.meta.matched_preset_id
    assert text_res.meta.early_stopped is True
