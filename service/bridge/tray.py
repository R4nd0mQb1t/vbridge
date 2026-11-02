"""System-tray front end (pystray). Menu actions only touch the store/supervisor/router; no MT5 calls here.

build_menu() is separate from run() so the menu structure is unit-tested without showing an icon.
Submenus MUST be pystray.Menu objects (pystray.Menu(callable) re-evaluates on every open); a plain
callable passed as a MenuItem action is a click handler and opens nothing (bug fixed 2026-11-02).
"""
from __future__ import annotations

import logging
import os
import subprocess
import threading
import webbrowser
from datetime import datetime

import pystray
from PIL import Image, ImageDraw

from bridge.diagnostics import export_zip
from bridge.update import UPDATE_URL, check_latest

log = logging.getLogger("bridge.tray")

GREEN, AMBER, RED, GREY = (34, 160, 80), (220, 150, 30), (190, 40, 40), (120, 120, 120)


def _icon(color) -> Image.Image:
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse((4, 4, 60, 60), fill=color + (255,))
    d.rectangle((18, 28, 46, 36), fill=(255, 255, 255, 255))   # a small bridge bar
    return img


def worst_color(states, disabled: bool):
    if disabled:
        return AMBER
    if not states:
        return GREY
    attached = [s for s in states if s.attached]
    broken = [s for s in states if s.enabled and not s.attached]
    if broken:
        return RED
    if attached:
        return GREEN
    return GREY


def account_label(s) -> str:
    who = f"{s.login} {s.server}" if s.login else s.terminal
    firm = f" [{s.firm}]" if s.firm else ""
    bal = f" {s.balance:,.0f}" if s.balance else ""
    algo = "" if s.algo_trading is None else (" | Algo ON" if s.algo_trading else " | Algo OFF")
    state = "connected" if s.attached else (s.reason or "")
    return f"{who}{firm}{bal}{algo} - {state}"


def build_menu(supervisor, router, version: str, update_state: dict, *, refresh_now, toggle_kill, open_logs,
               export, check_update, open_update, quit_, on_confirm) -> pystray.Menu:
    # pystray rejects handlers with extra (default) parameters: bind per-item values with factories.
    def make_toggle(login):
        def toggle(icon, item):
            acc = supervisor.store.get(login)
            supervisor.set_enabled(login, not (acc.enabled if acc else False))
            if icon is not None:
                icon.update_menu()
        return toggle

    def make_checked(login):
        def checked(item):
            acc = supervisor.store.get(login)
            return bool(acc and acc.enabled)
        return checked

    def make_confirm(account, root, symbol):
        def do(icon, item):
            on_confirm(icon, account, root, symbol)
        return do

    def account_items():
        items = []
        for s in supervisor.states():
            if s.login is None:
                items.append(pystray.MenuItem(account_label(s), None, enabled=False))
                continue
            items.append(pystray.MenuItem(account_label(s), make_toggle(s.login), checked=make_checked(s.login)))
        if not items:
            items.append(pystray.MenuItem("No MT5 terminal running", None, enabled=False))
        return items

    def confirm_items():
        items = []
        for account, root, symbol in sorted(router.pending_confirmations()):
            items.append(pystray.MenuItem(f"{root} -> {symbol} on {account}: confirm",
                                          make_confirm(account, root, symbol)))
        if not items:
            items.append(pystray.MenuItem("Nothing to confirm", None, enabled=False))
        return items

    return pystray.Menu(
        pystray.MenuItem("Refresh accounts now", refresh_now, default=True),
        pystray.MenuItem("Accounts", pystray.Menu(account_items)),
        pystray.MenuItem(lambda item: f"Confirm symbols ({len(router.pending_confirmations())})",
                         pystray.Menu(confirm_items)),
        pystray.MenuItem("Kill switch (block new opens)", toggle_kill, checked=lambda item: router.disabled),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Open logs folder", open_logs),
        pystray.MenuItem("Export diagnostics", export),
        pystray.MenuItem("Check for updates", check_update),
        pystray.MenuItem(lambda item: f"Update available: {update_state['info'].latest}" if update_state["info"]
                         and update_state["info"].available else "", open_update,
                         visible=lambda item: bool(update_state["info"] and update_state["info"].available)),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem(f"V Bridge {version}", None, enabled=False),
        pystray.MenuItem("Quit", quit_),
    )


def run(supervisor, router, settings_dir: str, version: str, stop: dict) -> None:
    logs_dir = os.path.join(settings_dir, "logs")
    update_state = {"info": None}

    def repaint(icon):
        icon.icon = _icon(worst_color(supervisor.states(), router.disabled))
        icon.update_menu()

    def refresh_now(icon, item):
        def work():
            try:
                states = supervisor.refresh()
            except Exception as exc:  # noqa: BLE001
                log.exception("refresh failed")
                icon.notify(f"Refresh failed: {exc}", "V Bridge")
                return
            lines = []
            for s in states:
                if s.login is None:
                    lines.append(f"{s.terminal}: cannot attach")
                elif s.algo_trading is not None:   # running terminals only
                    lines.append(f"{s.login} {s.firm or s.server}: {'Algo ON' if s.algo_trading else 'Algo OFF'}, "
                                 f"{'connected' if s.attached else ('disabled: tick it under Accounts' if not s.enabled else s.reason)}")
            icon.notify("\n".join(lines) or "No MT5 terminal running", "V Bridge: accounts refreshed")
            for alert in supervisor.pop_alerts():
                icon.notify(alert, "V Bridge: account switched")
            repaint(icon)
        threading.Thread(target=work, name="tray-refresh-now", daemon=True).start()

    def on_confirm(icon, account, root, symbol):
        try:
            supervisor.confirm_symbol(int(account), root, symbol)
        except ValueError:
            log.error("cannot confirm for non-numeric account %s", account)
            return
        router.clear_confirmation(account, root, symbol)
        if icon is not None:
            icon.notify(f"{root} will trade as {symbol} on {account}", "V Bridge: symbol confirmed")
            icon.update_menu()

    def toggle_kill(icon, item):
        router.set_disabled(not router.disabled)
        repaint(icon)

    def open_logs(icon, item):
        os.makedirs(logs_dir, exist_ok=True)
        subprocess.Popen(["explorer", os.path.normpath(logs_dir)])

    def export(icon, item):
        out = os.path.join(os.path.expanduser("~"), "Desktop",
                           f"VBridge-diagnostics-{datetime.now():%Y%m%d-%H%M%S}.zip")
        export_zip(settings_dir, logs_dir, out)
        icon.notify(f"Diagnostics saved to {out}", "V Bridge")

    def check_update(icon, item):
        def work():
            info = check_latest(UPDATE_URL, version)
            update_state["info"] = info
            icon.notify(f"Update {info.latest} available" if info.available else "You are up to date", "V Bridge")
            icon.update_menu()
        threading.Thread(target=work, daemon=True).start()

    def open_update(icon, item):
        info = update_state["info"]
        if info and info.url:
            webbrowser.open(info.url)

    def quit_(icon, item):
        stop["flag"] = True
        icon.stop()

    menu = build_menu(supervisor, router, version, update_state, refresh_now=refresh_now, toggle_kill=toggle_kill,
                      open_logs=open_logs, export=export, check_update=check_update, open_update=open_update,
                      quit_=quit_, on_confirm=on_confirm)
    icon = pystray.Icon("vbridge", _icon(worst_color(supervisor.states(), router.disabled)), "V Bridge", menu)

    def refresher():
        while not stop["flag"]:
            for _ in range(10):
                if stop["flag"]:
                    return
                threading.Event().wait(0.5)
            try:
                repaint(icon)
                for alert in supervisor.pop_alerts():   # account switches noticed by the 30 s scan
                    icon.notify(alert, "V Bridge: account switched")
            except Exception:  # noqa: BLE001
                log.exception("tray refresh failed")

    threading.Thread(target=refresher, name="tray-refresh", daemon=True).start()
    log.info("tray up")
    icon.run()
