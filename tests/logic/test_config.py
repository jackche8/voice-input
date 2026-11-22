from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from _config import ConfigStore, apply_config_update, config_view
from _contracts import AppConfigUpdate, ErrorCode, LogicError


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
