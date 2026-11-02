"""ProcessWorkerHandle: runs one AccountWorker (and its MetaTrader5 connection) in its own process.

The MetaTrader5 Python API binds one terminal per process, so each account gets a process.
Requests travel over a multiprocessing Pipe:
  parent -> child: event dict | ("attach",) | ("confirm", root, name) | None (stop)
  child -> parent: (event id, result dict) | ("attached", error) | ("confirmed",)
Replies carry the event id so a late reply to a timed-out event can never be mistaken for the next ack.
"""
from __future__ import annotations

import logging
import logging.handlers
import multiprocessing as mp
import os
import threading
import time
import traceback
from dataclasses import asdict

from bridge.config import AccountConfig
from bridge.protocol import AccountResult, Event, event_from_dict, result_from_dict

log = logging.getLogger("bridge.procs")


def setup_worker_logging(log_dir: str, name: str, level: int) -> logging.Logger:
    """Rotating file per worker (logs/worker-<name>.log) plus console; returns the root logger."""
    os.makedirs(log_dir, exist_ok=True)
    root = logging.getLogger()
    root.setLevel(level)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    fh = logging.handlers.RotatingFileHandler(os.path.join(log_dir, f"worker-{name}.log"), maxBytes=5_000_000,
                                              backupCount=5, encoding="utf-8")
    fh.setFormatter(fmt)
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    for h in list(root.handlers):
        root.removeHandler(h)
    root.addHandler(fh)
    root.addHandler(sh)
    return root


def await_reply(conn, event_id: int, timeout: float) -> dict | None:
    """Read replies until one carries event_id; stale replies (from timed-out events) are discarded."""
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0 or not conn.poll(remaining):
            return None
        msg = conn.recv()
        if isinstance(msg, tuple) and len(msg) == 2 and msg[0] == event_id:
            return msg[1]
        log.warning("discarding stale reply %r (waiting for %s)", msg[0] if isinstance(msg, tuple) else msg, event_id)


def _await_tag(conn, tag: str, timeout: float):
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0 or not conn.poll(remaining):
            return None
        msg = conn.recv()
        if isinstance(msg, tuple) and msg and msg[0] == tag:
            return msg
        log.warning("discarding unexpected reply %r while waiting for %s", msg, tag)


def _worker_main(cfg: AccountConfig, conn, log_level: int, log_dir: str, firms_dir: str | None,
                 confirmed: list[tuple[str, str]]) -> None:  # runs in the child
    setup_worker_logging(log_dir, cfg.name, log_level)
    from bridge.mt5_api import RealMt5Api
    from bridge.symbols import load_firms
    from bridge.worker import AccountWorker
    worker = None
    try:
        worker = AccountWorker(cfg, RealMt5Api(), load_firms(firms_dir), set(confirmed))
        err = worker.attach()
    except Exception as exc:  # noqa: BLE001
        err = f"worker failed to start: {exc}"
        traceback.print_exc()
    conn.send(("attached", err))
    while True:
        try:
            msg = conn.recv()
        except (EOFError, OSError):
            break
        if msg is None:
            break
        if isinstance(msg, tuple):
            if msg[0] == "attach":
                err = worker.attach() if worker else err
                conn.send(("attached", err))
            elif msg[0] == "confirm" and worker is not None:
                worker.confirmed.add((msg[1], msg[2]))
                conn.send(("confirmed",))
            continue
        event = event_from_dict(msg)
        try:
            result = worker.handle(event) if worker else AccountResult(cfg.name, "error", reason=err)
        except Exception as exc:  # noqa: BLE001
            result = AccountResult(cfg.name, "error", reason=f"exception: {exc}")
        conn.send((event.id, asdict(result)))
    try:
        if worker is not None:
            worker.api.shutdown()
    except Exception:  # noqa: BLE001
        pass


def _probe_main(path: str, conn, magics: list[int]) -> None:  # runs in a short-lived child
    try:
        from bridge.mt5_api import RealMt5Api
        import MetaTrader5 as mt5
        from bridge.accounts import terminal_running
        if not terminal_running(path):          # closed since enumeration: never relaunch it
            conn.send(None)
            return
        api = RealMt5Api()
        if not api.initialize(path):
            conn.send(None)
            return
        acc = api.account_info()
        term = api.terminal_info()
        rows = mt5.positions_get() or ()
        bridge_positions = sum(1 for p in rows if int(p.magic) in set(magics))
        api.shutdown()
        if acc is None:
            conn.send(None)
            return
        conn.send({"login": acc.login, "server": acc.server, "company": getattr(acc, "company", ""),
                   "balance": acc.balance, "trade_allowed": bool(term and term.trade_allowed and acc.trade_allowed),
                   "bridge_positions": bridge_positions})
    except Exception:  # noqa: BLE001
        conn.send(None)


def probe_terminal(path: str, timeout: float = 30.0, magics: list[int] | None = None):
    """Attach to a terminal once in a child process and read who is logged in. None when it cannot."""
    from bridge.supervisor import ProbeResult
    parent, child = mp.Pipe()
    proc = mp.Process(target=_probe_main, args=(path, child, list(magics or [])), name="probe", daemon=True)
    proc.start()
    child.close()
    data = parent.recv() if parent.poll(timeout) else None
    proc.join(2)
    if proc.is_alive():
        proc.terminate()
    if not data:
        return None
    return ProbeResult(login=int(data["login"]), server=str(data["server"]), company=str(data["company"]),
                       balance=float(data["balance"]), trade_allowed=bool(data["trade_allowed"]),
                       bridge_positions=int(data.get("bridge_positions", 0)))


class ProcessWorkerHandle:
    def __init__(self, cfg: AccountConfig, log_dir: str = "logs", log_level: int = logging.INFO,
                 firms_dir: str | None = None, confirmed: set[tuple[str, str]] | None = None) -> None:
        self.cfg = cfg
        self.name = cfg.name
        self.attach_error: str | None = "not started"
        self._lock = threading.Lock()
        self._parent, child = mp.Pipe()
        self._proc = mp.Process(target=_worker_main,
                                args=(cfg, child, log_level, log_dir, firms_dir, sorted(confirmed or set())),
                                name=f"worker-{cfg.name}", daemon=True)
        self._proc.start()
        child.close()
        reply = _await_tag(self._parent, "attached", 60)
        self.attach_error = reply[1] if reply else "worker did not report within 60 s"
        if self.attach_error:
            log.error("%s: attach failed: %s", self.name, self.attach_error)
        else:
            log.info("%s: attached", self.name)

    def call(self, event: Event, timeout: float = 10.0) -> AccountResult:
        with self._lock:
            if not self._proc.is_alive():
                return AccountResult(self.name, "error", reason="worker process died")
            self._parent.send(asdict(event))
            result = await_reply(self._parent, event.id, timeout)
            if result is None:
                return AccountResult(self.name, "error", reason=f"worker timeout after {timeout}s")
            return result_from_dict(result)

    def attach_retry(self) -> bool:
        with self._lock:
            if not self._proc.is_alive():
                self.attach_error = "worker process died"
                return False
            self._parent.send(("attach",))
            reply = _await_tag(self._parent, "attached", 30)
            self.attach_error = reply[1] if reply else "attach retry timed out"
            return self.attach_error is None

    def confirm(self, root: str, name: str) -> None:
        with self._lock:
            if self._proc.is_alive():
                self._parent.send(("confirm", root, name))
                _await_tag(self._parent, "confirmed", 5)

    def stop(self) -> None:
        try:
            self._parent.send(None)
        except Exception:  # noqa: BLE001
            pass
        self._proc.join(5)
        if self._proc.is_alive():
            self._proc.terminate()
