from pathlib import Path

import pytest

from bridge.config import ConfigError, load_config

EXAMPLE = Path(__file__).resolve().parents[1] / "config.example.yaml"

BASE_YAML = """
listen: {{host: 127.0.0.1, port: 5577}}
token: {token}
state_dir: ./state
accounts:
  - name: {name1}
    terminal: "C:/Program Files/MetaTrader 5/terminal64.exe"
    login: 10000001
    server: FundingPips-Trial
    enabled: true
    risk_pct: {risk_pct}
    stop_points: {stop_points}
    tp_points: 40
    risk_base: {risk_base}
    slippage_points: 5
    magic: 557701
    symbols: {{NQ: US100}}
  - name: {name2}
    terminal: "C:/mt5-b/terminal64.exe"
    login: 1
    server: Other
    enabled: false
    risk_pct: 0.5
    stop_points: 10
    tp_points: 0
    risk_base: equity
    slippage_points: 5
    magic: 557702
    symbols: {{NQ: USTEC}}
"""


def write_yaml(tmp_path, **over):
    vals = dict(token="secret", name1="a", name2="b", risk_pct=1.0, stop_points=20, risk_base="balance")
    vals.update(over)
    p = tmp_path / "config.yaml"
    p.write_text(BASE_YAML.format(**vals), encoding="utf-8")
    return str(p)


def test_loads_example_config():
    cfg = load_config(str(EXAMPLE))
    assert cfg.port == 5577 and cfg.host == "127.0.0.1"
    assert cfg.accounts[0].magic == 557701
    assert cfg.accounts[0].symbols == {"NQ": "US100"}
    assert cfg.accounts[0].login == 10000001
    assert cfg.token and cfg.state_dir


def test_loads_two_accounts(tmp_path):
    cfg = load_config(write_yaml(tmp_path))
    assert [a.name for a in cfg.accounts] == ["a", "b"]
    assert cfg.accounts[1].enabled is False and cfg.accounts[1].tp_points == 0


def test_rejects_bad_risk_base(tmp_path):
    with pytest.raises(ConfigError, match="risk_base"):
        load_config(write_yaml(tmp_path, risk_base="margin"))


def test_rejects_empty_token(tmp_path):
    with pytest.raises(ConfigError, match="token"):
        load_config(write_yaml(tmp_path, token='""'))


def test_rejects_duplicate_account_names(tmp_path):
    with pytest.raises(ConfigError, match="duplicate"):
        load_config(write_yaml(tmp_path, name1="same", name2="same"))


def test_rejects_non_positive_risk(tmp_path):
    with pytest.raises(ConfigError, match="risk_pct"):
        load_config(write_yaml(tmp_path, risk_pct=0))


def test_rejects_non_positive_stop(tmp_path):
    with pytest.raises(ConfigError, match="stop_points"):
        load_config(write_yaml(tmp_path, stop_points=-5))


def test_rejects_risk_pct_above_cap_without_opt_in(tmp_path):
    with pytest.raises(ConfigError, match="risk_pct.*allow_high_risk"):
        load_config(write_yaml(tmp_path, risk_pct=10))


def test_accepts_risk_pct_above_cap_with_opt_in(tmp_path):
    p = write_yaml(tmp_path, risk_pct=10)
    text = open(p, encoding="utf-8").read().replace("    risk_pct: 10\n", "    risk_pct: 10\n    allow_high_risk: true\n", 1)
    open(p, "w", encoding="utf-8").write(text)
    assert load_config(p).accounts[0].risk_pct == 10.0


def test_rejects_missing_field(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text("listen: {host: 127.0.0.1, port: 1}\ntoken: t\naccounts: [{name: x}]\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="terminal"):
        load_config(str(p))
