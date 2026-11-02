"""The tray menu structure, built without showing an icon (pystray.Menu/MenuItem are plain objects)."""
from bridge.supervisor import AccountState
from bridge.tray import build_menu


class FakeStore:
    def __init__(self):
        self.enabled = {10000003: False}

    def get(self, login):
        return type("A", (), {"enabled": self.enabled.get(login, False)})()


class FakeSupervisor:
    def __init__(self):
        self.store = FakeStore()
        self.toggled = []

    def states(self):
        return [AccountState("C:/mt5/terminal64.exe", 10000003, "FundedNext-Server 3", "FundedNext", False, False,
                             "disabled: tick to enable", 6000.0, 557702, True)]

    def set_enabled(self, login, enabled):
        self.toggled.append((login, enabled))
        self.store.enabled[login] = enabled


class FakeRouter:
    disabled = False

    def pending_confirmations(self):
        return {("10000003", "NQ", "NAS100")}


def noop(*a, **k):
    pass


def make():
    sup, router = FakeSupervisor(), FakeRouter()
    menu = build_menu(sup, router, "2.1.0", {"info": None}, refresh_now=noop, toggle_kill=noop, open_logs=noop,
                      export=noop, check_update=noop, open_update=noop, quit_=noop, on_confirm=noop)
    return sup, router, menu


def item(menu, prefix):
    for it in menu.items:
        if it.text.startswith(prefix):
            return it
    raise AssertionError(f"no menu item starting with {prefix!r}: {[i.text for i in menu.items]}")


def test_accounts_is_a_submenu_listing_the_account():
    _, _, menu = make()
    acc = item(menu, "Accounts")
    assert acc.submenu is not None                     # 2026-11-02 bug: it was a click action, nothing opened
    texts = [i.text for i in acc.submenu.items]
    assert any("10000003" in t and "FundedNext" in t and "Algo ON" in t for t in texts)


def test_clicking_an_account_toggles_it():
    sup, _, menu = make()
    entry = item(menu, "Accounts").submenu.items[0]
    entry(None)                                        # pystray calls MenuItem(icon)
    assert sup.toggled == [(10000003, True)]


def test_confirm_symbols_is_a_submenu_with_pending_items():
    _, _, menu = make()
    conf = item(menu, "Confirm symbols")
    assert conf.text == "Confirm symbols (1)" and conf.submenu is not None
    assert any("NQ -> NAS100" in i.text for i in conf.submenu.items)


def test_refresh_is_first_and_default():
    _, _, menu = make()
    first = menu.items[0]
    assert first.text == "Refresh accounts now" and first.default
