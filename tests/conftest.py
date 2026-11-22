"""Shared fixtures for the test suite."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DATA = Path(__file__).resolve().parent / "data"


@pytest.fixture(scope="session")
def reference() -> dict:
    """Packets captured from a known-good client (credential-free subset)."""
    return json.loads((DATA / "reference_packets.json").read_text(encoding="utf-8"))


@pytest.fixture
def temp_data_dir(tmp_path: Path) -> Path:
    (tmp_path / "logs").mkdir(parents=True, exist_ok=True)
    return tmp_path


@pytest.fixture(autouse=True)
def _isolate_from_live_daemon(monkeypatch) -> None:
    """Prevent unit tests from touching a real drcomd daemon running on the host."""
    import drcom.remote_engine as remote_engine

    monkeypatch.setattr(remote_engine, "daemon_status", lambda *_a, **_kw: None)
    monkeypatch.setattr(remote_engine, "daemon_is_up", lambda *_a, **_kw: False)


@pytest.fixture
def controller(temp_data_dir: Path):
    """A controller wired to a throwaway data directory."""
    from drcom.controller import AppController

    instance = AppController(data_dir=temp_data_dir, log_level="WARNING")
    instance.config.notify.desktop = False
    yield instance
    instance.shutdown()
