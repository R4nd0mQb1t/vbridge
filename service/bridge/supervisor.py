"""Supervisor: running MT5 terminals -> settings store -> one worker handle per enabled account.

The router asks the supervisor for live handles on every dispatch, so accounts can appear, be
enabled, lose their terminal, switch login, or regain Algo Trading without a service restart.
Every scan re-probes every running terminal (phase 3): a login change on the same terminal path
stops the old worker and starts the new account's; a terminal must miss MISS_LIMIT consecutive
probes before its worker is stopped (a reconnecting terminal must not interrupt mirroring).
"""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from typing import Callable, Protocol

from bridge.accounts import detect_firm
from bridge.config import AccountConfig
from bridge.protocol import AccountResult, Event
from bridge.settings import SettingsStore
from bridge.symbols import Firms

log = logging.getLogger("bridge.supervisor")

MISS_LIMIT = 2


@dataclass(frozen=True)
class ProbeResult:
    login: int
    server: str
    company: str
    balance: float
    trade_allowed: bool
    bridge_positions: int = 0      # open positions carrying one of the bridge's magic numbers


class SupervisedHandle(Protocol):
    name: str
    attach_error: str | None

    def call(self, event: Event, timeout: float = 10.0) -> AccountResult: ...
    def attach_retry(self) -> bool: ...
    def stop(self) -> None: ...


@dataclass
class AccountState:
    terminal: str
    login: int | None
    server: str | None
    firm: str | None
    enabled: bool
    attached: bool
    reason: str | None
    balance: float | None
    magic: int | None = None
    algo_trading: bool | None = None


class Supervisor:
    def __init__(self, store: SettingsStore, firms: Firms,
                 handle_factory: Callable[[AccountConfig], SupervisedHandle],
                 enumerate: Callable[[], list[str]],
                 probe: Callable[[str], ProbeResult | None]) -> None:
        self.store = store
        self.firms = firms
        self._factory = handle_factory
        self._enumerate = enumerate
        self._probe = probe
        self._lock = threading.RLock()
        self._handles: dict[int, SupervisedHandle] = {}        # login -> handle (enabled accounts)
        self._running: dict[str, ProbeResult | None] = {}      # terminal path -> last good probe (None = unreachable)
        self._misses: dict[str, int] = {}                      # terminal path -> consecutive failed probes
        self._balances: dict[int, float] = {}
        self._algo: dict[int, bool] = {}
        self._alerts: list[str] = []

    # ------------------------------------------------------------------ discovery
    def scan(self) -> None:
        """Re-probe every running terminal, register accounts, react to login changes, start/stop handles."""
        # Probes spawn a process and can take seconds: run them OUTSIDE the lock, because every dispatch
        # (including the close for an ATAS stop-out) asks handles() for the live workers.
        paths = self._enumerate()
        probes = {path: self._probe(path) for path in paths}
        with self._lock:
            running: dict[str, ProbeResult | None] = {}
            for path in paths:
                prev = self._running.get(path)
                probe = probes[path]
                if probe is None:
                    misses = self._misses.get(path, 0) + 1
                    self._misses[path] = misses
                    if prev is not None and misses < MISS_LIMIT:
                        log.info("terminal %s: probe missed (%d/%d), keeping %s", path, misses, MISS_LIMIT, prev.login)
                        running[path] = prev
                    else:
                        if prev is not None:
                            log.warning("terminal %s: %d probes missed; treating %s as gone", path, misses, prev.login)
                        else:
                            log.warning("terminal %s: cannot attach (elevated or not logged in?)", path)
                        running[path] = None
                    continue
                self._misses[path] = 0
                if prev is not None and prev.login != probe.login:
                    log.warning("terminal %s switched account %s -> %s", path, prev.login, probe.login)
                    if prev.bridge_positions > 0:
                        self._alerts.append(
                            f"Account {prev.login} ({prev.server}) had {prev.bridge_positions} open V Bridge "
                            f"position(s) when the terminal switched to {probe.login}; manage them in MT5.")
                firm = detect_firm(probe.server, probe.company, self.firms)
                self.store.upsert_terminal(probe.login, path, probe.server, firm)
                self._balances[probe.login] = probe.balance
                self._algo[probe.login] = probe.trade_allowed
                running[path] = probe
            self._running = running
            live_logins = {p.login for p in running.values() if p is not None}
            for login in list(self._handles):
                if login not in live_logins:
                    log.warning("account %s is no longer on any running terminal; stopping its worker", login)
                    self._handles.pop(login).stop()
            for acc in self.store.accounts():
                if acc.enabled and acc.login in live_logins and acc.login not in self._handles:
                    self._start(acc.login)

    def _start(self, login: int) -> None:
        cfg = self.store.to_account_config(login)
        handle = self._factory(cfg)
        self._handles[login] = handle
        if handle.attach_error:
            log.error("%s: not attached: %s (will retry)", login, handle.attach_error)
        else:
            log.info("%s: worker attached (firm %s, magic %s)", login, cfg.firm or "-", cfg.magic)

    def refresh(self) -> list[AccountState]:
        """Tray 'Refresh accounts now': re-probe everything, retry attaches, report."""
        self.scan()
        self.retry_attach()
        return self.states()

    def pop_alerts(self) -> list[str]:
        with self._lock:
            out, self._alerts = self._alerts, []
            return out

    # ------------------------------------------------------------------ control (tray)
    def set_enabled(self, login: int, enabled: bool) -> None:
        with self._lock:
            self.store.set_enabled(login, enabled)
            if not enabled and login in self._handles:
                self._handles.pop(login).stop()
            elif enabled and login not in self._handles and self._is_running(login):
                self._start(login)

    def confirm_symbol(self, login: int, root: str, name: str) -> None:
        with self._lock:
            self.store.confirm_symbol(login, root, name)
            handle = self._handles.get(login)
            if handle is not None and hasattr(handle, "confirm"):
                handle.confirm(root, name)

    def retry_attach(self) -> None:
        with self._lock:
            for login, handle in self._handles.items():
                if handle.attach_error:
                    if handle.attach_retry():
                        log.info("%s: attached after retry", login)

    def _is_running(self, login: int) -> bool:
        return any(p is not None and p.login == login for p in self._running.values())

    # ------------------------------------------------------------------ views
    def handles(self) -> list[SupervisedHandle]:
        """Every enabled account's handle, attached or not: the router answers for unattached ones with
        their attach error, so the chart sees every enabled account."""
        with self._lock:
            return list(self._handles.values())

    def states(self) -> list[AccountState]:
        with self._lock:
            out: list[AccountState] = []
            for path, probe in self._running.items():
                if probe is None:
                    out.append(AccountState(path, None, None, None, False, False,
                                            "cannot attach: run MT5 and V Bridge as the same user, not as Administrator",
                                            None))
            for acc in self.store.accounts():
                handle = self._handles.get(acc.login)
                running = self._is_running(acc.login)
                if not running:
                    reason = "terminal not running (or logged into another account)"
                elif not acc.enabled:
                    reason = "disabled: tick to enable"
                elif handle is None:
                    reason = "starting"
                elif handle.attach_error:
                    reason = handle.attach_error
                else:
                    reason = None
                out.append(AccountState(acc.terminal, acc.login, acc.server, acc.firm, acc.enabled,
                                        handle is not None and not handle.attach_error, reason,
                                        self._balances.get(acc.login), acc.magic,
                                        self._algo.get(acc.login) if running else None))
            return out

    def stop_all(self) -> None:
        with self._lock:
            for handle in self._handles.values():
                handle.stop()
            self._handles.clear()
