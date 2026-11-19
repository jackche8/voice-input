from __future__ import annotations

from _v3.production import STATIC_V3
from _v3_app import create_app
from tests.api.conftest import (
    LAN_HOST,
    TEST_TOKEN,
    lan_client,
    lan_headers,
    local_client,
    local_headers,
)
from tests.api.providers import make_text_wav


def test_local_status_ok_and_does_not_echo_token(app):
    client = local_client(app)
    res = client.get("/api/v1/status")
    assert res.status_code == 200
    body = res.json()
    assert body["ok"] is True
    assert body["unmet_gates"]
    dumped = str(body)
    assert TEST_TOKEN not in dumped
    assert "token" not in dumped.lower() or "unmet" in dumped.lower()


def test_remote_status_and_claim_forbidden_even_with_token(app):
    client = lan_client(app)
    headers = lan_headers()
    status = client.get("/api/v1/status", headers=headers)
    assert status.status_code == 403
    assert status.json()["error"] == "forbidden"
    claim = client.post("/api/v1/jobs/claim", json={}, headers=headers)
    assert claim.status_code == 403
    stream = client.get("/api/v1/recognize/sessions/does-not-matter/events", headers=headers)
    assert stream.status_code == 403
    events = client.get("/api/v1/events", headers=headers)
    assert events.status_code == 403
    ack = client.post("/api/v1/jobs/job-x/ack", json={"claim_id": "c", "result": "complete"}, headers=headers)
    assert ack.status_code == 403
    cfg = client.post("/api/v1/config", json={"polish_enabled": True}, headers=headers)
    assert cfg.status_code == 403
    setup = client.post("/api/setup/pull_model", headers=headers)
    assert setup.status_code == 403


def test_x_forwarded_for_cannot_spoof_localhost(app):
    client = lan_client(app)
    spoof = lan_headers()
    spoof["X-Forwarded-For"] = "127.0.0.1"
    spoof["Forwarded"] = "for=127.0.0.1"
    res = client.get("/api/v1/status", headers=spoof)
    assert res.status_code == 403


def test_lan_stt_requires_valid_token(app):
    client = lan_client(app)
    missing = client.post(
        "/api/v1/stt",
        data={"language": "zh"},
        files={"audio": ("a.txt", b"hello", "application/octet-stream")},
        headers={"Origin": f"https://{LAN_HOST}:17945"},
    )
    assert missing.status_code == 403
    wrong = client.post(
        "/api/v1/stt",
        data={"language": "zh"},
        files={"audio": ("a.txt", b"hello", "application/octet-stream")},
        headers=lan_headers(token="wrong-token-not-the-test-one"),
    )
    assert wrong.status_code == 403
    ok = client.post(
        "/api/v1/stt",
        data={"language": "zh"},
        files={"audio": ("a.wav", make_text_wav("hello"), "audio/wav")},
        headers=lan_headers(),
    )
    assert ok.status_code == 200
    assert ok.json()["text"] == "hello"


def test_unknown_origin_rejected_on_write(app):
    client = local_client(app)
    res = client.post(
        "/api/v1/config",
        json={"polish_enabled": False},
        headers={"Origin": "https://evil.example:17945"},
    )
    assert res.status_code == 403
    assert "CSRF" in res.json()["message"]
    lan = lan_client(app)
    bad = lan.post(
        "/api/v1/stt",
        data={"language": "zh"},
        files={"audio": ("a.txt", b"hello", "application/octet-stream")},
        headers={
            "Origin": "https://evil.example:17945",
            "Host": f"{LAN_HOST}:17945",
            "X-Voice-Token": TEST_TOKEN,
        },
    )
    assert bad.status_code == 403


def test_local_same_origin_write_allowed(app):
    client = local_client(app)
    res = client.post(
        "/api/v1/config",
        json={"polish_enabled": False},
        headers=local_headers(),
    )
    assert res.status_code == 200
    assert res.json()["polish_enabled"] is False


def test_no_cors_star_header(app):
    client = local_client(app)
    res = client.get("/api/v1/status", headers={"Origin": "https://127.0.0.1:17945"})
    acao = res.headers.get("access-control-allow-origin")
    assert acao not in {"*", "https://*"}
    assert acao is None


def test_sec_fetch_site_cross_site_blocks_api_and_post_allows_top_level_pages(runtime):
    page_app = create_app(runtime, static_v3=STATIC_V3)
    local = local_client(page_app)
    cross = {"Sec-Fetch-Site": "cross-site"}

    status_res = local.get("/api/v1/status", headers=cross)
    assert status_res.status_code == 403
    assert "Sec-Fetch-Site" in status_res.json()["message"]

    cfg_res = local.post("/api/v1/config", json={"polish_enabled": False}, headers=cross)
    assert cfg_res.status_code == 403
    assert "Sec-Fetch-Site" in cfg_res.json()["message"]

    openapi_res = local.get("/openapi.json", headers=cross)
    assert openapi_res.status_code == 403
    assert "Sec-Fetch-Site" in openapi_res.json()["message"]

    post_root = local.post("/", headers=cross)
    assert post_root.status_code == 403
    assert "Sec-Fetch-Site" in post_root.json()["message"]

    get_root = local.get("/", headers=cross)
    assert get_root.status_code == 200

    get_voice = local.get("/voice", headers=cross)
    assert get_voice.status_code == 200

    lan = lan_client(page_app)
    lan_voice = lan.get("/voice", headers={"Host": f"{LAN_HOST}:17945", "Sec-Fetch-Site": "cross-site"})
    assert lan_voice.status_code == 200
    lan_api = lan.get("/api/v1/history", headers={**lan_headers(), "Sec-Fetch-Site": "cross-site"})
    assert lan_api.status_code == 403
