"""Test isolation: no test may touch the user's real %AppData% / %LocalAppData%.

2026-11-02: pairing tests wrote a dummy token into the real %AppData%\\ATAS X\\VBridge\\pairing.json
(the default mirror dir), and the live indicator read it and was locked out of the service.
"""
import os

import pytest


@pytest.fixture(autouse=True)
def _isolated_user_dirs(tmp_path, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path / "Roaming"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Local"))
    yield


def test_conftest_isolates_appdata():
    assert "Roaming" in os.environ["APPDATA"] and "pytest" in os.environ["APPDATA"].lower()
