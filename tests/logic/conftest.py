"""逻辑层测试夹具。禁止读写当前用户真实的配置 / 私人历史 / 任务队列（AppPaths 的 live 路径）。"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from _paths import default_app_paths

LIVE = default_app_paths(os.environ)
LIVE_CONFIG = LIVE.config
LIVE_HISTORY = LIVE.history
LIVE_JOBS = LIVE.jobs


def _fingerprint(path: Path) -> tuple[bool, int | None, int | None]:
    if not path.exists():
        return (False, None, None)
    st = path.stat()
    return (True, st.st_mtime_ns, st.st_size)


@pytest.fixture(scope="session")
def live_fingerprints() -> dict[str, tuple[bool, int | None, int | None]]:
    return {
        "config": _fingerprint(LIVE_CONFIG),
        "history": _fingerprint(LIVE_HISTORY),
        "jobs": _fingerprint(LIVE_JOBS),
    }


@pytest.fixture(autouse=True, scope="session")
def _forbid_live_mutation(live_fingerprints):
    yield
    after = {
        "config": _fingerprint(LIVE_CONFIG),
        "history": _fingerprint(LIVE_HISTORY),
        "jobs": _fingerprint(LIVE_JOBS),
    }
    assert after == live_fingerprints, f"逻辑测试不得改动 live 文件: {after} vs {live_fingerprints}"


@pytest.fixture
def tmp_store_dir(tmp_path: Path) -> Path:
    # pytest tmp_path is under the system tmp; also expose a project-local mirror when needed.
    return tmp_path
