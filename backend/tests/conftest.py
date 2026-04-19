"""Shared test config: make ``backend`` importable and provide a tmp DB."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))


@pytest.fixture(autouse=True)
def _isolate_env(tmp_path, monkeypatch):
    monkeypatch.setenv("MAC_DB_URL", f"sqlite+aiosqlite:///{tmp_path/'test.db'}")
    monkeypatch.setenv("MAC_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("MAC_WORKSPACES_DIR", str(tmp_path / "workspaces"))
    monkeypatch.setenv("MAC_ZIPS_DIR", str(tmp_path / "zips"))
    monkeypatch.setenv("MAC_CONFIG_PATH", str(BACKEND.parent / "config.yaml"))
    # bust the lru_cache on Settings / AppConfig between tests
    from app import config as _cfg

    _cfg.get_settings.cache_clear()
    _cfg.get_config.cache_clear()
    os.makedirs(tmp_path / "data", exist_ok=True)
    yield
