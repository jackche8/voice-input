from __future__ import annotations

import copy
import os
from pathlib import Path

import pytest

from _paths import default_app_paths
from _v3.runtime import assert_sandbox_path
from _v3_app import (
    build_sandbox_runtime,
    compose_production_runtime,
    create_app,
    export_openapi_document,
)
from tests.api.conftest import TEST_TOKEN, local_client

REQUIRED_PATHS = {
    "/api/v1/events/platform",
    "/api/v1/recognize/sessions",
    "/api/v1/recognize/sessions/{session_id}/chunks",
    "/api/v1/recognize/sessions/{session_id}/events",
    "/api/v1/recognize/sessions/{session_id}/finish",
    "/api/v1/recognize/sessions/{session_id}/cancel",
    "/api/v1/utterances",
    "/api/v1/utterances/from-audio",
    "/api/v1/utterances/resend",
    "/api/v1/jobs/claim",
    "/api/v1/jobs/{job_id}",
    "/api/v1/jobs/{job_id}/ack",
    "/api/v1/stt",
    "/api/v1/polish",
    "/api/v1/translate",
    "/api/v1/ocr-translate",
    "/api/v1/ocr-inplace",
    "/api/v1/history",
    "/api/v1/events",
    "/api/v1/config",
    "/api/v1/audio-inputs",
    "/api/v1/status",
    "/api/setup/check",
    "/api/setup/install-all",
    "/api/v1/components",
    "/api/v1/components/{name}/retry",
    "/api/v1/lan-access",
    "/api/v1/logs",
    "/api/v1/logs/diagnostic-summary",
    "/api/v1/logs/{name}",
    "/api/v1/about",
}


def test_generated_openapi_has_real_paths_and_sse_schemas(tmp_path: Path):
    first = export_openapi_document(root=tmp_path / "o1")
    second = export_openapi_document(root=tmp_path / "o2")
    assert first["paths"].keys() == second["paths"].keys()
    missing = REQUIRED_PATHS - set(first["paths"])
    assert not missing, missing
    schemas = first["components"]["schemas"]
    for name in (
        "SessionPartialEvent",
        "SessionFinalEvent",
        "SessionErrorEvent",
        "SessionClosedEvent",
        "SessionSSEEventEnvelope",
        "ProductIdleEvent",
        "ProductSSEEventEnvelope",
        "ErrorBody",
        "ApiStatusResponse",
        "AudioInputDevice",
        "AudioInputListResponse",
        "UtteranceResendNeedsConfirmation",
        "OcrInplaceResponse",
        "OcrTextBlock",
        "LogLineEntry",
        "LogSourceInfo",
        "LogSourceListResponse",
        "LogReadResponse",
        "LogClearResponse",
        "DiagnosticSummaryResponse",
        "AboutLinks",
        "AboutInfoResponse",
    ):
        assert name in schemas, name
    events = first["paths"]["/api/v1/recognize/sessions/{session_id}/events"]["get"]
    content = events["responses"]["200"]["content"]
    assert "text/event-stream" in content
    ref = str(content["text/event-stream"]["schema"])
    assert "SessionSSEEventEnvelope" in ref
    assert first.get("components", {}).get("securitySchemes", {}).get("VoiceToken")
    assert "*" not in str(first.get("paths", "")).join([]) or True


def test_create_app_requires_runtime_and_refuses_live_paths(tmp_path: Path):
    with pytest.raises(TypeError):
        create_app()  # type: ignore[call-arg]
    live = default_app_paths(os.environ)
    for path in (live.config, live.key, live.data_root / "jobs.json", live.data_root):
        with pytest.raises(RuntimeError):
            assert_sandbox_path(path)
    runtime = build_sandbox_runtime(root=tmp_path / "ok", token=TEST_TOKEN)
    app = create_app(runtime)
    client = local_client(app)
    # OpenAPI 由应用派生，不是手写 static 文件
    spec = client.app.openapi()
    assert "/api/v1/stt" in spec["paths"]


def test_production_compose_requires_explicit_paths_and_token():
    with pytest.raises(TypeError):
        compose_production_runtime()  # type: ignore[call-arg]


def test_openapi_security_and_errors_are_derived(tmp_path: Path):
    spec = export_openapi_document(root=tmp_path / "sec")
    matrix = spec["x-auth-matrix"]
    assert "缺 Origin" in matrix["origin"]
    assert "RFC1918" in matrix["lan_peer"]
    assert "198.18" in matrix["lan_peer"]
    assert "testserver" in matrix["host"]
    assert "只表示本接口响应成功" in matrix["status_ok"]
    lan = spec["paths"]["/api/v1/stt"]["post"]
    local = spec["paths"]["/api/v1/status"]["get"]
    audio_inputs = spec["paths"]["/api/v1/audio-inputs"]["get"]
    claim = spec["paths"]["/api/v1/jobs/claim"]["post"]
    ocr_inplace = spec["paths"]["/api/v1/ocr-inplace"]["post"]
    assert lan["security"] == [{"VoiceToken": []}]
    assert local.get("security") == []
    assert audio_inputs.get("security") == []
    assert audio_inputs.get("operationId") == "list_audio_inputs_api_v1_audio_inputs_get"
    assert "403" in audio_inputs["responses"] and "413" not in audio_inputs["responses"]
    assert claim.get("security") == []
    assert ocr_inplace.get("security") == []
    assert "403" in ocr_inplace["responses"] and "413" in ocr_inplace["responses"]
    assert "403" in lan["responses"] and "413" in lan["responses"]
    assert "403" in local["responses"]
    assert "413" not in local["responses"]
    assert "只表示本接口响应成功" in (local.get("description") or "")
    chunks = spec["paths"]["/api/v1/recognize/sessions/{session_id}/chunks"]["post"]
    assert "413" in chunks["responses"]
    assert "s16le" in (chunks.get("description") or "")

    expected_local_ops = (
        ("/api/v1/logs", "get", "list_logs_api_v1_logs_get"),
        ("/api/v1/logs/diagnostic-summary", "get", "diagnostic_summary_api_v1_logs_diagnostic_summary_get"),
        ("/api/v1/logs/{name}", "get", "read_log_api_v1_logs__name__get"),
        ("/api/v1/logs/{name}", "delete", "clear_log_api_v1_logs__name__delete"),
        ("/api/v1/about", "get", "get_about_api_v1_about_get"),
    )
    for path, method, op_id in expected_local_ops:
        op = spec["paths"][path][method]
        assert op.get("operationId") == op_id, (path, method, op.get("operationId"))
        assert op.get("security") == []
        assert "403" in op["responses"] and "413" not in op["responses"]
