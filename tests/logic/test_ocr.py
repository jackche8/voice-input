from __future__ import annotations

import io
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

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
    pytesseract_runtime_context,
    resolve_tesseract,
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
    assert missing.value.code == ErrorCode.ocr_failed and "未找到 Tesseract OCR" in missing.value.message


def test_resolve_tesseract_six_level_priority_and_spaced_bundled_path(tmp_path, monkeypatch):
    # 1. 构造含空格的内置目录与 Windows 标准目录
    bundled_root = tmp_path / "Ollama Voice Input" / "_internal"
    bundled_bin = bundled_root / "tesseract" / "tesseract.exe"
    bundled_tessdata = bundled_root / "tesseract" / "tessdata"
    bundled_tessdata.mkdir(parents=True)
    bundled_bin.write_bytes(b"")

    pf64 = tmp_path / "Program Files"
    pf86 = tmp_path / "Program Files (x86)"
    std64_bin = pf64 / "Tesseract-OCR" / "tesseract.exe"
    std64_td = pf64 / "Tesseract-OCR" / "tessdata"
    std64_td.mkdir(parents=True)
    std64_bin.write_bytes(b"")
    std86_bin = pf86 / "Tesseract-OCR" / "tesseract.exe"
    std86_bin.parent.mkdir(parents=True)
    std86_bin.write_bytes(b"")

    fake_pt_inner = SimpleNamespace(tesseract_cmd="tesseract")
    fake_pt = SimpleNamespace(pytesseract=fake_pt_inner)
    monkeypatch.setitem(sys.modules, "pytesseract", fake_pt)

    env_upper = {"PROGRAMFILES": str(pf64), "PROGRAMFILES(X86)": str(pf86)}

    # 级 1：显式入参最优先
    assert resolve_tesseract(
        r"C:\Custom\tesseract.exe",
        program_root=bundled_root,
        which=lambda _n: "/usr/bin/tesseract",
        os_name="windows",
        environ=env_upper,
    ) == (r"C:\Custom\tesseract.exe", None)

    # 级 2：显式非默认 pytesseract.tesseract_cmd
    fake_pt_inner.tesseract_cmd = r"D:\Tools\tesseract.exe"
    assert resolve_tesseract(
        program_root=bundled_root,
        which=lambda _n: "/usr/bin/tesseract",
        os_name="windows",
        environ=env_upper,
    ) == (r"D:\Tools\tesseract.exe", None)

    # 级 3：默认 pytesseract.tesseract_cmd 时优先命中内置 PROGRAM_ROOT/tesseract/tesseract.exe + 同级 tessdata
    fake_pt_inner.tesseract_cmd = "tesseract"
    assert resolve_tesseract(
        program_root=bundled_root,
        which=lambda _n: "/usr/bin/tesseract",
        os_name="windows",
        environ=env_upper,
    ) == (str(bundled_bin), bundled_tessdata)

    # 级 4：无内置时回落 which("tesseract")
    empty_root = tmp_path / "empty-root"
    assert resolve_tesseract(
        program_root=empty_root,
        which=lambda _n: r"C:\Bin\tesseract.exe",
        os_name="windows",
        environ=env_upper,
    ) == (r"C:\Bin\tesseract.exe", None)

    # 级 5：which 未命中时查 Windows Program Files → Program Files (x86)
    assert resolve_tesseract(
        program_root=empty_root,
        which=lambda _n: None,
        os_name="windows",
        environ=env_upper,
    ) == (str(std64_bin), std64_td)
    std64_bin.unlink()
    assert resolve_tesseract(
        program_root=empty_root,
        which=lambda _n: None,
        os_name="windows",
        environ=env_upper,
    ) == (str(std86_bin), None)

    # 级 6：全部未命中时兜底 "tesseract"
    assert resolve_tesseract(
        program_root=empty_root,
        which=lambda _n: None,
        os_name="windows",
        environ={},
    ) == ("tesseract", None)


def test_bundled_tessdata_uses_env_var_never_config_flag_and_restores_context(tmp_path, monkeypatch):
    bundled_root = tmp_path / "Ollama Voice Input" / "_internal"
    bundled_bin = bundled_root / "tesseract" / "tesseract.exe"
    bundled_td = bundled_root / "tesseract" / "tessdata"
    bundled_td.mkdir(parents=True)
    bundled_bin.write_bytes(b"")

    seen_calls: list[dict] = []

    def spy_run(cmd, **kw):
        seen_calls.append({"cmd": list(cmd), **kw})
        return subprocess.CompletedProcess(cmd, 0, "List of available languages (3):\nchi_sim\nchi_tra\neng\n", "")

    fake_pt_inner = SimpleNamespace(tesseract_cmd="tesseract")
    seen_pt: list[dict] = []

    class TesseractNotFoundError(Exception):
        pass

    def fake_image_to_string(img, *, lang, timeout=0, **kw):
        seen_pt.append({
            "cmd": fake_pt_inner.tesseract_cmd,
            "tessdata_prefix": os.environ.get("TESSDATA_PREFIX"),
            "extra_kw": dict(kw),
        })
        return "你好 World"

    fake_pt = SimpleNamespace(
        pytesseract=fake_pt_inner,
        image_to_string=fake_image_to_string,
        TesseractNotFoundError=TesseractNotFoundError,
    )
    monkeypatch.setitem(sys.modules, "pytesseract", fake_pt)
    monkeypatch.setattr(_ocr, "PROGRAM_ROOT", bundled_root)
    monkeypatch.setattr(_ocr, "detect_os", lambda: "windows")
    monkeypatch.setenv("TESSDATA_PREFIX", "/original/tessdata")

    langs = list_tesseract_languages(run=spy_run, program_root=bundled_root, os_name="windows", environ={})
    assert langs == ["chi_sim", "chi_tra", "eng"]
    assert seen_calls[0]["cmd"] == [str(bundled_bin), "--list-langs"]
    assert "--tessdata-dir" not in " ".join(seen_calls[0]["cmd"])
    assert seen_calls[0]["env"]["TESSDATA_PREFIX"] == str(bundled_td)

    out = image_to_text(_tiny_png(), lang="chi_sim+eng", get_languages=lambda: ["chi_sim", "eng"])
    assert out == "你好 World"
    assert seen_pt == [{
        "cmd": str(bundled_bin),
        "tessdata_prefix": str(bundled_td),
        "extra_kw": {},
    }]
    # 退出上下文后必须完整还原 tesseract_cmd 与 TESSDATA_PREFIX（无泄漏）
    assert fake_pt_inner.tesseract_cmd == "tesseract"
    assert os.environ.get("TESSDATA_PREFIX") == "/original/tessdata"

    # 异常退出上下文也必须还原且映射中文报错
    def missing_runner(img, *, lang, timeout=0):
        raise TesseractNotFoundError("tesseract is not installed or it's not in your PATH")

    fake_pt.image_to_string = missing_runner
    with pytest.raises(LogicError) as not_found:
        image_to_text(_tiny_png(), lang="eng", get_languages=lambda: ["eng"])
    assert not_found.value.code == ErrorCode.ocr_failed and "未找到 Tesseract OCR" in not_found.value.message
    assert fake_pt_inner.tesseract_cmd == "tesseract"
    assert os.environ.get("TESSDATA_PREFIX") == "/original/tessdata"

    # 未预设 TESSDATA_PREFIX 时退出后应清空（不残留）
    monkeypatch.delenv("TESSDATA_PREFIX", raising=False)
    with pytesseract_runtime_context(fake_pt, program_root=bundled_root, os_name="windows"):
        assert os.environ.get("TESSDATA_PREFIX") == str(bundled_td)
    assert "TESSDATA_PREFIX" not in os.environ


def test_ocr_reexports_preprocess_constants_rejects_corrupt_image_and_exposes_data_runner(monkeypatch):
    import _ocr_preprocess

    assert _ocr.OCR_BORDER_PX == _ocr_preprocess.OCR_BORDER_PX
    assert _ocr.OCR_UPSCALE == _ocr_preprocess.OCR_UPSCALE
    assert _ocr.OCR_UPSCALE_MAX_PIXELS == _ocr_preprocess.OCR_UPSCALE_MAX_PIXELS

    with pytest.raises(LogicError) as bad_img:
        prepare_ocr_image(b"not-a-png")
    assert bad_img.value.code == ErrorCode.invalid_request

    fake_inner = SimpleNamespace(tesseract_cmd="tesseract")
    seen: list[dict] = []

    def fake_to_data(img, *, lang, output_type, timeout=0):
        seen.append({"lang": lang, "output_type": output_type, "timeout": timeout, "size": img.size})
        return {"text": ["OK"]}

    fake_pt = SimpleNamespace(
        pytesseract=fake_inner,
        Output=SimpleNamespace(DICT="dict"),
        image_to_data=fake_to_data,
    )
    monkeypatch.setitem(sys.modules, "pytesseract", fake_pt)
    runner = _ocr.default_data_runner()
    res = runner(Image.new("L", (10, 10), 255), lang="eng", timeout=12)
    assert res == {"text": ["OK"]}
    assert seen == [{"lang": "eng", "output_type": "dict", "timeout": 12, "size": (10, 10)}]


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
