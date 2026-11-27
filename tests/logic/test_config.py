from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from _config import ConfigStore, apply_config_update, config_view, read_log_max_lines
from _contracts import (
    DEFAULT_LOG_MAX_LINES,
    AppConfigUpdate,
    ErrorCode,
    LogicError,
    OcrBinarizeMode,
    OcrPreprocessPreset,
)


def test_missing_file_returns_defaults_without_creating(tmp_path: Path):
    path = tmp_path / "missing.json"
    store = ConfigStore(path)
    cfg = store.load()
    assert cfg.ollama_model == "qwen3.5:4b"
    assert not path.exists()


def test_roundtrip_preserves_unknown_and_old_fields(tmp_path: Path):
    path = tmp_path / ".config.json"
    path.write_text(
        json.dumps(
            {
                "ollama_model": "kept-model",
                "whisper_model": "small",
                "whisper_language": "zh",
                "custom_keywords": ["Foo"],
                "vision_model": "legacy-vision",
                "voice_mode": "push",
                "unspecified_legacy": {"a": 1},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    store = ConfigStore(path)
    store.update(translate_auto_enabled=False)
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["ollama_model"] == "kept-model"
    assert data["translate_auto_enabled"] is False
    assert data["translate_lang_pair"] == "zh-en"
    assert data["vision_model"] == "legacy-vision"
    assert data["voice_mode"] == "push"
    assert data["unspecified_legacy"] == {"a": 1}
    assert data["custom_keywords"] == ["Foo"]


def test_corrupt_json_is_visible(tmp_path: Path):
    path = tmp_path / ".config.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(LogicError) as ei:
        ConfigStore(path).load()
    assert "JSON" in ei.value.message
    assert ei.value.code.value == "config_io"


def test_concurrent_updates_do_not_drop_fields(tmp_path: Path):
    import threading

    path = tmp_path / ".config.json"
    path.write_text(
        json.dumps({"ollama_model": "kept-model", "custom_keywords": []}),
        encoding="utf-8",
    )
    # 两个实例必须共享同一路径锁，否则会证明锁登记不正确。
    left = ConfigStore(path)
    right = ConfigStore(path)
    barrier = threading.Barrier(2)
    errors: list[BaseException] = []

    def set_lang() -> None:
        try:
            barrier.wait()
            left.update(translate_auto_enabled=False)
        except BaseException as exc:  # noqa: BLE001 — 收集后断言
            errors.append(exc)

    def set_keywords() -> None:
        try:
            barrier.wait()
            right.update(custom_keywords=["Foo"])
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    t1 = threading.Thread(target=set_lang)
    t2 = threading.Thread(target=set_keywords)
    t1.start()
    t2.start()
    t1.join()
    t2.join()
    assert errors == []
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["ollama_model"] == "kept-model"
    assert data["translate_auto_enabled"] is False
    assert data["custom_keywords"] == ["Foo"]


def test_annotate_hotkey_and_autostart_are_product_fields(tmp_path: Path):
    store = ConfigStore(tmp_path / "config.json")
    view = config_view(store.load())
    assert view.hotkey_annotate == "ctrl+alt+a" and view.autostart_enabled is True  # 缺省值
    out = apply_config_update(store, AppConfigUpdate(hotkey_annotate="Control+Shift+A", autostart_enabled=False))
    assert out.hotkey_annotate == "ctrl+shift+a" and out.autostart_enabled is False
    with pytest.raises(LogicError) as err:  # 第五键与其它四键一起查重
        apply_config_update(store, AppConfigUpdate(hotkey_annotate="ctrl+alt+z"))
    assert err.value.code.value == "invalid_request"


def test_translate_lang_pair_and_auto_enabled_replace_legacy_target_lang(tmp_path: Path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"translate_target_lang": "en"}), encoding="utf-8")
    store = ConfigStore(path)

    # 旧盘 translate_target_lang 由 extra="allow" 静默忽略，对外视图仅暴露新二字段
    view = config_view(store.load())
    dumped = view.model_dump()
    assert "translate_target_lang" not in dumped
    assert view.translate_lang_pair == "zh-en" and view.translate_auto_enabled is True

    # 更新模型拒绝已删除的旧字段
    with pytest.raises(ValidationError):
        AppConfigUpdate.model_validate({"translate_target_lang": "en"})

    # 合法更新规范化并落盘，同时不破坏磁盘保留旧键
    updated = apply_config_update(
        store,
        AppConfigUpdate(translate_lang_pair=" ZH-EN ", translate_auto_enabled=False),
    )
    assert updated.translate_lang_pair == "zh-en" and updated.translate_auto_enabled is False
    disk = json.loads(path.read_text(encoding="utf-8"))
    assert disk["translate_lang_pair"] == "zh-en" and disk["translate_auto_enabled"] is False
    assert disk["translate_target_lang"] == "en"

    # 非法语言对显式抛 invalid_request
    with pytest.raises(LogicError) as bad_upd:
        apply_config_update(store, AppConfigUpdate(translate_lang_pair="fr-de"))
    assert bad_upd.value.code == ErrorCode.invalid_request

    # 磁盘损坏写入非法语言对 → 显式抛 config_io
    bad_path = tmp_path / "bad_pair.json"
    bad_path.write_text(json.dumps({"translate_lang_pair": "fr-de"}), encoding="utf-8")
    with pytest.raises(LogicError) as bad_disk:
        ConfigStore(bad_path).load()
    assert bad_disk.value.code == ErrorCode.config_io


def test_ui_theme_and_audio_input_device_roundtrip_and_validation(tmp_path: Path):
    path = tmp_path / "config.json"
    store = ConfigStore(path)

    # 缺省默认亮色与跟随系统麦克风，且未创建磁盘文件
    initial = config_view(store.load())
    assert initial.ui_theme == "light"
    assert initial.audio_input_device == ""
    assert not path.exists()

    # 切暗色 + 指定麦克风（自动去首尾空白），并落盘可读
    updated = apply_config_update(
        store,
        AppConfigUpdate(ui_theme="dark", audio_input_device="  USB Condenser Mic  "),
    )
    assert updated.ui_theme == "dark"
    assert updated.audio_input_device == "USB Condenser Mic"
    disk = json.loads(path.read_text(encoding="utf-8"))
    assert disk["ui_theme"] == "dark" and disk["audio_input_device"] == "USB Condenser Mic"

    # 允许传空串或纯空白切回「跟随系统默认」，不影响已保存的主题
    reset = apply_config_update(store, AppConfigUpdate(audio_input_device=""))
    assert reset.audio_input_device == "" and reset.ui_theme == "dark"
    reset_ws = apply_config_update(store, AppConfigUpdate(audio_input_device="   "))
    assert reset_ws.audio_input_device == ""

    # 磁盘写入非法主题显式抛 config_io
    bad_theme = tmp_path / "bad_theme.json"
    bad_theme.write_text(json.dumps({"ui_theme": "blue"}), encoding="utf-8")
    with pytest.raises(LogicError) as err:
        ConfigStore(bad_theme).load()
    assert err.value.code == ErrorCode.config_io


def test_log_max_lines_config_roundtrip_and_validation(tmp_path: Path):
    path = tmp_path / "config.json"
    store = ConfigStore(path)

    initial = config_view(store.load())
    assert initial.log_max_lines == DEFAULT_LOG_MAX_LINES == 2000
    assert not path.exists()

    updated = apply_config_update(store, AppConfigUpdate(log_max_lines=5000))
    assert updated.log_max_lines == 5000
    assert json.loads(path.read_text(encoding="utf-8"))["log_max_lines"] == 5000

    shrunk = apply_config_update(store, AppConfigUpdate(log_max_lines=500))
    assert shrunk.log_max_lines == 500
    assert config_view(store.load()).log_max_lines == 500

    bad = tmp_path / "bad_log_max.json"
    bad.write_text(json.dumps({"log_max_lines": 1234}), encoding="utf-8")
    with pytest.raises(LogicError) as err:
        ConfigStore(bad).load()
    assert err.value.code == ErrorCode.config_io


def test_read_log_max_lines_fail_open(tmp_path: Path):
    missing = tmp_path / "missing.json"
    assert read_log_max_lines(missing) == DEFAULT_LOG_MAX_LINES
    assert not missing.exists()

    valid = tmp_path / "valid.json"
    valid.write_text(json.dumps({"log_max_lines": 500}), encoding="utf-8")
    assert read_log_max_lines(valid) == 500

    corrupt = tmp_path / "corrupt.json"
    corrupt.write_text("{broken json", encoding="utf-8")
    assert read_log_max_lines(corrupt) == DEFAULT_LOG_MAX_LINES

    invalid = tmp_path / "invalid.json"
    invalid.write_text(json.dumps({"log_max_lines": 9999}), encoding="utf-8")
    assert read_log_max_lines(invalid) == DEFAULT_LOG_MAX_LINES

    non_dict = tmp_path / "list.json"
    non_dict.write_text("[]", encoding="utf-8")
    assert read_log_max_lines(non_dict) == DEFAULT_LOG_MAX_LINES


def test_ocr_preprocess_presets_roundtrip_legacy_fallback_and_validation(tmp_path: Path):
    import threading

    path = tmp_path / "config.json"
    # 1. 旧盘缺 ocr_preprocess_presets 平滑读为 []，且只读起步模板不写入磁盘
    path.write_text(json.dumps({"ollama_model": "qwen3.5:4b", "legacy_keep": "yes"}), encoding="utf-8")
    store = ConfigStore(path)
    initial = config_view(store.load())
    assert initial.ocr_preprocess_presets == []
    assert [t.key for t in initial.ocr_preprocess_starter_templates] == [
        "dark_invert",
        "low_contrast",
        "strong_binary",
    ]

    # 2. 保存 4 条（3 启用 + 1 停用）、调序、更新并核对直接落盘内容与未知键保留
    presets = [
        OcrPreprocessPreset(
            id="p1", name="深底终端", enabled=True, invert=True, contrast=1.65, brightness=1.05, sharpen=0.8
        ),
        OcrPreprocessPreset(
            id="p2", name="灰字增强", enabled=True, invert=False, contrast=2.3, brightness=1.15, sharpen=1.6
        ),
        OcrPreprocessPreset(
            id="p3",
            name="字幕二值化",
            enabled=True,
            invert=False,
            contrast=1.8,
            brightness=1.0,
            sharpen=1.0,
            binarize_mode=OcrBinarizeMode.otsu,
            threshold=142,
        ),
        OcrPreprocessPreset(
            id="p4",
            name="手动阈值",
            enabled=False,
            binarize_mode=OcrBinarizeMode.manual,
            threshold=160,
        ),
    ]
    saved = apply_config_update(store, AppConfigUpdate(ocr_preprocess_presets=presets))
    assert [p.id for p in saved.ocr_preprocess_presets] == ["p1", "p2", "p3", "p4"]
    assert sum(1 for p in saved.ocr_preprocess_presets if p.enabled) == 3

    disk = json.loads(path.read_text(encoding="utf-8"))
    assert disk["legacy_keep"] == "yes"
    assert "ocr_preprocess_starter_templates" not in disk
    assert [item["id"] for item in disk["ocr_preprocess_presets"]] == ["p1", "p2", "p3", "p4"]
    assert disk["ocr_preprocess_presets"][2]["binarize_mode"] == "otsu"

    # 调序与停用/清空往返
    reordered = [presets[2], presets[0].model_copy(update={"enabled": False})]
    updated = apply_config_update(store, AppConfigUpdate(ocr_preprocess_presets=reordered))
    assert [(p.id, p.enabled) for p in updated.ocr_preprocess_presets] == [("p3", True), ("p1", False)]

    # 3. 绕过模型构造的非法写同样在 _normalized_update 显式拦截为 LogicError(invalid_request)
    raw_four_enabled = AppConfigUpdate.model_construct(
        ocr_preprocess_presets=[
            OcrPreprocessPreset(id=f"e{i}", name=f"P{i}", enabled=True) for i in range(4)
        ]
    )
    with pytest.raises(LogicError) as too_many:
        apply_config_update(store, raw_four_enabled)
    assert too_many.value.code == ErrorCode.invalid_request and "最多同时启用 3" in too_many.value.message

    raw_dup_id = AppConfigUpdate.model_construct(
        ocr_preprocess_presets=[
            OcrPreprocessPreset(id="same", name="A", enabled=True),
            OcrPreprocessPreset(id="same", name="B", enabled=False),
        ]
    )
    with pytest.raises(LogicError) as dup_err:
        apply_config_update(store, raw_dup_id)
    assert dup_err.value.code == ErrorCode.invalid_request and "不能重复" in dup_err.value.message

    # 4. 磁盘损坏（含 4 条启用或越界参数）读取显式抛 config_io
    bad_disk = tmp_path / "bad_presets.json"
    bad_disk.write_text(
        json.dumps(
            {
                "ocr_preprocess_presets": [
                    {"id": f"b{i}", "name": f"B{i}", "enabled": True} for i in range(4)
                ]
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(LogicError) as disk_err:
        ConfigStore(bad_disk).load()
    assert disk_err.value.code == ErrorCode.config_io

    # 5. 并发更新预设与其他配置字段互不覆盖
    left = ConfigStore(path)
    right = ConfigStore(path)
    barrier = threading.Barrier(2)
    errors: list[BaseException] = []

    def write_presets() -> None:
        try:
            barrier.wait()
            apply_config_update(left, AppConfigUpdate(ocr_preprocess_presets=[presets[0]]))
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    def write_keywords() -> None:
        try:
            barrier.wait()
            apply_config_update(right, AppConfigUpdate(custom_keywords=["ConcurrentKW"]))
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    t1 = threading.Thread(target=write_presets)
    t2 = threading.Thread(target=write_keywords)
    t1.start()
    t2.start()
    t1.join()
    t2.join()
    assert errors == []
    final_disk = json.loads(path.read_text(encoding="utf-8"))
    assert [p["id"] for p in final_disk["ocr_preprocess_presets"]] == ["p1"]
    assert final_disk["custom_keywords"] == ["ConcurrentKW"]
