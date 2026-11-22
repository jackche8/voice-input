from __future__ import annotations

import json
import math
import struct
from pathlib import Path

import httpx
import pytest

from _contracts import OcrTextBlock, OcrTextLine
from _ocr_layout import extract_ocr_layout
from _ollama import OllamaClient
from _translate import translate_blocks as real_translate_blocks, translate_text as real_translate_text
from _v3_app import build_sandbox_runtime, create_app
from tests.api.conftest import TEST_TOKEN, lan_client, lan_headers, local_client, local_headers
from tests.api.providers import fixture_ocr_layout, fixture_transcribe, make_text_wav, make_wav_bytes
from tests.logic.ocr_samples import all_samples, bbox_close, fonts_missing, text_similar


def _sine_pcm16(sample_rate: int, *, duration_s: float = 0.05, channels: int = 1) -> bytes:
    n = int(sample_rate * duration_s)
    frames = bytearray()
    for i in range(n):
        sample = int(12000 * math.sin(2 * math.pi * 440.0 * i / sample_rate))
        for _ in range(channels):
            frames.extend(struct.pack("<h", sample))
    return bytes(frames)


def test_public_stt_does_not_write_history_or_queue(app, runtime):
    client = local_client(app)
    res = client.post(
        "/api/v1/stt",
        data={"language": "zh"},
        files={"audio": ("a.wav", make_text_wav("only-stt"), "audio/wav")},
        headers=local_headers(),
    )
    assert res.status_code == 200
    assert res.json()["ok"] is True
    assert res.json()["text"] == "only-stt"
    assert runtime.history.list_records().total == 0
    assert runtime.jobs.get("anything").found is False


def test_stt_decodes_non_16k_container_and_rejects_corrupt(tmp_path: Path):
    seen_pcm: list[bytes] = []

    def capture_transcribe(audio: bytes, *, language: str | None = None, context: str | None = None):
        seen_pcm.append(audio)
        return fixture_transcribe(audio, language=language, context=context)

    runtime = build_sandbox_runtime(
        root=tmp_path / "codec",
        token=TEST_TOKEN,
        transcribe=capture_transcribe,
    )
    app = create_app(runtime)
    client = local_client(app)

    wav_48k_stereo = make_wav_bytes(_sine_pcm16(48000, duration_s=0.05, channels=2), sample_rate=48000, channels=2)
    res = client.post(
        "/api/v1/stt",
        data={"language": "zh"},
        files={"audio": ("rec.wav", wav_48k_stereo, "audio/wav")},
        headers=local_headers(),
    )
    assert res.status_code == 200, res.text
    assert len(seen_pcm) == 1
    assert len(seen_pcm[0]) % 2 == 0
    # 48kHz stereo 0.05s 重采样为 16kHz mono s16le 约 1600 字节
    assert 1200 <= len(seen_pcm[0]) <= 2000

    corrupt = client.post(
        "/api/v1/stt",
        files={"audio": ("bad.webm", b"not-a-valid-webm-container", "audio/webm")},
        headers=local_headers(),
    )
    assert corrupt.status_code == 400
    assert corrupt.json()["error"] == "invalid_request"


def test_stt_silence_and_missing_audio_are_400(app):
    client = local_client(app)
    silent_pcm = client.post(
        "/api/v1/stt",
        files={"audio": ("s.pcm", b"\x00\x00\x00\x00", "audio/pcm")},
        headers=local_headers(),
    )
    assert silent_pcm.status_code == 400
    silent_wav = client.post(
        "/api/v1/stt",
        files={"audio": ("s.wav", make_wav_bytes(b"\x00\x00" * 160), "audio/wav")},
        headers=local_headers(),
    )
    assert silent_wav.status_code == 400
    missing = client.post("/api/v1/stt", data={"language": "zh"}, headers=local_headers())
    assert missing.status_code == 400


def test_polish_and_translate_use_injected_capabilities(app):
    client = local_client(app)
    polish = client.post("/api/v1/polish", json={"text": "有错字", "language": "zh"}, headers=local_headers())
    assert polish.status_code == 200
    assert polish.json()["text"] == "有正字"
    trans = client.post("/api/v1/translate", json={"text": "hello", "lang_pair": "zh-en"}, headers=local_headers())
    assert trans.status_code == 200
    body = trans.json()
    assert body["text"] == "[en->zh]hello"
    assert (body["lang_pair"], body["source_lang"], body["target_lang"], body["direction_mode"], body["chunks"]) == (
        "zh-en",
        "en",
        "zh",
        "auto",
        1,
    )


def test_missing_polish_capability_is_structured_error(tmp_path: Path):
    runtime = build_sandbox_runtime(
        root=tmp_path / "no-polish",
        token=TEST_TOKEN,
        transcribe=fixture_transcribe,
        polish=None,
    )
    app = create_app(runtime)
    client = local_client(app)
    res = client.post("/api/v1/polish", json={"text": "原文"}, headers=local_headers())
    assert res.status_code == 503
    body = res.json()
    assert body["error"] == "polish_failed"
    assert body["ok"] is False
    assert body.get("text") != "原文"


def test_ocr_translate_raw_and_translated(app):
    client = local_client(app)
    raw = client.post(
        "/api/v1/ocr-translate",
        data={"mode": "raw"},
        files={"image": ("t.txt", "画面文字".encode("utf-8"), "text/plain")},
        headers=local_headers(),
    )
    assert raw.status_code == 200
    raw_body = raw.json()
    assert raw_body["mode"] == "raw"
    assert raw_body["ocr_text"] == "画面文字"
    assert raw_body["text"] == "画面文字"
    assert raw_body["lang_pair"] is None and raw_body["source_lang"] is None and raw_body["target_lang"] is None
    assert raw_body["direction_mode"] is None and raw_body["chunks"] is None

    translated = client.post(
        "/api/v1/ocr-translate",
        data={"mode": "translate", "lang_pair": "zh-en"},
        files={"image": ("t.txt", "画面文字".encode("utf-8"), "text/plain")},
        headers=local_headers(),
    )
    assert translated.status_code == 200
    tr_body = translated.json()
    assert tr_body["mode"] == "translate"
    assert tr_body["ocr_text"] == "画面文字"
    assert tr_body["text"] == "[zh->en]画面文字"
    assert (tr_body["lang_pair"], tr_body["source_lang"], tr_body["target_lang"], tr_body["direction_mode"], tr_body["chunks"]) == (
        "zh-en",
        "zh",
        "en",
        "auto",
        1,
    )


def test_ocr_inplace_translated_and_raw_modes(app, tmp_path: Path):
    client = local_client(app)
    translated = client.post(
        "/api/v1/ocr-inplace",
        data={"mode": "translate", "lang_pair": "zh-en"},
        files={"image": ("t.png", "第一行\n---\n第二行".encode("utf-8"), "image/png")},
        headers=local_headers(),
    )
    assert translated.status_code == 200, translated.text
    body = translated.json()
    assert body["ok"] is True
    assert body["mode"] == "translate"
    assert (body["lang_pair"], body["source_lang"], body["target_lang"], body["direction_mode"]) == (
        "zh-en",
        "zh",
        "en",
        "auto",
    )
    assert body["image_width"] == 320 and body["image_height"] == 180
    assert body["ocr_text"] == "第一行\n\n---\n\n第二行"  # 全文块间空一行
    # 沙箱 _default_translate_blocks 同步跳过纯符号行段 "---" 原样保留，其余走整图单方向 zh->en
    assert body["text"] == "[zh->en]第一行\n\n---\n\n[zh->en]第二行"
    assert len(body["blocks"]) == 3
    assert body["blocks"][0]["lines"] == [{"bbox": [12, 16, 200, 28], "text": "第一行", "soft_wrap": False}]
    assert body["blocks"][0]["translated_text"] == "[zh->en]第一行"
    assert body["blocks"][1]["translated_text"] == "---"
    assert body["blocks"][2]["translated_text"] == "[zh->en]第二行"
    assert body["blocks"][0]["bg_color"] == "#ffffff"
    assert body["blocks"][1]["bg_color"] == "#111827"

    # raw 模式直通原文，不触发翻译能力
    translate_calls = []

    def fail_translate(blocks, *, source_lang, target_lang, lang_pair="zh-en"):
        translate_calls.append((lang_pair, source_lang, target_lang))
        raise AssertionError("raw 模式不得调用翻译")

    rt_raw = build_sandbox_runtime(
        root=tmp_path / "raw-only",
        token=TEST_TOKEN,
        ocr_layout=fixture_ocr_layout,
        translate_blocks=fail_translate,
    )
    raw_client = local_client(create_app(rt_raw))
    raw = raw_client.post(
        "/api/v1/ocr-inplace",
        data={"mode": "raw"},
        files={"image": ("t.png", "原文A\n原文B".encode("utf-8"), "image/png")},
        headers=local_headers(),
    )
    assert raw.status_code == 200, raw.text
    raw_body = raw.json()
    assert raw_body["mode"] == "raw"
    assert raw_body["lang_pair"] is None and raw_body["source_lang"] is None and raw_body["target_lang"] is None
    assert raw_body["direction_mode"] is None
    assert raw_body["text"] == raw_body["ocr_text"] == "原文A\n\n原文B"
    assert [b["translated_text"] for b in raw_body["blocks"]] == ["原文A", "原文B"]
    assert translate_calls == []


def test_ocr_inplace_real_layout_returns_truth_lines_and_blank_line_full_text(tmp_path: Path):
    """T14 真实链路：生成器样本 → 真实 extract_ocr_layout 的 sandbox app → POST /api/v1/ocr-inplace（raw）。"""
    pytesseract = pytest.importorskip("pytesseract")
    try:
        if "eng" not in pytesseract.get_languages(config=""):
            pytest.skip("本机未安装 tesseract eng 语言包")
    except Exception as exc:
        pytest.skip(f"无法查询 tesseract 语言包: {exc}")
    if fonts_missing():
        pytest.skip(fonts_missing())
    sample = all_samples()["light_paragraph"]
    rt = build_sandbox_runtime(root=tmp_path / "real-ocr", token=TEST_TOKEN, ocr_layout=extract_ocr_layout)
    res = local_client(create_app(rt)).post(
        "/api/v1/ocr-inplace",
        data={"mode": "raw", "lang": "eng"},  # 显式 lang：链路不依赖默认语言包是否齐全
        files={"image": ("s.png", sample.png, "image/png")},
        headers=local_headers(),
    )
    assert res.status_code == 200, res.text
    body = res.json()
    lines = [(index, line) for index, block in enumerate(body["blocks"]) for line in block["lines"]]
    assert len(lines) == len(sample.lines)
    for (index, line), truth in zip(lines, sample.lines):
        assert index == truth.block and line["soft_wrap"] == truth.soft_wrap, (truth.text, line)
        assert bbox_close(tuple(line["bbox"]), truth.bbox) and text_similar(line["text"], truth.text), (truth, line)
    assert body["ocr_text"] == body["text"] == "\n\n".join(block["ocr_text"] for block in body["blocks"])
    assert all("\n\n" not in block["ocr_text"] for block in body["blocks"])


def test_ocr_inplace_empty_and_missing_capability(app, tmp_path: Path):
    client = local_client(app)
    empty = client.post(
        "/api/v1/ocr-inplace",
        data={"mode": "translate", "lang_pair": "zh-en"},
        files={"image": ("t.png", b"empty-image", "image/png")},
        headers=local_headers(),
    )
    assert empty.status_code == 503
    assert empty.json()["error"] == "ocr_failed"

    rt = build_sandbox_runtime(root=tmp_path / "no-ocr-layout", token=TEST_TOKEN)
    missing = local_client(create_app(rt)).post(
        "/api/v1/ocr-inplace",
        data={"mode": "translate", "lang_pair": "zh-en"},
        files={"image": ("t.png", b"hi", "image/png")},
        headers=local_headers(),
    )
    assert missing.status_code == 503
    assert missing.json()["error"] == "ocr_failed"


def test_ocr_empty_is_error(app):
    client = local_client(app)
    res = client.post(
        "/api/v1/ocr-translate",
        data={"mode": "translate", "lang_pair": "zh-en"},
        files={"image": ("t.txt", b"empty-image", "text/plain")},
        headers=local_headers(),
    )
    assert res.status_code == 503
    assert res.json()["error"] == "ocr_failed"


def test_lan_can_call_stt_and_cannot_call_ocr(app):
    client = lan_client(app)
    stt = client.post(
        "/api/v1/stt",
        files={"audio": ("a.wav", make_text_wav("lan-hi"), "audio/wav")},
        headers=lan_headers(),
    )
    assert stt.status_code == 200
    ocr = client.post(
        "/api/v1/ocr-translate",
        data={"mode": "raw"},
        files={"image": ("t.txt", "画面文字".encode("utf-8"), "text/plain")},
        headers=lan_headers(),
    )
    assert ocr.status_code == 403
    inplace = client.post(
        "/api/v1/ocr-inplace",
        data={"mode": "raw"},
        files={"image": ("t.png", "画面文字".encode("utf-8"), "image/png")},
        headers=lan_headers(),
    )
    assert inplace.status_code == 403


def test_translate_real_chain_auto_manual_chunking_length_503_and_invalid_400(tmp_path: Path):
    """真链路：真 FastAPI + 真 _translate.translate_text + MockTransport（零路由 mock）。"""
    state = {"truncate": False}
    seen_payloads: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        seen_payloads.append(body)
        user_msg = body["messages"][1]["content"]
        inner = user_msg.split("<source_text>\n", 1)[1].rsplit("\n</source_text>", 1)[0]
        if state["truncate"]:
            return httpx.Response(200, json={"message": {"role": "assistant", "content": "cut"}, "done_reason": "length"})
        return httpx.Response(200, json={"message": {"role": "assistant", "content": f"OUT:{inner}"}, "done_reason": "stop"})

    with OllamaClient(transport=httpx.MockTransport(handler)) as ol:
        rt = build_sandbox_runtime(
            root=tmp_path / "real-tr",
            token=TEST_TOKEN,
            translate=lambda req: real_translate_text(req, model="qwen3.5:4b", client=ol),
        )
        client = local_client(create_app(rt))

        # 1. 中英混排自动判 zh→en
        r_mixed = client.post(
            "/api/v1/translate",
            json={"text": "请把 FastAPI 和 WebSocket 修一下", "lang_pair": "zh-en"},
            headers=local_headers(),
        )
        assert r_mixed.status_code == 200, r_mixed.text
        bm = r_mixed.json()
        assert (bm["source_lang"], bm["target_lang"], bm["direction_mode"], bm["chunks"]) == ("zh", "en", "auto", 1)

        # 2. 手动反转方向
        r_manual = client.post(
            "/api/v1/translate",
            json={"text": "你好", "lang_pair": "zh-en", "source_lang": "en", "target_lang": "zh"},
            headers=local_headers(),
        )
        assert r_manual.status_code == 200, r_manual.text
        assert r_manual.json()["direction_mode"] == "manual"
        assert (r_manual.json()["source_lang"], r_manual.json()["target_lang"]) == ("en", "zh")

        # 3. 长文本分段返回 chunks > 1 且不截断
        long_src = ("第一段长文本用来触发自动分段翻译。" * 25) + "\n\n" + ("第二段长文本内容。" * 25)
        r_long = client.post(
            "/api/v1/translate",
            json={"text": long_src, "lang_pair": "zh-en"},
            headers=local_headers(),
        )
        assert r_long.status_code == 200, r_long.text
        assert r_long.json()["chunks"] > 1
        assert "\n\n" in r_long.json()["text"]

        # 4. done_reason == "length" 显式 503 translate_failed
        state["truncate"] = True
        r_trunc = client.post("/api/v1/translate", json={"text": "hello", "lang_pair": "zh-en"}, headers=local_headers())
        assert r_trunc.status_code == 503
        assert r_trunc.json()["error"] == "translate_failed"
        assert "截断" in r_trunc.json()["message"]

    # 5. 非法参数在能力未注入（runtime.translate is None）时也必须先返 400（不得误返 503）
    rt_none = build_sandbox_runtime(root=tmp_path / "no-tr", token=TEST_TOKEN, translate=None)
    c_none = local_client(create_app(rt_none))
    for bad_body in (
        {"text": "   ", "lang_pair": "zh-en"},
        {"text": "hello", "lang_pair": "fr-de"},
        {"text": "hello", "target_lang": "zh"},
        {"text": "hello", "lang_pair": "zh-en", "source_lang": "zh", "target_lang": "zh"},
    ):
        resp = c_none.post("/api/v1/translate", json=bad_body, headers=local_headers())
        assert resp.status_code == 400 and resp.json()["error"] == "invalid_request", (bad_body, resp.status_code, resp.text)


def test_ocr_real_chain_single_direction_symbol_skip_and_prevalidation_400(tmp_path: Path):
    """真链路：真 FastAPI + 真 translate_blocks 整图单方向、纯符号跳过不 503、OCR 前置校验 400。"""
    sent_batches: list[tuple[str, list[str]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        user_msg = body["messages"][1]["content"]
        items = json.loads(user_msg.split("<source_text>\n", 1)[1].rsplit("\n</source_text>", 1)[0])
        sent_batches.append((user_msg, items))
        return httpx.Response(
            200,
            json={
                "message": {"role": "assistant", "content": json.dumps([f"TR:{it}" for it in items], ensure_ascii=False)},
                "done_reason": "stop",
            },
        )

    def mixed_layout(_payload: bytes, **_kw):
        blocks = [
            OcrTextBlock(
                bbox=(10, 10, 260, 24),
                lines=[OcrTextLine(bbox=(10, 10, 260, 24), text="请把 FastAPI 和 WebSocket 的本地翻译配置修一下")],
                ocr_text="请把 FastAPI 和 WebSocket 的本地翻译配置修一下",
            ),
            OcrTextBlock(bbox=(10, 40, 80, 16), lines=[OcrTextLine(bbox=(10, 40, 80, 16), text="---")], ocr_text="---"),
            OcrTextBlock(bbox=(10, 60, 100, 16), lines=[OcrTextLine(bbox=(10, 60, 100, 16), text="2026-11-21")], ocr_text="2026-11-21"),
            OcrTextBlock(bbox=(10, 80, 90, 20), lines=[OcrTextLine(bbox=(10, 80, 90, 20), text="Settings")], ocr_text="Settings"),
        ]
        return 400, 140, "\n\n".join(b.ocr_text for b in blocks), blocks

    with OllamaClient(transport=httpx.MockTransport(handler)) as ol:
        rt = build_sandbox_runtime(
            root=tmp_path / "real-ocr-chain",
            token=TEST_TOKEN,
            ocr_layout=mixed_layout,
            translate_blocks=lambda blocks, *, source_lang, target_lang, lang_pair="zh-en": real_translate_blocks(
                blocks,
                source_lang=source_lang,
                target_lang=target_lang,
                lang_pair=lang_pair,
                model="qwen3.5:4b",
                client=ol,
            ),
        )
        client = local_client(create_app(rt))
        res = client.post(
            "/api/v1/ocr-inplace",
            data={"mode": "translate", "lang_pair": "zh-en"},
            files={"image": ("s.png", b"fake-img", "image/png")},
            headers=local_headers(),
        )
        assert res.status_code == 200, res.text
        body = res.json()
        # 整图合并 ocr_text 中文占优 → 统一判为 zh→en，仅有效项送模型，纯符号/数字原位保留
        assert (body["source_lang"], body["target_lang"], body["direction_mode"]) == ("zh", "en", "auto")
        assert len(sent_batches) == 1
        user_msg, items = sent_batches[0]
        assert "源语言: 中文 (zh)" in user_msg and "目标语言: 英文 (en)" in user_msg
        assert items == ["请把 FastAPI 和 WebSocket 的本地翻译配置修一下", "Settings"]
        assert [b["translated_text"] for b in body["blocks"]] == [
            "TR:请把 FastAPI 和 WebSocket 的本地翻译配置修一下",
            "---",
            "2026-11-21",
            "TR:Settings",
        ]

    # OCR 能力未注入时，非法 mode / lang_pair / 旧单传 target_lang 仍必须先返 400（不误返 503）
    rt_empty = build_sandbox_runtime(root=tmp_path / "no-ocr", token=TEST_TOKEN)
    c_empty = local_client(create_app(rt_empty))
    for path in ("/api/v1/ocr-translate", "/api/v1/ocr-inplace"):
        for bad_form in (
            {"mode": "bogus"},
            {"mode": "translate", "lang_pair": "fr-de"},
            {"target_lang": "en"},
            {"target_lang": "raw"},
            {"mode": "translate", "lang_pair": "zh-en", "source_lang": "zh", "target_lang": "zh"},
        ):
            r_bad = c_empty.post(
                path,
                data=bad_form,
                files={"image": ("s.png", b"x", "image/png")},
                headers=local_headers(),
            )
            assert r_bad.status_code == 400 and r_bad.json()["error"] == "invalid_request", (path, bad_form, r_bad.status_code, r_bad.text)
