"""Load and validate service/config.yaml."""
from __future__ import annotations

from dataclasses import dataclass, field

import yaml


class ConfigError(Exception):
    pass


MAX_RISK_PCT = 5.0   # above this a typo (10 for 1.0) is more likely than intent; allow_high_risk: true overrides


@dataclass
class AccountConfig:
    name: str
    terminal: str
    login: int
    server: str
    enabled: bool
    risk_pct: float
    stop_points: float
    tp_points: float
    risk_base: str
    slippage_points: float
    magic: int
    symbols: dict[str, str] = field(default_factory=dict)   # optional pre-mapped root -> broker symbol
    firm: str | None = None                                  # prop firm for symbol resolution
    max_risk_pct: float | None = None                        # cap on the chart's risk_pct
    allow_high_risk: bool = False                            # permit risk_pct > MAX_RISK_PCT


@dataclass
class Config:
    host: str
    port: int
    token: str
    accounts: list[AccountConfig]
    state_dir: str


_REQUIRED = ("name", "terminal", "login", "server", "enabled", "risk_pct", "stop_points",
             "tp_points", "risk_base", "slippage_points", "magic")


def _account(raw: dict) -> AccountConfig:
    for key in _REQUIRED:
        if key not in raw:
            raise ConfigError(f"account {raw.get('name', '?')}: missing field '{key}'")
    acc = AccountConfig(
        name=str(raw["name"]), terminal=str(raw["terminal"]), login=int(raw["login"]),
        server=str(raw["server"]), enabled=bool(raw["enabled"]), risk_pct=float(raw["risk_pct"]),
        stop_points=float(raw["stop_points"]), tp_points=float(raw["tp_points"]),
        risk_base=str(raw["risk_base"]), slippage_points=float(raw["slippage_points"]),
        magic=int(raw["magic"]), symbols={str(k): str(v) for k, v in dict(raw.get("symbols") or {}).items()},
        firm=(str(raw["firm"]) if raw.get("firm") else None),
        max_risk_pct=(float(raw["max_risk_pct"]) if raw.get("max_risk_pct") is not None else None),
        allow_high_risk=bool(raw.get("allow_high_risk", False)),
    )
    if acc.risk_base not in ("balance", "equity"):
        raise ConfigError(f"account {acc.name}: risk_base must be balance or equity")
    if acc.risk_pct <= 0:
        raise ConfigError(f"account {acc.name}: risk_pct must be > 0")
    if acc.risk_pct > MAX_RISK_PCT and not bool(raw.get("allow_high_risk", False)):
        raise ConfigError(f"account {acc.name}: risk_pct {acc.risk_pct} > {MAX_RISK_PCT}; "
                          f"set allow_high_risk: true if this is really intended")
    if acc.stop_points <= 0:
        raise ConfigError(f"account {acc.name}: stop_points must be > 0")
    if acc.tp_points < 0:
        raise ConfigError(f"account {acc.name}: tp_points must be >= 0")
    if acc.max_risk_pct is not None and acc.max_risk_pct <= 0:
        raise ConfigError(f"account {acc.name}: max_risk_pct must be > 0")
    return acc


def load_config(path: str) -> Config:
    with open(path, encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}
    listen = raw.get("listen") or {}
    token = raw.get("token")
    if not token or not str(token).strip():
        raise ConfigError("token must be a non-empty string")
    accounts = [_account(a) for a in (raw.get("accounts") or [])]
    if not accounts:
        raise ConfigError("accounts: at least one account is required")
    names = [a.name for a in accounts]
    if len(set(names)) != len(names):
        raise ConfigError("duplicate account name")
    return Config(
        host=str(listen.get("host", "127.0.0.1")), port=int(listen.get("port", 5577)),
        token=str(token), accounts=accounts, state_dir=str(raw.get("state_dir", "./state")),
    )
