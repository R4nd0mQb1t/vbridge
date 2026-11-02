"""Router: authenticates connections, dedupes sources, applies the kill switch and fans events
out to every account worker concurrently."""
from __future__ import annotations

import logging
import re
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from typing import Protocol

from bridge.config import Config
from bridge.protocol import AccountResult, Ack, Command, Event, Hello
from bridge.state import AuditLog
from bridge.worker import AccountWorker

log = logging.getLogger("bridge.router")

CALL_TIMEOUT = 10.0


class WorkerHandle(Protocol):
    name: str

    def call(self, event: Event, timeout: float = CALL_TIMEOUT) -> AccountResult: ...


class InProcessHandle:
    """A worker living in this process (tests, --inprocess debugging)."""

    def __init__(self, worker: AccountWorker) -> None:
        self.worker = worker
        self.name = worker.cfg.name

    def call(self, event: Event, timeout: float = CALL_TIMEOUT) -> AccountResult:
        return self.worker.handle(event)


class Router:
    def __init__(self, cfg: Config, handles, audit: AuditLog, disable_file: str) -> None:
        self.cfg = cfg
        self.handles = handles          # a list, or a callable returning the live list (supervisor)
        self.audit = audit
        self.disable_file = disable_file
        self._disabled_flag = False
        self._sources: dict[tuple[str, str], tuple[str, float | None]] = {}   # (account, symbol) -> (conn, arm_ts)
        self._pending: set[tuple[str, str, str]] = set()            # (account, root, symbol) to confirm in the tray
        self._lock = threading.Lock()
        self._pool = ThreadPoolExecutor(max_workers=16, thread_name_prefix="router")

    def _live(self) -> list[WorkerHandle]:
        return list(self.handles() if callable(self.handles) else self.handles)

    # ------------------------------------------------------------------ auth / kill switch
    def authenticate(self, hello: Hello) -> bool:
        return bool(hello.token) and hello.token == self.cfg.token

    @property
    def disabled(self) -> bool:
        return self._disabled_flag or os.path.exists(self.disable_file)

    def set_disabled(self, flag: bool) -> None:
        self._disabled_flag = flag
        log.warning("kill switch %s", "ON" if flag else "off")

    def command(self, cmd: Command) -> bool:
        if cmd.token != self.cfg.token:
            log.warning("command %s rejected: bad token", cmd.name)
            return False
        if cmd.name == "disable":
            self.set_disabled(True)
        elif cmd.name == "enable":
            self.set_disabled(False)
        else:
            return False
        return True

    # ------------------------------------------------------------------ sources
    def register_source(self, conn_id: str, account: str, symbol: str, arm_ts: float | None = None) -> str | None:
        """One indicator copy owns (account, symbol): the one that ARMED most recently (arm_ts). ATAS can keep an
        old copy alive next to its replacement; an older copy is told to stand by and nothing it sends is
        forwarded, however late it speaks. Without arm_ts (v2 indicators) the latest sender takes over."""
        key = (account, symbol)
        with self._lock:
            owner = self._sources.get(key)
            if owner is None or owner[0] == conn_id:
                ts = arm_ts if arm_ts is not None else (owner[1] if owner else None)
                self._sources[key] = (conn_id, ts)
                return None
            owner_conn, owner_ts = owner
            if arm_ts is not None and owner_ts is not None and arm_ts < owner_ts:
                return f"standby: a newer V Bridge copy handles {account}/{symbol}"
            log.warning("source %s/%s moved from connection %s to %s", account, symbol, owner_conn, conn_id)
            self._sources[key] = (conn_id, arm_ts)
        return None

    def unregister(self, conn_id: str) -> None:
        with self._lock:
            for key in [k for k, v in self._sources.items() if v[0] == conn_id]:
                del self._sources[key]

    # ------------------------------------------------------------------ dispatch
    def dispatch(self, conn_id: str, event: Event) -> Ack:
        handles = self._live()
        reason = self.register_source(conn_id, event.account, event.symbol, event.arm_ts)
        if reason:
            status = "warn" if reason.startswith("standby") else "error"
            ack = Ack(id=event.id, results=[AccountResult(account=h.name, status=status, reason=reason)
                                            for h in (handles or [type("H", (), {"name": "-"})()])])
        elif event.type == "open" and self.disabled:
            ack = Ack(id=event.id, results=[AccountResult(account=h.name, status="error", reason="disabled")
                                            for h in handles])
        elif not handles:
            ack = Ack(id=event.id, results=[AccountResult(account="-", status="error",
                                                          reason="no account enabled and attached (see tray)")])
        else:
            futures = [(h, None if getattr(h, "attach_error", None) else self._pool.submit(h.call, event, CALL_TIMEOUT))
                       for h in handles]
            results = []
            for handle, fut in futures:
                if fut is None:     # enabled but not attached: answer for it, never hide it
                    results.append(AccountResult(account=handle.name, status="error",
                                                 reason=f"not attached: {handle.attach_error}"))
                    continue
                try:
                    results.append(fut.result(timeout=CALL_TIMEOUT + 1))
                except Exception as exc:  # noqa: BLE001 - a dead worker must show up as an error ack
                    log.exception("worker %s unavailable", handle.name)
                    results.append(AccountResult(account=handle.name, status="error",
                                                 reason=f"worker unavailable: {exc}"))
            ack = Ack(id=event.id, results=results)
        self._collect_confirmations(ack.results)
        self.audit.append({"event": asdict(event), "results": [asdict(r) for r in ack.results]})
        log.info("event %s %s %s -> %s", event.id, event.type, event.dir or "",
                 ", ".join(f"{r.account}:{r.status}" for r in ack.results))
        return ack

    # ------------------------------------------------------------------ symbol confirmations (tray)
    _CONFIRM = re.compile(r"confirm symbol (\S+) for (\S+) in the tray")

    def _collect_confirmations(self, results: list[AccountResult]) -> None:
        for r in results:
            preview = r.preview
            note = preview.get("note") if isinstance(preview, dict) else getattr(preview, "note", None)
            for text in (r.reason, note):
                m = self._CONFIRM.search(text or "")
                if m:
                    with self._lock:
                        self._pending.add((r.account, m.group(2), m.group(1)))

    def pending_confirmations(self) -> set[tuple[str, str, str]]:
        """(account name, root, broker symbol) waiting for the user's one-time confirmation."""
        with self._lock:
            return set(self._pending)

    def clear_confirmation(self, account: str, root: str, symbol: str) -> None:
        with self._lock:
            self._pending.discard((account, root, symbol))

    def shutdown(self) -> None:
        self._pool.shutdown(wait=False)
