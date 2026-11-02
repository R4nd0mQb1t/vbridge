from bridge.protocol import AccountResult, Event
from bridge.settings import SettingsStore
from bridge.supervisor import ProbeResult, Supervisor
from bridge.symbols import load_firms

FIRMS = load_firms(None)
FP = "C:/Program Files/MetaTrader 5/terminal64.exe"
FTMO = "C:/ftmo/terminal64.exe"


class FakeHandle:
    """Stands in for ProcessWorkerHandle: attach state is driven by the probe the test controls."""
    created = []

    def __init__(self, cfg, attach_error=None):
        self.cfg = cfg
        self.name = cfg.name
        self.attach_error = attach_error
        self.stopped = False
        self.retries = 0
        FakeHandle.created.append(self)

    def call(self, event, timeout=10.0):
        return AccountResult(self.name, "ok")

    def attach_retry(self):
        self.retries += 1
        self.attach_error = self.next_attach_error
        return self.attach_error is None

    def stop(self):
        self.stopped = True


class World:
    def __init__(self, tmp_path):
        self.store = SettingsStore(str(tmp_path / "settings.json"))
        self.terminals = [FP]
        self.probes = {FP: ProbeResult(login=10000001, server="FundingPips-Trial", company="FundingPips Corp",
                                       balance=50000.0, trade_allowed=True)}
        self.attach_errors = {}
        FakeHandle.created = []
        self.sup = Supervisor(self.store, FIRMS, self.make_handle, lambda: list(self.terminals), self.probe)

    def make_handle(self, cfg):
        h = FakeHandle(cfg, self.attach_errors.get(cfg.login))
        h.next_attach_error = None
        return h

    def probe(self, path):
        return self.probes.get(path)


def test_supervisor_adds_new_terminal_disabled(tmp_path):
    w = World(tmp_path)
    w.sup.scan()
    acc = w.store.get(10000001)
    assert acc is not None and acc.enabled is False and acc.firm == "FundingPips"
    assert w.sup.handles() == [] and FakeHandle.created == []
    st = w.sup.states()[0]
    assert (st.login, st.enabled, st.attached, st.firm) == (10000001, False, False, "FundingPips")
    assert "disabled" in st.reason


def test_supervisor_creates_handle_when_enabled(tmp_path):
    w = World(tmp_path)
    w.sup.scan()
    w.sup.set_enabled(10000001, True)
    assert len(w.sup.handles()) == 1 and FakeHandle.created[0].cfg.firm == "FundingPips"
    assert w.sup.states()[0].attached is True
    w.sup.set_enabled(10000001, False)
    assert w.sup.handles() == [] and FakeHandle.created[0].stopped is True


def test_supervisor_retries_attach(tmp_path):
    w = World(tmp_path)
    w.attach_errors[10000001] = "terminal: Algo Trading is disabled"
    w.sup.scan()
    w.sup.set_enabled(10000001, True)
    assert w.sup.handles()[0].attach_error            # enabled, not attached: still answers (with its error)
    st = w.sup.states()[0]
    assert st.enabled and not st.attached and "Algo Trading" in st.reason
    FakeHandle.created[0].next_attach_error = None    # user switched Algo Trading on
    w.sup.retry_attach()
    assert FakeHandle.created[0].retries == 1 and w.sup.handles()[0].attach_error is None


def test_supervisor_probe_failure_is_reported_not_fatal(tmp_path):
    w = World(tmp_path)
    w.terminals = [FP, "C:/elevated/terminal64.exe"]
    w.sup.scan()
    unreachable = [s for s in w.sup.states() if s.terminal == "C:/elevated/terminal64.exe"]
    assert len(unreachable) == 1 and "cannot attach" in unreachable[0].reason and unreachable[0].login is None


def test_supervisor_second_terminal_other_firm(tmp_path):
    w = World(tmp_path)
    w.terminals = [FP, FTMO]
    w.probes[FTMO] = ProbeResult(login=777, server="FTMO-Demo", company="FTMO S.R.O.", balance=100000.0,
                                 trade_allowed=True)
    w.sup.scan()
    firms = {s.login: s.firm for s in w.sup.states()}
    assert firms == {10000001: "FundingPips", 777: "FTMO"}
    assert {a.magic for a in w.store.accounts()} == {557701, 557702}


def test_supervisor_terminal_gone_marks_detached(tmp_path):
    w = World(tmp_path)
    w.sup.scan()
    w.sup.set_enabled(10000001, True)
    w.terminals = []
    w.sup.scan()
    assert w.sup.handles() == [] and "not running" in w.sup.states()[0].reason


# --- phase 3: re-probe every scan, login change on the same terminal, refresh, switch alerts ----------

def test_login_change_on_same_terminal_switches_worker(tmp_path):
    w = World(tmp_path)
    w.sup.scan()
    w.sup.set_enabled(10000001, True)
    old = FakeHandle.created[0]
    # user logs the same MT5 into a second account, already enabled earlier
    w.store.upsert_terminal(55555, FP, "FundingPips-Trial", "FundingPips")
    w.store.set_enabled(55555, True)
    w.probes[FP] = ProbeResult(login=55555, server="FundingPips-Trial", company="FundingPips Corp",
                               balance=25000.0, trade_allowed=True)
    w.sup.scan()
    assert old.stopped is True
    assert [h.cfg.login for h in w.sup.handles()] == [55555]
    states = {s.login: s for s in w.sup.states()}
    assert states[55555].attached and "not running" in states[10000001].reason


def test_new_login_on_same_terminal_is_registered_disabled(tmp_path):
    w = World(tmp_path)
    w.sup.scan()
    w.probes[FP] = ProbeResult(login=777, server="FTMO-Demo", company="FTMO S.R.O.", balance=100000.0,
                               trade_allowed=True)
    w.sup.scan()
    acc = w.store.get(777)
    assert acc is not None and acc.enabled is False and acc.firm == "FTMO"


def test_single_probe_miss_keeps_worker(tmp_path):
    w = World(tmp_path)
    w.sup.scan()
    w.sup.set_enabled(10000001, True)
    saved = w.probes.pop(FP)              # terminal reconnecting: probe fails once
    w.sup.scan()
    assert len(w.sup.handles()) == 1 and FakeHandle.created[0].stopped is False
    w.probes[FP] = saved
    w.sup.scan()
    assert len(w.sup.handles()) == 1


def test_two_probe_misses_stop_worker(tmp_path):
    w = World(tmp_path)
    w.sup.scan()
    w.sup.set_enabled(10000001, True)
    w.probes.pop(FP)
    w.sup.scan()
    w.sup.scan()
    assert w.sup.handles() == [] and FakeHandle.created[0].stopped is True


def test_switch_with_open_bridge_position_raises_alert(tmp_path):
    w = World(tmp_path)
    w.probes[FP] = ProbeResult(login=10000001, server="FundingPips-Trial", company="FundingPips Corp",
                               balance=50000.0, trade_allowed=True, bridge_positions=1)
    w.sup.scan()
    w.probes[FP] = ProbeResult(login=777, server="FTMO-Demo", company="FTMO", balance=1.0, trade_allowed=True)
    w.sup.scan()
    alerts = w.sup.pop_alerts()
    assert len(alerts) == 1 and "10000001" in alerts[0] and "open V Bridge position" in alerts[0]
    assert w.sup.pop_alerts() == []


def test_refresh_returns_states_with_algo_flag(tmp_path):
    w = World(tmp_path)
    w.probes[FP] = ProbeResult(login=10000001, server="FundingPips-Trial", company="FundingPips Corp",
                               balance=50000.0, trade_allowed=False)
    states = w.sup.refresh()
    st = [s for s in states if s.login == 10000001][0]
    assert st.algo_trading is False and st.firm == "FundingPips"


def test_refresh_retries_attach(tmp_path):
    w = World(tmp_path)
    w.attach_errors[10000001] = "terminal: Algo Trading is disabled"
    w.sup.scan()
    w.sup.set_enabled(10000001, True)
    assert w.sup.handles()[0].attach_error
    w.sup.refresh()
    assert w.sup.handles()[0].attach_error is None


def test_probes_run_outside_the_lock(tmp_path):
    """I1: a slow probe must not block handles() (every dispatch, incl. closes, calls it)."""
    import threading
    w = World(tmp_path)
    w.sup.scan()
    w.sup.set_enabled(10000001, True)
    blocked = {}

    def slow_probe(path):
        t = threading.Thread(target=lambda: blocked.setdefault("handles", w.sup.handles()))
        t.start()
        t.join(0.5)
        blocked["timed_out"] = t.is_alive()
        return w.probes.get(path)

    w.sup._probe = slow_probe
    w.sup.scan()
    assert blocked["timed_out"] is False and len(blocked["handles"]) == 1


def test_enabled_unattached_accounts_still_answer(tmp_path):
    """I5: an enabled account whose attach failed must appear in acks, not vanish."""
    w = World(tmp_path)
    w.attach_errors[10000001] = "terminal: Algo Trading is disabled"
    w.sup.scan()
    w.sup.set_enabled(10000001, True)
    assert [h.name for h in w.sup.handles()] == ["10000001"]
