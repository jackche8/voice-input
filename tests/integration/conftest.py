"""隔离集成夹具。禁止指向 live 配置/历史/队列或读取生产 token。"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from _contracts import PolishRequest, PolishResponse, TranslateRequest, TranslateResponse
from _paths import default_app_paths
from _v3.isolated import compose_isolated_candidate
from tests.integration.fixtures import FixtureStreamProvider, fixture_transcribe

TEST_TOKEN = "test-isolated-composition-token"
LIVE = default_app_paths(os.environ)
LIVE_PATHS = {
    "config": LIVE.config,
    "jobs": LIVE.jobs,
    "history": LIVE.history,
    "token": LIVE.token,
}


def _fingerprint(path: Path) -> tuple[bool, int | None, int | None]:
    if not path.exists():
        return (False, None, None)
    st = path.stat()
    return (True, st.st_mtime_ns, st.st_size)


@pytest.fixture(scope="session")
def live_fingerprints() -> dict[str, tuple[bool, int | None, int | None]]:
    return {name: _fingerprint(path) for name, path in LIVE_PATHS.items()}


@pytest.fixture(autouse=True, scope="session")
def _forbid_live_mutation(live_fingerprints):
    yield
    after = {name: _fingerprint(path) for name, path in LIVE_PATHS.items()}
    assert after == live_fingerprints, f"集成测试不得改动 live 文件: {after}"


@pytest.fixture
def composition(tmp_path: Path):
    cert = LIVE.cert
    key = LIVE.key

    def polish(request: PolishRequest) -> PolishResponse:
        return PolishResponse(text=request.text.replace("错字", "正字"))

    def translate(request: TranslateRequest) -> TranslateResponse:
        return TranslateResponse(text=f"[{request.target_lang}]{request.text}", target_lang=request.target_lang)

    comp = compose_isolated_candidate(
        root=tmp_path / "isolated",
        token=TEST_TOKEN,
        cert=cert if cert.is_file() else None,
        key=key if key.is_file() else None,
        transcribe=fixture_transcribe,
        polish=polish,
        translate=translate,
        stream_provider=FixtureStreamProvider(final_text="fixture-final"),
        fixture_notes=("tests/integration FixtureStreamProvider / fixture_transcribe，非官方引擎",),
    )
    try:
        yield comp
    finally:
        comp.close()
