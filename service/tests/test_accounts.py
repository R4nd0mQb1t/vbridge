from bridge.accounts import ProcInfo, detect_firm, enumerate_terminals, import_config_yaml
from bridge.config import load_config
from bridge.settings import SettingsStore
from bridge.symbols import load_firms

FIRMS = load_firms(None)


def test_enumerate_terminals_dedupes_paths():
    procs = [ProcInfo(pid=1, name="terminal64.exe", exe="C:/Program Files/MetaTrader 5/terminal64.exe"),
             ProcInfo(pid=2, name="TERMINAL64.EXE", exe="C:/Program Files/MetaTrader 5/terminal64.exe"),
             ProcInfo(pid=3, name="terminal64.exe", exe="C:/mt5-collector/terminal64.exe"),
             ProcInfo(pid=4, name="chrome.exe", exe="C:/x/chrome.exe"),
             ProcInfo(pid=5, name="terminal64.exe", exe=None)]
    assert enumerate_terminals(procs) == ["C:/Program Files/MetaTrader 5/terminal64.exe", "C:/mt5-collector/terminal64.exe"]


def test_enumerate_terminals_live_returns_list():
    assert isinstance(enumerate_terminals(), list)


def test_detect_firm_from_server():
    assert detect_firm("FundingPips-Trial", "FundingPips Corp", FIRMS) == "FundingPips"
    assert detect_firm("FTMO-Server2", "FTMO S.R.O.", FIRMS) == "FTMO"
    assert detect_firm("Exness-MT5Trial", "Exness Technologies Ltd", FIRMS) is None


def test_detect_firm_from_company_when_server_is_generic():
    assert detect_firm("MetaQuotes-Demo", "The Trading Pit GmbH", FIRMS) == "TradingPit"
    assert detect_firm("mt5-live01", "Goat Funded Trader", FIRMS) == "GoatFunded"


def test_import_config_yaml_matches_by_login(tmp_path):
    store = SettingsStore(str(tmp_path / "settings.json"))
    store.upsert_terminal(10000001, "C:/Program Files/MetaTrader 5/terminal64.exe", "FundingPips-Trial", "FundingPips")
    store.upsert_terminal(555, "C:/other/terminal64.exe", "Other-Demo", None)
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text("""
listen: {host: 127.0.0.1, port: 5577}
token: t
accounts:
  - name: fundingpips-demo
    terminal: "C:/Program Files/MetaTrader 5/terminal64.exe"
    login: 10000001
    server: FundingPips-Trial
    enabled: true
    risk_pct: 1.0
    stop_points: 20
    tp_points: 40
    risk_base: balance
    slippage_points: 5
    magic: 557701
    max_risk_pct: 2.0
""", encoding="utf-8")
    n = import_config_yaml(load_config(str(cfg_path)), store)
    assert n == 1
    acc = store.get(10000001)
    assert acc.enabled is True and acc.magic == 557701 and acc.max_risk_pct == 2.0 and acc.firm == "FundingPips"
    assert store.get(555).enabled is False


def test_terminal_running_checks_the_exact_path():
    """I7: the probe must not relaunch a terminal the user just closed."""
    from bridge.accounts import terminal_running
    procs = [ProcInfo(pid=1, name="terminal64.exe", exe="C:/Program Files/MetaTrader 5/terminal64.exe")]
    assert terminal_running("C:/Program Files/MetaTrader 5/terminal64.exe", procs) is True
    assert terminal_running(r"c:\program files\metatrader 5\TERMINAL64.EXE", procs) is True
    assert terminal_running("C:/other/terminal64.exe", procs) is False
