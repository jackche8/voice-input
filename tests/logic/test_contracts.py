from __future__ import annotations

import pytest
from pydantic import ValidationError

from _contracts import (
    ASR_STREAMING_BLOCKERS,
    DEFAULT_LOG_MAX_LINES,
    LOG_MAX_LINES_CHOICES,
    LOG_READ_MAX_BYTES,
    AboutAsrVariant,
    AboutInfoResponse,
    AboutLinks,
    AckResult,
    AppConfig,
    AppConfigUpdate,
    AppConfigView,
    AudioInputDevice,
    AudioInputListResponse,
    ComponentName,
    ComponentReport,
    ComponentState,
    ComponentStatus,
    DiagnosticSummaryResponse,
    ErrorBody,
    ErrorCode,
    HistoryPolish,
    HistoryRecord,
    JobAckRequest,
    JobQueryFound,
    JobQueryResponseEnvelope,
    JobQueryUnknown,
    JobRecord,
    JobState,
    LogClearResponse,
    LogGroup,
    LogLevel,
    LogLineEntry,
    LogReadResponse,
    LogSourceInfo,
    LogSourceListResponse,
    OCR_PREPROCESS_MAX_ENABLED,
    OCR_PREPROCESS_STARTER_TEMPLATES,
    OcrAttemptSummary,
    OcrBinarizeMode,
    OcrInplaceResponse,
    OcrPreprocessParams,
    OcrPreprocessPreset,
    OcrPreprocessPreviewResponse,
    OcrPreprocessStarterTemplate,
    OcrRunMeta,
    OcrRunMode,
    OcrTranslateMode,
    OcrTranslateResponse,
    PolishResponse,
    ProductBusyEvent,
    ProductComponentCommandEvent,
    ProductConfigChangedEvent,
    ProductDeliveryUnknownEvent,
    ProductErrorEvent,
    ProductEventType,
    ProductHistoryWriteFailedEvent,
    ProductIdleEvent,
    ProductPartialEvent,
    ProductPolishingEvent,
    ProductRecordingEvent,
    ProductSSEEventEnvelope,
    ProductTypedEvent,
    RunMode,
    SessionClosedEvent,
    SessionClosedReason,
    SessionPartialEvent,
    SessionSSEEventEnvelope,
    SttResponse,
    TranslateDirectionMode,
    TranslateRequest,
    TranslateResponse,
    UtteranceResendNeedsConfirmation,
    UtteranceResendRequest,
    validate_direction_override,
    validate_lang_pair,
    UtteranceSource,
    build_openapi_skeleton,
    contract_models,
)


def test_error_body_and_logic_shape():
    body = ErrorBody(error=ErrorCode.timeout, message="x")
    assert body.ok is False
    dumped = body.model_dump()
    assert dumped["error"] == "timeout"


def test_session_sse_discriminator_roundtrip():
    ev = SessionPartialEvent(session_id="s1", stable_text="", tail_text="甚至")
    wrapped = SessionSSEEventEnvelope.model_validate(ev.model_dump())
    assert wrapped.root.event == "partial"
    assert wrapped.root.stable_text == ""
    assert wrapped.root.tail_text == "甚至"
    closed = SessionSSEEventEnvelope.model_validate(
        SessionClosedEvent(session_id="s1", reason=SessionClosedReason.cancelled).model_dump()
    )
    assert closed.root.reason == SessionClosedReason.cancelled


def test_session_partial_uses_stable_text_tail_text_only():
    ev = SessionPartialEvent(session_id="s1", stable_text="甚至出现", tail_text="交易")
    dumped = ev.model_dump()
    assert dumped["stable_text"] == "甚至出现"
    assert dumped["tail_text"] == "交易"
    assert "stable" not in dumped
    assert "tail" not in dumped
    assert "text" not in dumped
    with pytest.raises(ValidationError):
        SessionPartialEvent.model_validate(
            {"event": "partial", "session_id": "s1", "text": "甚至", "stable": "", "tail": "甚至"}
        )
    closed = SessionClosedEvent(session_id="s1", reason=SessionClosedReason.disconnected)
    assert closed.reason == SessionClosedReason.disconnected
    assert SessionClosedReason.disconnected.value == "disconnected"


def test_resend_needs_confirmation_ok_is_false():
    body = UtteranceResendNeedsConfirmation(original_job_id="old", new_job_id="new")
    assert body.ok is False
    assert body.requires_confirmation is True
    dumped = body.model_dump()
    assert dumped["ok"] is False
    with pytest.raises(ValidationError):
        UtteranceResendNeedsConfirmation.model_validate(
            {
                "ok": True,
                "requires_confirmation": True,
                "original_job_id": "old",
                "new_job_id": "new",
            }
        )


def test_source_includes_resend_and_ack_is_partial_or_unknown():
    rec = JobRecord(
        job_id="j-resend",
        state=JobState.queued,
        source=UtteranceSource.resend,
        focus_id="window-a",
        created_at="2026-01-01T00:00:00",
        updated_at="2026-01-01T00:00:00",
    )
    assert rec.source == UtteranceSource.resend
    ack = JobAckRequest(claim_id="c1", result=AckResult.partial_or_unknown)
    assert ack.result == AckResult.partial_or_unknown
    assert ack.result.value == "partial_or_unknown"
    with pytest.raises(ValidationError):
        JobAckRequest.model_validate({"claim_id": "c1", "result": "partial"})
    with pytest.raises(AttributeError):
        _ = AckResult.partial  # noqa: B018 — 不得保留旧别名


def test_job_query_unknown_is_not_a_persistable_state():
    found = JobQueryFound(
        job=JobRecord(
            job_id="j1",
            state=JobState.delivery_unknown,
            focus_id="window-a",
            created_at="2026-01-01T00:00:00",
            updated_at="2026-01-01T00:00:00",
        )
    )
    assert found.found is True
    assert found.job.state == JobState.delivery_unknown
    unknown = JobQueryUnknown(job_id="missing")
    assert unknown.found is False
    assert unknown.state == "unknown"
    wrapped = JobQueryResponseEnvelope.model_validate(unknown.model_dump())
    assert wrapped.root.state == "unknown"
    with pytest.raises(ValidationError):
        JobRecord(
            job_id="j1",
            state="unknown",
            created_at="t",
            updated_at="t",
        )


def test_history_explicit_fields_and_legacy_mode_optional():
    legacy = HistoryRecord.model_validate(
        {
            "timestamp": "2026-01-01T10:00:00",
            "raw_text": "第一句",
            "final_text": "第一句",
            "mode": "raw",
            "language": "zh",
        }
    )
    assert legacy.mode == "raw"
    assert legacy.source is None
    assert legacy.polish is None
    assert legacy.job_id is None
    modern = HistoryRecord(
        timestamp="2026-01-01T12:00:00",
        raw_text="a",
        final_text="A",
        language="zh",
        source=UtteranceSource.resend,
        polish=HistoryPolish.failed,
        job_id="j9",
    )
    dumped = modern.model_dump()
    assert dumped["source"] == "resend"
    assert dumped["polish"] == "failed"
    assert dumped["job_id"] == "j9"
    assert "mode" in dumped
    assert dumped["mode"] is None
    without_mode = HistoryRecord.model_validate(
        {
            "timestamp": "t",
            "raw_text": "r",
            "final_text": "f",
            "language": "zh",
            "source": "desktop",
            "polish": "skipped",
            "job_id": "j2",
        }
    )
    assert without_mode.mode is None
    assert without_mode.polish == HistoryPolish.skipped
    with pytest.raises(ValidationError):
        HistoryRecord.model_validate(
            {
                "timestamp": "t",
                "raw_text": "r",
                "final_text": "f",
                "language": "zh",
                "polish": "raw",
            }
        )


def test_job_state_values_and_record_focus_is_per_job():
    assert JobState.failed.value == "failed"
    rec = JobRecord(
        job_id="j1",
        state=JobState.delivery_unknown,
        text="hi",
        focus_id="window-a",
        created_at="2026-01-01T00:00:00",
        updated_at="2026-01-01T00:00:00",
    )
    assert rec.focus_id == "window-a"
    assert "focus" not in rec.model_dump()
    with pytest.raises(ValidationError):
        JobRecord(
            job_id="j1",
            state="running",
            created_at="t",
            updated_at="t",
        )


def test_success_bodies_are_explicit_ok():
    assert SttResponse(text="a").ok is True
    assert PolishResponse(text="a").ok is True
    assert (
        TranslateResponse(
            text="b",
            lang_pair="zh-en",
            source_lang="zh",
            target_lang="en",
            direction_mode=TranslateDirectionMode.auto,
        ).ok
        is True
    )


def test_translate_request_and_lang_pair_single_source_validation():
    assert validate_lang_pair(" ZH-EN ") == "zh-en"
    assert validate_direction_override("zh-en", None, None) == ("zh-en", None)
    assert validate_direction_override(" ZH-EN ", " EN ", " ZH ") == ("zh-en", ("en", "zh"))

    req = TranslateRequest(text="你好", lang_pair=" ZH-EN ", source_lang=" ZH ", target_lang=" EN ")
    assert (req.lang_pair, req.source_lang, req.target_lang) == ("zh-en", "zh", "en")

    for bad_kwargs in (
        {"text": "   "},
        {"text": "hi", "lang_pair": "fr-de"},
        {"text": "hi", "lang_pair": ""},
        {"text": "hi", "source_lang": "zh"},
        {"text": "hi", "target_lang": "zh"},
        {"text": "hi", "source_lang": "zh", "target_lang": "   "},
        {"text": "hi", "source_lang": "zh", "target_lang": "zh"},
        {"text": "hi", "source_lang": "zh", "target_lang": "ja"},
    ):
        with pytest.raises(ValidationError):
            TranslateRequest(**bad_kwargs)

    with pytest.raises(ValidationError):
        AppConfig(translate_lang_pair="fr-de")
    with pytest.raises(ValidationError):
        AppConfigUpdate(translate_target_lang="en")  # type: ignore[call-arg]


def test_product_events_are_typed_not_style_text():
    cases = [
        ProductIdleEvent(),
        ProductRecordingEvent(session_id="s"),
        ProductPartialEvent(session_id="s", stable_text="稳", tail_text="尾"),
        ProductPolishingEvent(job_id="j"),
        ProductTypedEvent(job_id="j", text="已输入"),
        ProductErrorEvent(error=ErrorCode.internal, message="boom"),
        ProductBusyEvent(),
        ProductHistoryWriteFailedEvent(job_id="j", message="disk", error=ErrorCode.history_io),
        ProductDeliveryUnknownEvent(job_id="j", text="maybe"),
        ProductComponentCommandEvent(action="retry", component=ComponentName.asr),
        ProductComponentCommandEvent(action="stop", component=ComponentName.asr),
        ProductConfigChangedEvent(),
    ]
    seen: set[str] = set()
    for ev in cases:
        wrapped = ProductSSEEventEnvelope.model_validate(ev.model_dump())
        assert wrapped.root.event == ev.event
        seen.add(ev.event.value if hasattr(ev.event, "value") else ev.event)
        assert not hasattr(ev, "style") or "style" not in ev.model_dump()
    for name in ProductEventType:
        assert name.value in seen, name
    partial = ProductPartialEvent(stable_text="a", tail_text="b")
    assert "stable_text" in partial.model_dump()
    assert "tail_text" in partial.model_dump()
    with pytest.raises(ValidationError):
        ProductSSEEventEnvelope.model_validate({"event": "status", "text": "idle", "style": "normal"})
    with pytest.raises(ValidationError):
        ProductPartialEvent.model_validate({"event": "partial", "text": "甚至", "stable": "", "tail": "甚至"})


def test_component_report_carries_each_component_exactly_once():
    def status(name: ComponentName) -> ComponentStatus:
        return ComponentStatus(name=name, state=ComponentState.running, since="2026-11-20T10:00:00+00:00")

    report = ComponentReport(components=[status(n) for n in ComponentName])
    assert [c.name for c in report.components] == [ComponentName.ollama, ComponentName.asr, ComponentName.server]
    for broken in (
        [status(ComponentName.ollama), status(ComponentName.asr)],  # 少一项
        [status(n) for n in ComponentName] + [status(ComponentName.asr)],  # 重复
    ):
        with pytest.raises(ValidationError):
            ComponentReport(components=broken)
    stop_ev = ProductSSEEventEnvelope.model_validate(
        {"event": "component_command", "action": "stop", "component": "asr"}
    )
    assert isinstance(stop_ev.root, ProductComponentCommandEvent)
    assert stop_ev.root.action == "stop" and stop_ev.root.component == ComponentName.asr
    for bad_action in ("installed", "restart", ""):
        with pytest.raises(ValidationError):  # 命令只允许 retry / stop；「已安装」由看护层按文件发现，不经事件
            ProductSSEEventEnvelope.model_validate(
                {"event": "component_command", "action": bad_action, "component": "asr"}
            )


def test_openapi_skeleton_includes_both_sse_families_and_no_fake_paths():
    spec = build_openapi_skeleton()
    assert spec["paths"] == {}
    names = set(spec["components"]["schemas"])
    for required in (
        "ErrorBody",
        "SessionPartialEvent",
        "SessionFinalEvent",
        "SessionErrorEvent",
        "SessionClosedEvent",
        "SessionSSEEventEnvelope",
        "ProductIdleEvent",
        "ProductRecordingEvent",
        "ProductPartialEvent",
        "ProductPolishingEvent",
        "ProductTypedEvent",
        "ProductErrorEvent",
        "ProductBusyEvent",
        "ProductHistoryWriteFailedEvent",
        "ProductDeliveryUnknownEvent",
        "ProductSSEEventEnvelope",
        "UtteranceResendNeedsConfirmation",
        "JobQueryFound",
        "JobQueryUnknown",
        "JobRecord",
        "HistoryRecord",
        "AppConfig",
        "AppConfigView",
        "AppConfigUpdate",
        "AudioInputDevice",
        "AudioInputListResponse",
    ):
        assert required in names, required
    assert "error" not in spec["components"]["schemas"]["AudioInputListResponse"]["properties"]
    assert "ProductStatusEvent" not in names
    for model in contract_models():
        assert model.__name__ in names
    session_props = spec["components"]["schemas"]["SessionPartialEvent"]["properties"]
    assert "stable_text" in session_props
    assert "tail_text" in session_props
    assert "stable" not in session_props
    assert "tail" not in session_props
    assert "text" not in session_props
    confirm = spec["components"]["schemas"]["UtteranceResendNeedsConfirmation"]["properties"]["ok"]
    # const false or enum [false]
    assert confirm.get("const") is False or confirm.get("enum") == [False] or confirm.get("default") is False
    history_props = spec["components"]["schemas"]["HistoryRecord"]["properties"]
    for key in ("source", "polish", "job_id", "mode"):
        assert key in history_props, key
    unknown_state = spec["components"]["schemas"]["JobQueryUnknown"]["properties"]["state"]
    assert unknown_state.get("const") == "unknown" or "unknown" in (unknown_state.get("enum") or [])
    job_state = spec["components"]["schemas"]["JobRecord"]["properties"]["state"]
    job_state_dump = str(job_state)
    assert "unknown" not in job_state_dump or job_state_dump.count("delivery_unknown")
    # JobRecord.state 不得把 unknown 列为可写取值
    ref = job_state.get("$ref")
    if ref:
        enum_name = ref.rsplit("/", 1)[-1]
        values = spec["components"]["schemas"][enum_name].get("enum") or []
        assert "unknown" not in values
        assert "delivery_unknown" in values


def test_streaming_blockers_remain_explicit():
    joined = " ".join(ASR_STREAMING_BLOCKERS)
    # 旧 P2 p95 阻塞已由 r9 证据关闭，不得残留；但限制必须如实保留，不得写成 ASR 全部已支持
    assert "1.515" not in joined and "不得宣称 sidecar 已可 120s 达标" not in joined
    assert any("suffix/prefix" in item or "拼接" in item for item in ASR_STREAMING_BLOCKERS)
    assert "用户口径" in joined and "0.749–1.226" in joined
    assert "不等于严格 p95≤1s" in joined and "超过 120s" in joined and "qwen3.5:4b" in joined
    assert "不承诺零错" in joined and "九点×21" in joined


def test_app_config_keeps_unknown_keys_and_defaults_to_qwen35():
    assert AppConfig().ollama_model == "qwen3.5:4b"
    cfg = AppConfig.model_validate({"ollama_model": "x", "future_flag": 1})
    assert cfg.ollama_model == "x"
    assert cfg.model_dump()["future_flag"] == 1


def test_resend_request_requires_original_job_id():
    req = UtteranceResendRequest(job_id="n", text="t", original_job_id="old")
    assert req.original_job_id == "old"
    with pytest.raises(ValidationError):
        UtteranceResendRequest(job_id="n", text="t")
    with pytest.raises(ValidationError):
        UtteranceResendRequest.model_validate({"job_id": "n", "text": "t", "original_job_id": ""})


def test_ui_theme_and_audio_input_contracts():
    cfg = AppConfig()
    assert cfg.ui_theme == "light"
    assert cfg.audio_input_device == ""
    with pytest.raises(ValidationError):
        AppConfig(ui_theme="sepia")  # type: ignore[arg-type]

    upd_clear = AppConfigUpdate(ui_theme="dark", audio_input_device="")
    assert upd_clear.ui_theme == "dark"
    assert upd_clear.audio_input_device == ""
    with pytest.raises(ValidationError):
        AppConfigUpdate(ui_theme="blue")  # type: ignore[arg-type]

    view = AppConfigView(
        polish_enabled=False,
        custom_keywords=[],
        translate_lang_pair="zh-en",
        translate_auto_enabled=True,
        hotkey_voice="ctrl+alt+z",
        hotkey_screenshot="ctrl+alt+x",
        hotkey_repeat="ctrl+alt+r",
        hotkey_annotate="ctrl+alt+a",
        autostart_enabled=True,
        ollama_model="qwen3.5:4b",
    )
    assert view.ui_theme == "light" and view.audio_input_device == ""

    dev = AudioInputDevice(name="USB Mic", hostapi="Windows DirectSound", is_default=True)
    assert dev.model_dump() == {"name": "USB Mic", "hostapi": "Windows DirectSound", "is_default": True}
    with pytest.raises(ValidationError):
        AudioInputDevice(name="")
    with pytest.raises(ValidationError):
        AudioInputDevice.model_validate({"name": "USB Mic", "index": 0})

    resp = AudioInputListResponse()
    assert resp.ok is True and resp.default_device is None and resp.devices == []
    with pytest.raises(ValidationError):
        AudioInputListResponse.model_validate({"ok": True, "devices": [], "error": "boom"})


def test_log_max_lines_and_logs_io_contracts():
    assert LOG_MAX_LINES_CHOICES == (500, 2000, 5000)
    assert DEFAULT_LOG_MAX_LINES == 2000
    assert LOG_READ_MAX_BYTES == 4 * 1024 * 1024
    assert ErrorCode.logs_io.value == "logs_io"
    assert ErrorBody(error=ErrorCode.logs_io, message="io").model_dump()["error"] == "logs_io"

    cfg = AppConfig()
    assert cfg.log_max_lines == 2000
    assert AppConfig(log_max_lines=500).log_max_lines == 500
    assert AppConfig(log_max_lines=5000).log_max_lines == 5000
    with pytest.raises(ValidationError):
        AppConfig(log_max_lines=1000)  # type: ignore[arg-type]

    upd = AppConfigUpdate(log_max_lines=5000)
    assert upd.log_max_lines == 5000
    with pytest.raises(ValidationError):
        AppConfigUpdate(log_max_lines=100)  # type: ignore[arg-type]

    view = AppConfigView(
        polish_enabled=False,
        custom_keywords=[],
        translate_lang_pair="zh-en",
        translate_auto_enabled=True,
        hotkey_voice="ctrl+alt+z",
        hotkey_screenshot="ctrl+alt+x",
        hotkey_repeat="ctrl+alt+r",
        hotkey_annotate="ctrl+alt+a",
        autostart_enabled=True,
        ollama_model="qwen3.5:4b",
    )
    assert view.log_max_lines == 2000


def test_logs_and_about_models_and_openapi_registration():
    entry = LogLineEntry(line_no=1, text="started", level=LogLevel.info)
    assert entry.model_dump() == {"line_no": 1, "text": "started", "level": "info"}
    with pytest.raises(ValidationError):
        LogLineEntry(line_no=0, text="bad", level=LogLevel.info)
    with pytest.raises(ValidationError):
        LogLineEntry.model_validate({"line_no": 1, "text": "x", "level": "info", "extra": 1})

    source = LogSourceInfo(
        name="app.log",
        group=LogGroup.runtime,
        exists=True,
        line_count=10,
        bytes=128,
        truncated=False,
        has_error=False,
        has_warn=True,
        updated_at="2026-11-23T00:00:00+00:00",
    )
    listed = LogSourceListResponse(log_max_lines=2000, sources=[source])
    assert listed.ok is True and listed.allowed_max_lines == [500, 2000, 5000]
    with pytest.raises(ValidationError):
        LogSourceInfo.model_validate({**source.model_dump(), "bytes": -1})

    read = LogReadResponse(
        name="app.log",
        group=LogGroup.runtime,
        exists=True,
        max_lines=2000,
        line_count=1,
        bytes=12,
        truncated=False,
        has_error=False,
        has_warn=False,
        entries=[entry],
    )
    assert read.ok is True and read.entries[0].line_no == 1
    with pytest.raises(ValidationError):
        LogReadResponse.model_validate({**read.model_dump(), "max_lines": 5001})
    with pytest.raises(ValidationError):
        LogReadResponse.model_validate({**read.model_dump(), "max_lines": 0})

    cleared = LogClearResponse(name="app.log", bytes_before=12)
    assert cleared.ok is True and cleared.cleared is True and cleared.bytes_before == 12

    summary = DiagnosticSummaryResponse(
        generated_at="2026-11-23T00:00:00+00:00",
        log_max_lines=2000,
        truncated=False,
        included_files=["app.log"],
        text="summary",
    )
    assert summary.ok is True and summary.included_files == ["app.log"]

    links = AboutLinks(
        repo_url="https://github.com/jackche8/voice-input",
        pages_url="https://jackche8.github.io/voice-input/",
        releases_url="https://github.com/jackche8/voice-input/releases",
        issues_url="https://github.com/jackche8/voice-input/issues",
    )
    about = AboutInfoResponse(
        app_name="Ollama Voice Input",
        version="3.4.6",
        summary="本地语音输入工具",
        os_name="linux",
        platform_detail="Ubuntu 24.04",
        session_type="wayland",
        python_version="3.12.3",
        run_mode=RunMode.source,
        asr_variant=AboutAsrVariant.corrupt,
        asr_detail="清单损坏",
        asr_model="Qwen/Qwen3-ASR-1.7B",
        ollama_model="qwen3.5:4b",
        ollama_version="0.40.16",
        data_root="/data",
        logs_dir="/data/logs",
        config_root="/config",
        license_title="GPL-3.0 / 商业双许可",
        license_note="说明",
        links=links,
    )
    assert about.ok is True and about.asr_variant == AboutAsrVariant.corrupt
    assert AboutInfoResponse.model_validate({**about.model_dump(), "asr_variant": None}).asr_variant is None
    with pytest.raises(ValidationError):
        AboutInfoResponse.model_validate({**about.model_dump(), "os_name": "bsd"})
    with pytest.raises(ValidationError):
        AboutInfoResponse.model_validate({**about.model_dump(), "unknown_extra": 1})

    registered = set(contract_models())
    expected_models = {
        LogLineEntry,
        LogSourceInfo,
        LogSourceListResponse,
        LogReadResponse,
        LogClearResponse,
        DiagnosticSummaryResponse,
        AboutLinks,
        AboutInfoResponse,
    }
    assert expected_models.issubset(registered)
    schemas = build_openapi_skeleton()["components"]["schemas"]
    for model in expected_models:
        assert model.__name__ in schemas
    for enum_name in ("LogLevel", "LogGroup", "RunMode", "AboutAsrVariant"):
        assert enum_name in schemas


def test_ocr_preprocess_contracts_validation_starters_and_openapi_registration():
    assert OCR_PREPROCESS_MAX_ENABLED == 3

    default_params = OcrPreprocessParams()
    assert default_params.model_dump() == {
        "invert": False,
        "contrast": 1.0,
        "brightness": 1.0,
        "sharpen": 0.0,
        "binarize_mode": "off",
        "threshold": 128,
    }
    # 边界合法值
    OcrPreprocessParams(invert=True, contrast=0.5, brightness=0.5, sharpen=0.0, binarize_mode=OcrBinarizeMode.otsu, threshold=0)
    OcrPreprocessParams(invert=False, contrast=3.0, brightness=2.0, sharpen=3.0, binarize_mode=OcrBinarizeMode.manual, threshold=255)

    # 越界、NaN/Inf 与未知字段一律拒绝
    for bad_payload in (
        {"contrast": 0.49},
        {"contrast": 3.01},
        {"contrast": float("nan")},
        {"contrast": float("inf")},
        {"brightness": 0.49},
        {"brightness": 2.01},
        {"brightness": float("-inf")},
        {"sharpen": -0.1},
        {"sharpen": 3.1},
        {"sharpen": float("nan")},
        {"binarize_mode": "adaptive"},
        {"threshold": -1},
        {"threshold": 256},
        {"extra_key": 1},
    ):
        with pytest.raises(ValidationError):
            OcrPreprocessParams.model_validate(bad_payload)

    # 预设 strip 规范化与空/超长拒绝
    preset = OcrPreprocessPreset(id="  p1  ", name="  深底终端  ", enabled=True, invert=True, contrast=1.65)
    assert preset.id == "p1" and preset.name == "深底终端" and preset.enabled is True
    for bad_preset in (
        {"id": "   ", "name": "ok"},
        {"id": "p1", "name": "   "},
        {"id": "x" * 65, "name": "ok"},
        {"id": "p1", "name": "名" * 49},
    ):
        with pytest.raises(ValidationError):
            OcrPreprocessPreset.model_validate(bad_preset)

    # 3 套内置起步参考模板单源校验
    assert [tpl.key for tpl in OCR_PREPROCESS_STARTER_TEMPLATES] == [
        "dark_invert",
        "low_contrast",
        "strong_binary",
    ]
    assert [tpl.label for tpl in OCR_PREPROCESS_STARTER_TEMPLATES] == [
        "深底反色",
        "低对比增强",
        "强黑白二值化",
    ]
    assert OCR_PREPROCESS_STARTER_TEMPLATES[0].params.invert is True
    assert OCR_PREPROCESS_STARTER_TEMPLATES[2].params.binarize_mode == OcrBinarizeMode.otsu

    # 3 条启用 + 1 条停用合法；重复 id 与启用第 4 条均被拒绝
    valid_four = [
        OcrPreprocessPreset(id="p1", name="预设1", enabled=True),
        OcrPreprocessPreset(id="p2", name="预设2", enabled=True),
        OcrPreprocessPreset(id="p3", name="预设3", enabled=True),
        OcrPreprocessPreset(id="p4", name="预设4", enabled=False),
    ]
    assert len(AppConfig(ocr_preprocess_presets=valid_four).ocr_preprocess_presets) == 4
    assert len(AppConfigUpdate(ocr_preprocess_presets=valid_four).ocr_preprocess_presets or []) == 4

    with pytest.raises(ValidationError, match="最多同时启用 3 个预处理预设"):
        AppConfigUpdate(
            ocr_preprocess_presets=[
                OcrPreprocessPreset(id=f"p{i}", name=f"预设{i}", enabled=True) for i in range(1, 5)
            ]
        )
    with pytest.raises(ValidationError, match="不能重复"):
        AppConfig(
            ocr_preprocess_presets=[
                OcrPreprocessPreset(id="dup", name="A", enabled=True),
                OcrPreprocessPreset(id="dup", name="B", enabled=False),
            ]
        )
    with pytest.raises(ValidationError):
        AppConfigUpdate.model_validate({"ocr_preprocess_starter_templates": []})

    # AppConfigView 兼容旧构造并默认携带 3 套只读起步模板
    view = AppConfigView(
        polish_enabled=False,
        custom_keywords=[],
        translate_lang_pair="zh-en",
        translate_auto_enabled=True,
        hotkey_voice="ctrl+alt+z",
        hotkey_screenshot="ctrl+alt+x",
        hotkey_repeat="ctrl+alt+r",
        hotkey_annotate="ctrl+alt+a",
        autostart_enabled=True,
        ollama_model="qwen3.5:4b",
    )
    assert view.ocr_preprocess_presets == []
    assert [t.key for t in view.ocr_preprocess_starter_templates] == ["dark_invert", "low_contrast", "strong_binary"]

    # OcrRunMeta / OcrPreprocessPreviewResponse / OCR 响应扩展校验
    meta = OcrRunMeta(
        run_mode=OcrRunMode.preset_chain,
        matched_preset_id="p1",
        matched_preset_name="深底终端",
        matched_rank=1,
        confidence=92,
        elapsed_ms=210,
        early_stopped=True,
        fallback_to_original=False,
        attempts=[
            OcrAttemptSummary(
                preset_id="p1",
                label="#1 深底终端",
                rank=1,
                confidence=92,
                score=88.5,
                valid_chars=24,
                accepted=True,
            )
        ],
    )
    tr_resp = OcrTranslateResponse(text="hi", ocr_text="hi", mode=OcrTranslateMode.raw, ocr_meta=meta)
    in_resp = OcrInplaceResponse(
        text="hi", ocr_text="hi", mode=OcrTranslateMode.raw, image_width=100, image_height=40, ocr_meta=meta
    )
    assert tr_resp.ocr_meta == meta and in_resp.ocr_meta == meta

    preview = OcrPreprocessPreviewResponse(
        image_base64="AAAA",
        image_width=100,
        image_height=50,
        preview_width=200,
        preview_height=100,
        otsu_threshold=132,
    )
    assert preview.ok is True and preview.media_type == "image/png" and preview.otsu_threshold == 132
    with pytest.raises(ValidationError):
        OcrPreprocessPreviewResponse.model_validate({**preview.model_dump(), "otsu_threshold": 256})

    registered = set(contract_models())
    expected_models = {
        OcrPreprocessParams,
        OcrPreprocessPreset,
        OcrPreprocessStarterTemplate,
        OcrAttemptSummary,
        OcrRunMeta,
        OcrPreprocessPreviewResponse,
    }
    assert expected_models.issubset(registered)
    schemas = build_openapi_skeleton()["components"]["schemas"]
    for model in expected_models:
        assert model.__name__ in schemas
    for enum_name in ("OcrBinarizeMode", "OcrRunMode"):
        assert enum_name in schemas
