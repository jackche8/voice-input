from __future__ import annotations

import math
import struct
from pathlib import Path

from _v3_app import build_sandbox_runtime, create_app
from tests.api.conftest import TEST_TOKEN, lan_client, lan_headers, local_client, local_headers
from tests.api.providers import fixture_ocr_layout, fixture_transcribe, make_text_wav, make_wav_bytes


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
    trans = client.post("/api/v1/translate", json={"text": "hello", "target_lang": "zh"}, headers=local_headers())
    assert trans.status_code == 200
    assert trans.json()["text"] == "[zh]hello"
    assert trans.json()["text"] != "hello" or trans.json()["target_lang"] == "zh"


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
        data={"target_lang": "raw"},
        files={"image": ("t.txt", "画面文字".encode("utf-8"), "text/plain")},
        headers=local_headers(),
    )
    assert raw.status_code == 200
    assert raw.json()["ocr_text"] == "画面文字"
    assert raw.json()["text"] == "画面文字"
    translated = client.post(
        "/api/v1/ocr-translate",
        data={"target_lang": "en"},
        files={"image": ("t.txt", "画面文字".encode("utf-8"), "text/plain")},
        headers=local_headers(),
    )
    assert translated.status_code == 200
    assert translated.json()["ocr_text"] == "画面文字"
    assert translated.json()["text"] == "[en]画面文字"
    assert translated.json()["text"] != translated.json()["ocr_text"]


def test_ocr_inplace_translated_and_raw_modes(app, tmp_path: Path):
    client = local_client(app)
    translated = client.post(
        "/api/v1/ocr-inplace",
        data={"target_lang": "en"},
        files={"image": ("t.png", "第一行\n第二行".encode("utf-8"), "image/png")},
        headers=local_headers(),
    )
    assert translated.status_code == 200, translated.text
    body = translated.json()
    assert body["ok"] is True
    assert body["target_lang"] == "en"
    assert body["image_width"] == 320 and body["image_height"] == 180
    assert body["ocr_text"] == "第一行\n第二行"
    assert body["text"] == "[en]第一行\n[en]第二行"
    assert len(body["blocks"]) == 2
    assert body["blocks"][0]["translated_text"] == "[en]第一行"
    assert body["blocks"][1]["translated_text"] == "[en]第二行"
    assert body["blocks"][0]["bg_color"] == "#ffffff"
    assert body["blocks"][1]["bg_color"] == "#111827"

    # raw 模式直通原文，不触发翻译能力
    translate_calls = []

    def fail_translate(blocks, *, target_lang):
        translate_calls.append(target_lang)
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
        data={"target_lang": "raw"},
        files={"image": ("t.png", "原文A\n原文B".encode("utf-8"), "image/png")},
        headers=local_headers(),
    )
    assert raw.status_code == 200, raw.text
    raw_body = raw.json()
    assert raw_body["target_lang"] == "raw"
    assert raw_body["text"] == "原文A\n原文B"
    assert [b["translated_text"] for b in raw_body["blocks"]] == ["原文A", "原文B"]
    assert translate_calls == []


def test_ocr_inplace_empty_and_missing_capability(app, tmp_path: Path):
    client = local_client(app)
    empty = client.post(
        "/api/v1/ocr-inplace",
        data={"target_lang": "en"},
        files={"image": ("t.png", b"empty-image", "image/png")},
        headers=local_headers(),
    )
    assert empty.status_code == 503
    assert empty.json()["error"] == "ocr_failed"

    rt = build_sandbox_runtime(root=tmp_path / "no-ocr-layout", token=TEST_TOKEN)
    missing = local_client(create_app(rt)).post(
        "/api/v1/ocr-inplace",
        data={"target_lang": "en"},
        files={"image": ("t.png", b"hi", "image/png")},
        headers=local_headers(),
    )
    assert missing.status_code == 503
    assert missing.json()["error"] == "ocr_failed"


def test_ocr_empty_is_error(app):
    client = local_client(app)
    res = client.post(
        "/api/v1/ocr-translate",
        data={"target_lang": "en"},
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
        data={"target_lang": "raw"},
        files={"image": ("t.txt", "画面文字".encode("utf-8"), "text/plain")},
        headers=lan_headers(),
    )
    assert ocr.status_code == 403
    inplace = client.post(
        "/api/v1/ocr-inplace",
        data={"target_lang": "raw"},
        files={"image": ("t.png", "画面文字".encode("utf-8"), "image/png")},
        headers=lan_headers(),
    )
    assert inplace.status_code == 403
