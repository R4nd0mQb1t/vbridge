"""Find MT5 terminals on this machine, recognise the prop firm, import a legacy config.yaml."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Iterable

from bridge.config import Config
from bridge.settings import SettingsStore
from bridge.symbols import Firms

log = logging.getLogger("bridge.accounts")

TERMINAL_EXE = "terminal64.exe"


@dataclass(frozen=True)
class ProcInfo:
    pid: int
    name: str
    exe: str | None


def _live_processes() -> list[ProcInfo]:
    try:
        import psutil
    except ImportError:  # pragma: no cover - packaging guarantees psutil
        log.error("psutil missing: cannot enumerate terminals")
        return []
    out = []
    for p in psutil.process_iter(["pid", "name", "exe"]):
        info = p.info
        if (info.get("name") or "").lower() != TERMINAL_EXE:
            continue
        try:
            exe = info.get("exe") or p.exe()
        except (psutil.AccessDenied, psutil.NoSuchProcess, psutil.ZombieProcess):
            exe = None
        out.append(ProcInfo(pid=int(info.get("pid") or 0), name=info.get("name") or "", exe=exe))
    return out


def enumerate_terminals(procs: Iterable[ProcInfo] | None = None) -> list[str]:
    """Distinct executable paths of running terminal64.exe processes, in first-seen order."""
    if procs is None:
        procs = _live_processes()
    seen: list[str] = []
    for p in procs:
        if (p.name or "").lower() != TERMINAL_EXE or not p.exe:
            continue
        path = p.exe.replace("\\", "/")
        if path.lower() not in [s.lower() for s in seen]:
            seen.append(path)
    return seen


def terminal_running(path: str, procs: Iterable[ProcInfo] | None = None) -> bool:
    """True when a terminal64.exe with exactly this executable path is running now. The probe checks this
    right before mt5.initialize(), which would otherwise RELAUNCH a terminal the user just closed."""
    want = (path or "").replace("\\", "/").lower()
    return any(p.exe and p.exe.replace("\\", "/").lower() == want for p in (procs if procs is not None
                                                                         else _live_processes()))


_COMPANY_HINTS = {
    "fundingpips": "FundingPips", "ftmo": "FTMO", "5ers": "The5ers", "five percent": "The5ers",
    "fivepercent": "The5ers", "fundednext": "FundedNext", "funded next": "FundedNext", "goat": "GoatFunded",
    "trading pit": "TradingPit", "tradingpit": "TradingPit",
}


def detect_firm(server: str, company: str, firms: Firms) -> str | None:
    firm = firms.firm_for_server(server or "")
    if firm:
        return firm
    hay = (company or "").lower()
    for hint, name in _COMPANY_HINTS.items():
        if hint in hay and name in firms.names():
            return name
    return None


def import_config_yaml(cfg: Config, store: SettingsStore) -> int:
    """Carry enabled/magic/max_risk_pct/allow_high_risk/firm from a phase-1 config into the store, by login."""
    count = 0
    for acc_cfg in cfg.accounts:
        acc = store.get(acc_cfg.login)
        if acc is None:
            acc = store.upsert_terminal(acc_cfg.login, acc_cfg.terminal, acc_cfg.server, acc_cfg.firm)
        acc.enabled = acc_cfg.enabled
        acc.magic = acc_cfg.magic
        acc.max_risk_pct = acc_cfg.max_risk_pct
        acc.allow_high_risk = acc_cfg.allow_high_risk
        if acc_cfg.firm:
            acc.firm = acc_cfg.firm
        acc.risk_pct, acc.stop_points, acc.tp_points = acc_cfg.risk_pct, acc_cfg.stop_points, acc_cfg.tp_points
        acc.risk_base, acc.slippage_points = acc_cfg.risk_base, acc_cfg.slippage_points
        count += 1
        log.info("imported config.yaml account %s (login %s, magic %s, enabled %s)", acc_cfg.name, acc_cfg.login,
                 acc_cfg.magic, acc_cfg.enabled)
    if count:
        store.save()
    return count
