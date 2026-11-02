"""Per-login settings store (settings.json). Replaces config.yaml for ordinary users.

Rules: a terminal seen for the first time is disabled; magic numbers are assigned once per login
and never change (open positions are found by magic, so a change would orphan them).
"""
from __future__ import annotations

import json
import logging
import os
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone

from bridge.config import AccountConfig

log = logging.getLogger("bridge.settings")

FIRST_MAGIC = 557701
FILE_VERSION = 1


@dataclass
class AccountSettings:
    login: int
    terminal: str
    server: str
    firm: str | None
    enabled: bool
    magic: int
    max_risk_pct: float | None = None
    allow_high_risk: bool = False
    confirmed_symbols: list[str] = field(default_factory=list)   # "ROOT:SYMBOL"
    first_seen: str = ""
    last_seen: str = ""
    # defaults used when a v1 indicator sends no risk fields
    risk_pct: float = 1.0
    stop_points: float = 20.0
    tp_points: float = 40.0
    risk_base: str = "balance"
    slippage_points: float = 5.0


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class SettingsStore:
    def __init__(self, path: str) -> None:
        self.path = path
        self._lock = threading.RLock()
        self._accounts: dict[int, AccountSettings] = {}
        self.load()

    # ------------------------------------------------------------------ persistence
    def load(self) -> None:
        with self._lock:
            self._accounts = {}
            if not os.path.exists(self.path):
                return
            try:
                with open(self.path, encoding="utf-8") as fh:
                    data = json.load(fh)
                for key, raw in (data.get("accounts") or {}).items():
                    raw = {k: v for k, v in raw.items() if k in AccountSettings.__dataclass_fields__}
                    acc = AccountSettings(**raw)
                    self._accounts[int(acc.login)] = acc
            except (OSError, ValueError, TypeError) as exc:
                log.error("settings file %s unreadable (%s); starting empty, it will be rewritten", self.path, exc)
                self._accounts = {}

    def save(self) -> None:
        with self._lock:
            os.makedirs(os.path.dirname(os.path.abspath(self.path)) or ".", exist_ok=True)
            data = {"version": FILE_VERSION, "accounts": {str(k): asdict(v) for k, v in self._accounts.items()}}
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=2)
            os.replace(tmp, self.path)

    # ------------------------------------------------------------------ queries
    def accounts(self) -> list[AccountSettings]:
        with self._lock:
            return sorted(self._accounts.values(), key=lambda a: a.login)

    def get(self, login: int) -> AccountSettings | None:
        with self._lock:
            return self._accounts.get(int(login))

    def confirmed_pairs(self, login: int) -> set[tuple[str, str]]:
        acc = self.get(login)
        if acc is None:
            return set()
        pairs = set()
        for item in acc.confirmed_symbols:
            root, _, name = item.partition(":")
            if root and name:
                pairs.add((root, name))
        return pairs

    # ------------------------------------------------------------------ mutations
    def upsert_terminal(self, login: int, terminal: str, server: str, firm: str | None) -> AccountSettings:
        with self._lock:
            acc = self._accounts.get(int(login))
            if acc is None:
                used = {a.magic for a in self._accounts.values()}
                magic = FIRST_MAGIC
                while magic in used:
                    magic += 1
                acc = AccountSettings(login=int(login), terminal=terminal, server=server, firm=firm, enabled=False,
                                      magic=magic, first_seen=_now(), last_seen=_now())
                self._accounts[int(login)] = acc
                log.info("new account %s on %s (%s): disabled until enabled in the tray, magic %s",
                         login, server, firm or "firm unknown", magic)
            else:
                acc.terminal = terminal
                acc.server = server
                if firm:
                    acc.firm = firm
                acc.last_seen = _now()
            self.save()
            return acc

    def set_enabled(self, login: int, enabled: bool) -> None:
        with self._lock:
            acc = self._accounts[int(login)]
            acc.enabled = bool(enabled)
            self.save()

    def set_firm(self, login: int, firm: str | None) -> None:
        with self._lock:
            self._accounts[int(login)].firm = firm
            self.save()

    def set_max_risk(self, login: int, max_risk_pct: float | None) -> None:
        with self._lock:
            self._accounts[int(login)].max_risk_pct = max_risk_pct
            self.save()

    def confirm_symbol(self, login: int, root: str, name: str) -> None:
        with self._lock:
            acc = self._accounts[int(login)]
            key = f"{root}:{name}"
            if key not in acc.confirmed_symbols:
                acc.confirmed_symbols.append(key)
                self.save()

    # ------------------------------------------------------------------ adapters
    def to_account_config(self, login: int) -> AccountConfig:
        acc = self.get(login)
        if acc is None:
            raise KeyError(login)
        return AccountConfig(name=str(acc.login), terminal=acc.terminal, login=acc.login, server=acc.server,
                             enabled=acc.enabled, risk_pct=acc.risk_pct, stop_points=acc.stop_points,
                             tp_points=acc.tp_points, risk_base=acc.risk_base, slippage_points=acc.slippage_points,
                             magic=acc.magic, symbols={}, firm=acc.firm, max_risk_pct=acc.max_risk_pct,
                             allow_high_risk=acc.allow_high_risk)
