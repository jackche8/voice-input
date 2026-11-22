from __future__ import annotations

import json
import threading
from pathlib import Path

import httpx
import pytest

from _desktop.api import ApiError, DesktopApi
from _jobs_client import JobsClient, resolve_verify_ca


class _FakeTransport(httpx.BaseTransport):
    def __init__(self) -> None:
        self.acks: list[dict] = []
        self.requests: list[httpx.Request] = []

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        url = str(request.url)
        if request.method == "POST" and url.endswith("/api/v1/jobs/claim"):
            return httpx.Response(200, json={"job_id": "j1", "claim_id": "c1", "text": "hi"})
        if request.method == "GET" and "/api/v1/jobs/j1" in url:
            return httpx.Response(200, json={"found": True, "state": "claimed", "job_id": "j1"})
        if request.method == "POST" and url.endswith("/ack"):
            payload = json.loads(request.content.decode())
            self.acks.append(payload)
            return httpx.Response(200, json={"ok": True, "state": payload["result"]})
        if request.method == "POST" and url.endswith("/api/v1/recognize/sessions"):
            payload = json.loads(request.content.decode())
            return httpx.Response(200, json={"session_id": f"sess:{payload.get('context', '')}"})
        if request.method == "POST" and url.endswith("/api/v1/ocr-inplace"):
            raw = request.read().decode("utf-8", errors="ignore")
            assert 'name="mode"' in raw and 'name="image"' in raw
            if 'name="mode"\r\n\r\nraw' in raw:
                return httpx.Response(
                    200,
                    json={
                        "ok": True,
                        "text": "Hello World",
                        "ocr_text": "Hello World",
                        "mode": "raw",
                        "lang_pair": None,
                        "source_lang": None,
                        "target_lang": None,
                        "direction_mode": None,
                        "image_width": 200,
                        "image_height": 80,
                        "blocks": [
                            {
                                "bbox": [10, 10, 120, 30],
                                "lines": [{"bbox": [10, 10, 120, 30], "text": "Hello World", "soft_wrap": False}],
                                "ocr_text": "Hello World",
                                "translated_text": "Hello World",
                                "bg_color": "#ffffff",
                                "fg_color": "#0f172a",
                            }
                        ],
                    },
                )
            assert 'name="lang_pair"' in raw
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "text": "你好世界",
                    "ocr_text": "Hello World",
                    "mode": "translate",
                    "lang_pair": "zh-en",
                    "source_lang": "en",
                    "target_lang": "zh",
                    "direction_mode": "auto",
                    "image_width": 200,
                    "image_height": 80,
                    "blocks": [
                        {
                            "bbox": [10, 10, 120, 30],
                            "lines": [{"bbox": [10, 10, 120, 30], "text": "Hello World", "soft_wrap": False}],
                            "ocr_text": "Hello World",
                            "translated_text": "你好世界",
                            "bg_color": "#ffffff",
                            "fg_color": "#0f172a",
                        }
                    ],
                },
            )
        if request.method == "GET" and url.endswith("/api/v1/events"):
            sse = (
                ": keepalive\n"
                'event: idle\ndata: {"event":"idle"}\n\n'
                'event: partial\ndata: {"event":"partial","stable_text":"你好","tail_text":"世界"}\n\n'
                'event: delivery_unknown\ndata: {"event":"delivery_unknown","job_id":"j9","text":"测试"}\n\n'
            ).encode("utf-8")
            return httpx.Response(200, content=sse, headers={"content-type": "text/event-stream"})
        return httpx.Response(404, json={"error": "not_found", "message": "not found"})


def test_jobs_client_ack_idempotent_and_loopback_only():
    transport = _FakeTransport()
    with JobsClient(base_url="http://127.0.0.1:17945", transport=transport) as client:
        assert client._client._trust_env is False
        claimed = client.claim()
        assert claimed["claim_id"] == "c1"
        body = client.ack("j1", "c1", "complete")
        assert body["state"] == "complete"
        again = client.ack("j1", "c1", "complete")
        assert again["state"] == "complete"
        with pytest.raises(ValueError):
            client.ack("j1", "c1", "none")
    with pytest.raises(ValueError):
        JobsClient(base_url="https://example.com:17945")


def test_resolve_verify_ca_prefers_sibling_ca_and_falls_back(tmp_path: Path):
    cert = tmp_path / "cert.pem"
    ca = tmp_path / "ca.pem"
    with pytest.raises(ValueError):
        resolve_verify_ca(None)
    with pytest.raises(ValueError):
        resolve_verify_ca(cert)

    cert.write_text("self-signed", encoding="utf-8")
    assert resolve_verify_ca(cert) == cert

    ca.write_text("root-ca", encoding="utf-8")
    assert resolve_verify_ca(cert) == ca


def test_desktop_api_context_ocr_inplace_sse_and_no_proxy(monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9")
    monkeypatch.setenv("ALL_PROXY", "http://127.0.0.1:9")
    transport = _FakeTransport()
    with DesktopApi(base_url="https://127.0.0.1:17945", transport=transport) as api:
        assert api._client._trust_env is False

        sid1 = api.create_session(language="zh")
        assert sid1 == "sess:"
        body1 = json.loads(transport.requests[-1].content.decode())
        assert body1 == {"language": "zh"}

        sid2 = api.create_session(language="zh", context="PySide6, Qwen3-ASR")
        assert sid2 == "sess:PySide6, Qwen3-ASR"
        body2 = json.loads(transport.requests[-1].content.decode())
        assert body2 == {"language": "zh", "context": "PySide6, Qwen3-ASR"}

        res = api.ocr_inplace(b"\x89PNG\r\n\x1a\nfake", mode="translate", lang_pair="zh-en", lang="chi_sim+eng")
        assert res["text"] == "你好世界" and res["ocr_text"] == "Hello World"
        assert res["mode"] == "translate" and res["lang_pair"] == "zh-en" and res["direction_mode"] == "auto"
        assert len(res["blocks"]) == 1 and res["blocks"][0]["bbox"] == [10, 10, 120, 30]

        raw_res = api.ocr_inplace(b"\x89PNG\r\n\x1a\nfake", mode="raw")
        assert raw_res["mode"] == "raw" and raw_res["text"] == "Hello World" and raw_res["lang_pair"] is None

        stop = threading.Event()
        events = list(api.iter_product_events(stop_event=stop))
        assert [e["event"] for e in events] == ["idle", "partial", "delivery_unknown"]
        assert events[1]["stable_text"] == "你好" and events[1]["tail_text"] == "世界"
        assert events[2]["job_id"] == "j9"

        with pytest.raises(ApiError) as ei:
            api.get_config()
        assert ei.value.status == 404 and ei.value.code == "not_found"
