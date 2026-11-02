import json
import os

from bridge.settings import SettingsStore


def store(tmp_path) -> SettingsStore:
    return SettingsStore(str(tmp_path / "settings.json"))


def test_new_terminal_is_disabled_with_unique_magic(tmp_path):
    s = store(tmp_path)
    a = s.upsert_terminal(10000001, "C:/mt5a/terminal64.exe", "FundingPips-Trial", "FundingPips")
    b = s.upsert_terminal(123, "C:/mt5b/terminal64.exe", "FTMO-Demo", "FTMO")
    assert a.enabled is False and b.enabled is False
    assert a.magic == 557701 and b.magic == 557702 and a.magic != b.magic
    assert a.firm == "FundingPips" and b.server == "FTMO-Demo"
    assert a.first_seen and a.last_seen


def test_magic_is_stable_across_upserts(tmp_path):
    s = store(tmp_path)
    first = s.upsert_terminal(10000001, "C:/mt5a/terminal64.exe", "FundingPips-Trial", "FundingPips")
    s.set_enabled(10000001, True)
    again = s.upsert_terminal(10000001, "C:/moved/terminal64.exe", "FundingPips-Trial", None)
    assert again.magic == first.magic and again.enabled is True
    assert again.terminal == "C:/moved/terminal64.exe" and again.firm == "FundingPips"  # firm kept when None


def test_confirm_symbol_round_trip(tmp_path):
    s = store(tmp_path)
    s.upsert_terminal(1, "t", "FundingPips-Trial", "FundingPips")
    s.confirm_symbol(1, "NQ", "NDX100")
    s.confirm_symbol(1, "NQ", "NDX100")   # idempotent
    assert s.get(1).confirmed_symbols == ["NQ:NDX100"]
    assert s.confirmed_pairs(1) == {("NQ", "NDX100")}


def test_atomic_save_and_load(tmp_path):
    s = store(tmp_path)
    s.upsert_terminal(1, "t", "FundingPips-Trial", "FundingPips")
    s.set_enabled(1, True)
    s.set_max_risk(1, 0.5)
    s2 = store(tmp_path)
    acc = s2.get(1)
    assert acc.enabled is True and acc.max_risk_pct == 0.5
    assert not [f for f in os.listdir(tmp_path) if f.endswith(".tmp")]
    data = json.loads((tmp_path / "settings.json").read_text(encoding="utf-8"))
    assert data["version"] == 1 and "1" in data["accounts"]


def test_to_account_config(tmp_path):
    s = store(tmp_path)
    s.upsert_terminal(10000001, "C:/mt5a/terminal64.exe", "FundingPips-Trial", "FundingPips")
    s.set_enabled(10000001, True)
    cfg = s.to_account_config(10000001)
    assert cfg.name == "10000001" and cfg.terminal == "C:/mt5a/terminal64.exe" and cfg.firm == "FundingPips"
    assert cfg.magic == 557701 and cfg.risk_pct == 1.0 and cfg.stop_points == 20 and cfg.symbols == {}


def test_accounts_lists_all(tmp_path):
    s = store(tmp_path)
    s.upsert_terminal(1, "a", "X-Live", None)
    s.upsert_terminal(2, "b", "Y-Demo", None)
    assert [a.login for a in s.accounts()] == [1, 2]


def test_bad_file_is_ignored_not_fatal(tmp_path):
    (tmp_path / "settings.json").write_text("{not json", encoding="utf-8")
    s = store(tmp_path)
    assert s.accounts() == []
