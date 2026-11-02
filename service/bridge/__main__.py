"""V Bridge service entry point.

  python -m bridge                       zero-config: settings in %LocalAppData%\\VBridge, tray icon
  python -m bridge --no-tray             same, console only
  python -m bridge --config config.yaml  phase-1 mode (explicit accounts, token from the file)
"""
from __future__ import annotations

import argparse
import logging
import logging.handlers
import os
import signal
import sys
import threading
import time

from bridge import pairing
from bridge.accounts import enumerate_terminals, import_config_yaml
from bridge.config import Config, load_config
from bridge.router import InProcessHandle, Router
from bridge.server import BridgeServer
from bridge.settings import SettingsStore
from bridge.state import AuditLog
from bridge.supervisor import Supervisor
from bridge.symbols import load_firms
from bridge.version import VERSION

SCAN_INTERVAL = 30.0
DEFAULT_PORT = 5577


def default_settings_dir() -> str:
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return os.path.join(base, "VBridge")


def _setup_logging(log_dir: str) -> None:
    os.makedirs(log_dir, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    for h in list(root.handlers):
        root.removeHandler(h)
    fh = logging.handlers.RotatingFileHandler(os.path.join(log_dir, "bridge.log"), maxBytes=5_000_000,
                                              backupCount=5, encoding="utf-8")
    fh.setFormatter(fmt)
    root.addHandler(fh)
    if sys.stdout is not None:
        sh = logging.StreamHandler(sys.stdout)
        sh.setFormatter(fmt)
        root.addHandler(sh)


def _run_loop(stop: dict, server: BridgeServer, router: Router, cleanup) -> int:
    log = logging.getLogger("bridge")
    signal.signal(signal.SIGINT, lambda *_: stop.update(flag=True))
    try:
        signal.signal(signal.SIGTERM, lambda *_: stop.update(flag=True))
    except (AttributeError, ValueError):
        pass
    try:
        while not stop["flag"]:
            time.sleep(0.5)
    finally:
        log.info("shutting down")
        server.stop()
        router.shutdown()
        cleanup()
    return 0


def main_config_mode(args) -> int:
    """Phase-1 behaviour: explicit accounts from config.yaml."""
    base = os.path.dirname(os.path.abspath(args.config))
    _setup_logging(os.path.join(base, "logs"))
    log = logging.getLogger("bridge")
    cfg = load_config(args.config)
    firms_dir = args.settings_dir
    enabled = [a for a in cfg.accounts if a.enabled]
    if not enabled:
        log.error("no enabled accounts in %s", args.config)
        return 2
    handles = []
    if args.inprocess:
        from bridge.mt5_api import RealMt5Api
        from bridge.worker import AccountWorker
        worker = AccountWorker(enabled[0], RealMt5Api(), load_firms(firms_dir))
        worker.attach()
        handles.append(InProcessHandle(worker))
    else:
        from bridge.procs import ProcessWorkerHandle
        for acc in enabled:
            handles.append(ProcessWorkerHandle(acc, log_dir=os.path.join(base, "logs"), firms_dir=firms_dir))
    state_dir = cfg.state_dir if os.path.isabs(cfg.state_dir) else os.path.join(base, cfg.state_dir)
    router = Router(cfg, handles, AuditLog(os.path.join(state_dir, "audit.jsonl")),
                    disable_file=os.path.join(base, "DISABLE"))
    server = BridgeServer(router, cfg.host, cfg.port)
    server.start()
    log.info("V Bridge %s up (config mode): %d account(s), kill switch file %s", VERSION, len(handles),
             os.path.join(base, "DISABLE"))
    return _run_loop({"flag": False}, server, router, lambda: [h.stop() for h in handles if hasattr(h, "stop")])


def main_settings_mode(args) -> int:
    """Zero-config behaviour: terminals auto-detected, settings.json, pairing.json, optional tray."""
    sdir = args.settings_dir
    os.makedirs(sdir, exist_ok=True)
    _setup_logging(os.path.join(sdir, "logs"))
    log = logging.getLogger("bridge")
    firms = load_firms(sdir)
    store = SettingsStore(os.path.join(sdir, "settings.json"))
    legacy = os.path.join(sdir, "config.yaml")
    if os.path.exists(legacy) and (not os.path.exists(store.path)
                                   or os.path.getmtime(legacy) > os.path.getmtime(store.path)):
        try:
            legacy_cfg = load_config(legacy)
            n = import_config_yaml(legacy_cfg, store)
            log.info("imported %d account(s) from %s", n, legacy)
            if pairing.read(sdir) is None and legacy_cfg.token:
                # keep the token users already typed into their indicator settings
                pairing.write_token(sdir, legacy_cfg.token, args.port)
        except Exception as exc:  # noqa: BLE001
            log.error("config.yaml import failed: %s", exc)
    token = pairing.write(sdir, args.port)
    if args.enable:
        for login in args.enable:
            if store.get(login) is None:
                store.upsert_terminal(login, "", "", None)
            store.set_enabled(login, True)
            log.info("account %s enabled from the command line", login)
    cfg = Config(host="127.0.0.1", port=args.port, token=token, accounts=[], state_dir=sdir)

    from bridge.procs import ProcessWorkerHandle, probe_terminal

    def factory(acc_cfg):
        return ProcessWorkerHandle(acc_cfg, log_dir=os.path.join(sdir, "logs"), firms_dir=sdir,
                                   confirmed=store.confirmed_pairs(acc_cfg.login))

    supervisor = Supervisor(store, firms, factory, enumerate_terminals,
                            lambda path: probe_terminal(path, magics=[a.magic for a in store.accounts()]))
    router = Router(cfg, supervisor.handles, AuditLog(os.path.join(sdir, "audit.jsonl")),
                    disable_file=os.path.join(sdir, "DISABLE"))
    supervisor.scan()
    server = BridgeServer(router, cfg.host, cfg.port)
    server.start()
    log.info("V Bridge %s up: settings %s, port %s, %d account(s) known", VERSION, sdir, cfg.port,
             len(store.accounts()))
    for st in supervisor.states():
        log.info("account %s on %s firm=%s enabled=%s attached=%s %s", st.login, st.server, st.firm, st.enabled,
                 st.attached, st.reason or "")

    stop = {"flag": False}

    def scanner():
        while not stop["flag"]:
            for _ in range(int(SCAN_INTERVAL * 2)):
                if stop["flag"]:
                    return
                time.sleep(0.5)
            try:
                supervisor.scan()
                supervisor.retry_attach()
            except Exception:  # noqa: BLE001
                log.exception("scan failed")

    threading.Thread(target=scanner, name="scanner", daemon=True).start()

    if not args.no_tray:
        try:
            from bridge.tray import run as run_tray
        except ImportError as exc:
            log.warning("tray unavailable (%s); running headless", exc)
        else:
            run_tray(supervisor, router, sdir, VERSION, stop)   # blocks until Quit
            stop["flag"] = True
    return _run_loop(stop, server, router, supervisor.stop_all)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="bridge")
    ap.add_argument("--config", default=None, help="phase-1 explicit config.yaml (optional)")
    ap.add_argument("--settings-dir", default=default_settings_dir())
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--no-tray", action="store_true")
    ap.add_argument("--enable", type=int, action="append", metavar="LOGIN",
                    help="enable this MT5 login without the tray (headless/VPS use); repeatable")
    ap.add_argument("--inprocess", action="store_true", help="config mode: run the first account in-process")
    args = ap.parse_args(argv)
    if args.config:
        return main_config_mode(args)
    return main_settings_mode(args)


if __name__ == "__main__":
    sys.exit(main())
