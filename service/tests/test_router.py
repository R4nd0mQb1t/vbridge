import json

import pytest

from bridge.config import AccountConfig, Config
from bridge.protocol import AccountResult, Command, Event, Hello
from bridge.router import InProcessHandle, Router
from bridge.sizing import SymbolSpec
from bridge.state import AuditLog
from bridge.worker import AccountWorker
from tests.fake_mt5 import FakeMt5

SPEC = SymbolSpec(name="US100", digits=2, point=0.01, tick_size=0.01, tick_value_loss=0.01,
                  volume_min=0.01, volume_max=100.0, volume_step=0.01, stops_level=0, filling_mode=1)


def acc_cfg(name: str, login: int) -> AccountConfig:
    return AccountConfig(name=name, terminal="x", login=login, server="s", enabled=True, risk_pct=1.0,
                         stop_points=20.0, tp_points=40.0, risk_base="balance", slippage_points=5.0,
                         magic=557701, symbols={"NQ": "US100"})


def ev(type_: str, dir_=None, id_=1, account="atas-demo", symbol="NQ") -> Event:
    return Event(id=id_, type=type_, symbol=symbol, account=account, security="NQZ6", dir=dir_,
                 price=20123.5, volume=1.0, ts="t")


class BoomHandle:
    name = "boom"

    def call(self, event, timeout=10.0):
        raise RuntimeError("pipe broken")


@pytest.fixture
def setup(tmp_path):
    good_api = FakeMt5(specs={"US100": SPEC})
    bad_api = FakeMt5(specs={"US100": SPEC}, login=999)
    good = AccountWorker(acc_cfg("good", 10000001), good_api)
    bad = AccountWorker(acc_cfg("bad", 10000001), bad_api)
    good.attach()
    bad.attach()
    cfg = Config(host="127.0.0.1", port=5577, token="secret", accounts=[good.cfg, bad.cfg],
                 state_dir=str(tmp_path))
    audit = AuditLog(str(tmp_path / "audit.jsonl"))
    router = Router(cfg, [InProcessHandle(good), InProcessHandle(bad)], audit,
                    disable_file=str(tmp_path / "DISABLE"))
    return router, good_api, tmp_path


def test_router_rejects_bad_token(setup):
    router, _, _ = setup
    assert router.authenticate(Hello(token="wrong")) is False
    assert router.authenticate(Hello(token="secret")) is True


def test_router_fans_out_and_reports_per_account(setup):
    router, api, _ = setup
    ack = router.dispatch("c1", ev("open", "buy"))
    assert ack.id == 1
    by_name = {r.account: r for r in ack.results}
    assert by_name["good"].status == "ok" and by_name["good"].ticket
    assert by_name["bad"].status == "error" and "login mismatch" in by_name["bad"].reason
    assert len(api.open_positions()) == 1


def test_newest_connection_takes_source(setup):
    router, api, _ = setup
    router.dispatch("c1", _arm(ev("open", "buy"), 1000.0))                 # old copy of the indicator
    ack = router.dispatch("c2", _arm(ev("close", id_=2), 2000.0))          # newer copy takes over and is served
    assert {r.account: r.status for r in ack.results}["good"] == "ok"
    assert api.open_positions() == []
    stale = router.dispatch("c1", _arm(ev("open", "buy", id_=3), 1000.0))  # the old copy is told to stand by
    assert all(r.status == "warn" and r.reason.startswith("standby") for r in stale.results)
    assert api.open_positions() == []                        # and nothing it sends is forwarded


def test_modify_passes_kill_switch(setup):
    from bridge.protocol import Follow
    router, api, _ = setup
    router.dispatch("c1", ev("open", "buy"))
    router.set_disabled(True)
    m = ev("modify", id_=4)
    m.mode, m.follow = "follow", Follow(lots=1.0, sl_offset=0.0, tp_offset=None)
    ack = router.dispatch("c1", m)
    assert {r.account: r.status for r in ack.results}["good"] == "ok"
    assert api.open_positions()[0].sl == round(api.open_positions()[0].price_open, 2)


def test_source_freed_on_unregister(setup):
    router, api, _ = setup
    router.dispatch("c1", ev("open", "buy"))
    router.unregister("c1")
    ack = router.dispatch("c2", ev("close", id_=2))
    assert {r.account: r.status for r in ack.results}["good"] == "ok"
    assert api.open_positions() == []


def test_same_connection_may_serve_two_symbols(setup):
    router, _, _ = setup
    router.dispatch("c1", ev("sync", "flat", symbol="NQ"))
    ack = router.dispatch("c1", ev("sync", "flat", id_=2, symbol="ES"))
    good = {r.account: r for r in ack.results}["good"]
    # ES is not in the config map: phase 2 discovers SPX500 (unverified -> warn), and it is NOT a duplicate source
    assert good.status in ("warn", "error") and "duplicate" not in (good.reason or "")


def test_disabled_blocks_open_only(setup):
    router, api, _ = setup
    router.dispatch("c1", ev("open", "buy"))
    router.set_disabled(True)
    ack = router.dispatch("c1", ev("open", "buy", id_=2))
    assert all(r.status == "error" and r.reason == "disabled" for r in ack.results)
    assert len(api.open_positions()) == 1
    ack = router.dispatch("c1", ev("close", id_=3))
    assert {r.account: r.status for r in ack.results}["good"] == "ok"
    assert api.open_positions() == []
    router.set_disabled(False)
    ack = router.dispatch("c1", ev("open", "sell", id_=4))
    assert {r.account: r.status for r in ack.results}["good"] == "ok"


def test_disable_file_is_respected(setup):
    router, api, tmp_path = setup
    (tmp_path / "DISABLE").write_text("")
    assert router.disabled is True
    ack = router.dispatch("c1", ev("open", "buy"))
    assert all(r.reason == "disabled" for r in ack.results) and api.open_positions() == []
    (tmp_path / "DISABLE").unlink()
    assert router.disabled is False


def test_command_toggles_disabled_with_token(setup):
    router, _, _ = setup
    assert router.command(Command("disable", "wrong")) is False and router.disabled is False
    assert router.command(Command("disable", "secret")) is True and router.disabled is True
    assert router.command(Command("enable", "secret")) is True and router.disabled is False


def test_worker_exception_becomes_error_result(setup, tmp_path):
    router, _, _ = setup
    router.handles.append(BoomHandle())
    ack = router.dispatch("c1", ev("ping"))
    boom = {r.account: r for r in ack.results}["boom"]
    assert boom.status == "error" and "worker unavailable" in boom.reason and "pipe broken" in boom.reason


def test_router_resolves_symbol_per_account_firm(tmp_path):
    """Two enabled accounts at two firms: one click trades NDX100 on one and US100.cash on the other."""
    from bridge.symbols import load_firms
    from tests.fake_mt5 import _listing
    firms = load_firms(None)
    fp_api = FakeMt5(specs={}, balance=50000.0)
    ftmo_api = FakeMt5(specs={}, balance=100000.0,
                       listings=[_listing("US100.cash", "US Tech 100 Cash CFD", 0.2), _listing("XAUUSD", "Gold", 0.01)])
    fp = AccountWorker(AccountConfig(name="fp", terminal="a", login=10000001, server="FundingPips-Trial",
                                     enabled=True, risk_pct=1.0, stop_points=20.0, tp_points=40.0,
                                     risk_base="balance", slippage_points=5.0, magic=557701, firm="FundingPips"),
                       fp_api, firms, {("NQ", "US100.cash")})
    ftmo = AccountWorker(AccountConfig(name="ftmo", terminal="b", login=10000001, server="FTMO-Demo",
                                       enabled=True, risk_pct=1.0, stop_points=20.0, tp_points=40.0,
                                       risk_base="balance", slippage_points=5.0, magic=557702, firm="FTMO"),
                         ftmo_api, firms, {("NQ", "US100.cash")})
    assert fp.attach() is None and ftmo.attach() is None
    cfg = Config(host="127.0.0.1", port=5577, token="secret", accounts=[], state_dir=str(tmp_path))
    router = Router(cfg, [InProcessHandle(fp), InProcessHandle(ftmo)], AuditLog(str(tmp_path / "a.jsonl")),
                    str(tmp_path / "DISABLE"))
    ack = router.dispatch("c1", ev("open", "buy"))
    assert {r.account: r.status for r in ack.results} == {"fp": "ok", "ftmo": "ok"}
    assert fp_api.requests[0]["symbol"] == "NDX100" and ftmo_api.requests[0]["symbol"] == "US100.cash"


def test_router_uses_live_handles_from_provider(setup):
    """Handles may come from a callable (the supervisor), re-read on every dispatch."""
    router, api, tmp_path = setup
    live = list(router.handles)
    router.handles = lambda: live
    ack = router.dispatch("c1", ev("ping"))
    assert len(ack.results) == 2
    live.pop()
    assert len(router.dispatch("c1", ev("ping", id_=2)).results) == 1


def test_audit_line_written(setup):
    router, _, tmp_path = setup
    router.dispatch("c1", ev("open", "buy", id_=7))
    lines = (tmp_path / "audit.jsonl").read_text(encoding="utf-8").strip().splitlines()
    rec = json.loads(lines[-1])
    assert rec["event"]["id"] == 7 and rec["event"]["type"] == "open"
    assert {r["account"] for r in rec["results"]} == {"good", "bad"} and rec["ts"]


def test_router_collects_pending_symbol_confirmations(tmp_path):
    from bridge.symbols import load_firms
    firms = load_firms(None)
    api = FakeMt5(specs={}, balance=50000.0)
    w = AccountWorker(AccountConfig(name="10000003", terminal="t", login=10000001, server="X", enabled=True,
                                    risk_pct=1.0, stop_points=20.0, tp_points=40.0, risk_base="balance",
                                    slippage_points=5.0, magic=557701, firm="Other"), api, firms, set())
    assert w.attach() is None
    cfg = Config(host="127.0.0.1", port=5577, token="secret", accounts=[], state_dir=str(tmp_path))
    router = Router(cfg, [InProcessHandle(w)], AuditLog(str(tmp_path / "a.jsonl")), str(tmp_path / "DISABLE"))
    router.dispatch("c1", ev("sync", "flat"))
    assert router.pending_confirmations() == {("10000003", "NQ", "NDX100")}
    router.clear_confirmation("10000003", "NQ", "NDX100")
    assert router.pending_confirmations() == set()


class PipeLikeHandle:
    """Behaves like ProcessWorkerHandle: the result crosses a Pipe as a dict and is rebuilt."""
    def __init__(self, worker):
        self.worker, self.name = worker, worker.cfg.name

    def call(self, event, timeout=10.0):
        from dataclasses import asdict
        from bridge.protocol import result_from_dict
        return result_from_dict(asdict(self.worker.handle(event)))


def test_router_survives_preview_from_worker_process(tmp_path):
    """2026-11-02 live bug: preview came back as a dict from the worker process and crashed dispatch."""
    from bridge.symbols import load_firms
    firms = load_firms(None)
    api = FakeMt5(specs={}, balance=6000.0)
    w = AccountWorker(AccountConfig(name="10000003", terminal="t", login=10000001, server="FundedNext-Server 3",
                                    enabled=True, risk_pct=1.0, stop_points=20.0, tp_points=40.0, risk_base="balance",
                                    slippage_points=5.0, magic=557702, firm="Other"), api, firms, set())
    assert w.attach() is None
    cfg = Config(host="127.0.0.1", port=5577, token="secret", accounts=[], state_dir=str(tmp_path))
    router = Router(cfg, [PipeLikeHandle(w)], AuditLog(str(tmp_path / "a.jsonl")), str(tmp_path / "DISABLE"))
    ack = router.dispatch("c1", ev("sync", "flat"))
    assert ack.results[0].preview.symbol == "NDX100"
    assert router.pending_confirmations() == {("10000003", "NQ", "NDX100")}


def test_unattached_handle_answers_with_its_error(setup):
    """I5: the router reports an unattached account instead of calling it or hiding it."""
    router, _, _ = setup
    class Unattached:
        name, attach_error = "late", "terminal: Algo Trading is disabled"
        def call(self, event, timeout=10.0):
            raise AssertionError("must not be called")
    router.handles.append(Unattached())
    late = {r.account: r for r in router.dispatch("c1", ev("ping")).results}["late"]
    assert late.status == "error" and "not attached: terminal: Algo Trading is disabled" in late.reason


def _arm(e, ts):
    e.arm_ts = ts
    return e


def test_older_copy_cannot_take_over_by_sending_later(setup):
    """I6: ownership follows the indicator's arm time, not who spoke last."""
    router, api, _ = setup
    router.dispatch("new", _arm(ev("sync", "flat"), 2000.0))
    stale = router.dispatch("old", _arm(ev("open", "buy", id_=2), 1000.0))
    assert all(r.status == "warn" and r.reason.startswith("standby") for r in stale.results)
    assert api.open_positions() == []


def test_standby_copy_takes_over_when_owner_leaves(setup):
    router, api, _ = setup
    router.dispatch("new", _arm(ev("sync", "flat"), 2000.0))
    router.unregister("new")                              # the newer copy was removed from the chart
    ack = router.dispatch("old", _arm(ev("open", "buy", id_=3), 1000.0))
    assert {r.account: r.status for r in ack.results}["good"] == "ok"
