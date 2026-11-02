from dataclasses import replace

import pytest

from bridge.config import AccountConfig
from bridge.mt5_api import ACTION_DEAL, ACTION_SLTP, FILLING_FOK, FILLING_IOC, TYPE_BUY, TYPE_SELL, Tick
from bridge.protocol import Event
from bridge.sizing import SymbolSpec
from bridge.worker import AccountWorker
from tests.fake_mt5 import FakeMt5

SPEC = SymbolSpec(name="US100", digits=2, point=0.01, tick_size=0.01, tick_value_loss=0.01,
                  volume_min=0.01, volume_max=100.0, volume_step=0.01, stops_level=0, filling_mode=1)


def cfg(**over) -> AccountConfig:
    base = dict(name="fundingpips-demo", terminal="C:/mt5/terminal64.exe", login=10000001,
                server="FundingPips-Trial", enabled=True, risk_pct=1.0, stop_points=20.0, tp_points=40.0,
                risk_base="balance", slippage_points=5.0, magic=557701, symbols={"NQ": "US100"})
    base.update(over)
    return AccountConfig(**base)


def ev(type_: str, dir_=None, id_=12, symbol="NQ") -> Event:
    return Event(id=id_, type=type_, symbol=symbol, account="atas-demo", security="NQZ6", dir=dir_,
                 price=20123.5, volume=1.0, ts="t")


@pytest.fixture
def api():
    return FakeMt5(specs={"US100": SPEC})


def attached(api, **over) -> AccountWorker:
    w = AccountWorker(cfg(**over), api)
    assert w.attach() is None
    return w


# --- attach -----------------------------------------------------------------

def test_attach_ok_caches_spec(api):
    w = attached(api)
    assert w.specs["NQ"] == SPEC


def test_attach_refuses_wrong_login(api):
    w = AccountWorker(cfg(login=1), api)
    assert "login mismatch" in w.attach()


def test_attach_refuses_when_trade_not_allowed(api):
    api.terminal_trade_allowed = False
    assert "algo trading" in AccountWorker(cfg(), api).attach().lower()


def test_attach_refuses_when_account_trading_disabled(api):
    api.trade_allowed = False
    assert "account" in AccountWorker(cfg(), api).attach()


def test_attach_refuses_unmapped_symbol(api):
    assert "USTEC" in AccountWorker(cfg(symbols={"NQ": "USTEC"}), api).attach()


def test_attach_refuses_when_initialize_fails(api):
    api.init_ok = False
    assert "initialize failed" in AccountWorker(cfg(), api).attach()


def test_handle_after_failed_attach_reports_error(api):
    w = AccountWorker(cfg(login=1), api)
    w.attach()
    r = w.handle(ev("open", "buy"))
    assert r.status == "error" and "login mismatch" in r.reason


# --- open --------------------------------------------------------------------

def test_open_buy_sizes_from_balance_and_attaches_bracket(api):
    w = attached(api)
    r = w.handle(ev("open", "buy"))
    assert r.status == "ok" and r.lots == 50.0          # 1% of 100k / (20 pts x $1)
    fill = api.tick.ask
    assert (r.sl, r.tp) == (round(fill - 20, 2), round(fill + 40, 2))
    req = api.requests[0]
    assert req["action"] == ACTION_DEAL and req["type"] == TYPE_BUY and req["symbol"] == "US100"
    assert req["volume"] == 50.0 and req["magic"] == 557701 and req["comment"] == "atas-12"
    assert req["deviation"] == 500 and req["type_filling"] == FILLING_FOK
    pos = api.open_positions()[0]
    assert (pos.sl, pos.tp, pos.ticket) == (r.sl, r.tp, r.ticket)


def test_open_uses_equity_when_configured(api):
    w = attached(api, risk_base="equity")
    assert w.handle(ev("open", "buy")).lots == 49.75


def test_open_sell_bracket_sides(api):
    w = attached(api)
    r = w.handle(ev("open", "sell"))
    fill = api.tick.bid
    assert (r.sl, r.tp) == (round(fill + 20, 2), round(fill - 40, 2))
    assert api.requests[0]["type"] == TYPE_SELL


def test_open_picks_ioc_when_fok_not_allowed(api):
    api.specs["US100"] = replace(SPEC, filling_mode=2)
    attached(api).handle(ev("open", "buy"))
    assert api.requests[0]["type_filling"] == FILLING_IOC


def test_open_no_tp_when_tp_points_zero(api):
    r = attached(api, tp_points=0.0).handle(ev("open", "buy"))
    assert r.status == "ok" and r.tp is None and api.requests[0]["tp"] == 0.0


def test_open_when_already_mirrored_warns(api):
    w = attached(api)
    w.handle(ev("open", "buy"))
    r = w.handle(ev("open", "buy", id_=13))
    assert r.status == "warn" and "already mirrored" in r.reason
    assert len(api.open_positions()) == 1


def test_open_below_min_lot_errors(api):
    r = attached(api, risk_pct=0.0001).handle(ev("open", "buy"))
    assert r.status == "error" and "below min lot" in r.reason and api.requests == []


def test_open_stop_below_stops_level_errors(api):
    api.specs["US100"] = replace(SPEC, stops_level=3000)
    r = attached(api).handle(ev("open", "buy"))
    assert r.status == "error" and "stop below broker minimum" in r.reason and api.requests == []


def test_open_unmapped_alias_is_discovered_but_needs_confirmation(api):
    # phase 2: an alias missing from the config map falls to discovery (SPX500 here) and, being unverified,
    # is blocked until confirmed; nothing is sent
    r = attached(api).handle(ev("open", "buy", symbol="ES"))
    assert r.status == "error" and "confirm symbol SPX500" in r.reason and api.requests == []


def test_open_unknown_root_errors(api):
    r = attached(api).handle(ev("open", "buy", symbol="ZB"))
    assert r.status == "error" and "not resolved" in r.reason and api.requests == []


def test_open_retries_on_requote_then_succeeds(api):
    api.retcodes = [10004]
    r = attached(api).handle(ev("open", "buy"))
    assert r.status == "ok" and len(api.requests) == 2


def test_open_gives_up_after_three_attempts(api):
    api.retcodes = [10004, 10020, 10021]
    r = attached(api).handle(ev("open", "buy"))
    assert r.status == "error" and len(api.requests) == 3 and "10021" in r.reason


def test_open_non_retryable_fails_at_once(api):
    api.retcodes = [10019]  # no money
    r = attached(api).handle(ev("open", "buy"))
    assert r.status == "error" and len(api.requests) == 1


def test_open_sltp_fallback_modify(api):
    api.drop_sltp_on_open = True
    r = attached(api).handle(ev("open", "buy"))
    assert r.status == "ok"
    assert [q["action"] for q in api.requests] == [ACTION_DEAL, ACTION_SLTP]
    pos = api.open_positions()[0]
    assert pos.sl == round(api.tick.ask - 20, 2) and pos.tp == round(api.tick.ask + 40, 2)


def test_open_sltp_fallback_closes_when_modify_fails(api):
    api.drop_sltp_on_open = True
    api.fail_sltp = True
    r = attached(api).handle(ev("open", "buy"))
    assert r.status == "error" and "bracket failed" in r.reason
    assert api.open_positions() == []


def test_open_recomputes_bracket_from_fill_price(api):
    api.fill_offset = 1.5
    r = attached(api).handle(ev("open", "buy"))
    fill = api.tick.ask + 1.5
    assert r.status == "ok" and (r.sl, r.tp) == (round(fill - 20, 2), round(fill + 40, 2))
    assert api.requests[-1]["action"] == ACTION_SLTP and api.requests[-1]["sl"] == round(fill - 20, 2)
    assert api.open_positions()[0].sl == round(fill - 20, 2)


# --- close -------------------------------------------------------------------

def test_close_closes_only_magic_positions(api):
    w = attached(api)
    foreign = api.add_position("US100", magic=1)
    r1 = w.handle(ev("open", "sell"))
    r = w.handle(ev("close", id_=13))
    assert r.status == "ok" and r.closed == [r1.ticket]
    assert [p.ticket for p in api.open_positions()] == [foreign]
    req = api.requests[-1]
    assert req["position"] == r1.ticket and req["type"] == TYPE_BUY and req["comment"] == "atas-13-close"


def test_close_with_no_bridge_position_is_ok(api):
    r = attached(api).handle(ev("close"))
    assert r.status == "ok" and r.closed == []


def test_close_retries_then_reports_failure(api):
    w = attached(api)
    w.handle(ev("open", "buy"))
    api.retcodes = [10004, 10004, 10004]
    r = w.handle(ev("close", id_=13))
    assert r.status == "error" and len(api.open_positions()) == 1


def test_open_after_bracket_hit_opens_again(api):
    w = attached(api)
    first = w.handle(ev("open", "buy"))
    api.remove_position(first.ticket)   # broker bracket finished the trade
    second = w.handle(ev("open", "buy", id_=14))
    assert second.status == "ok" and second.ticket != first.ticket


# --- sync / ping ---------------------------------------------------------------

def test_sync_flat_closes(api):
    w = attached(api)
    r1 = w.handle(ev("open", "buy"))
    r = w.handle(ev("sync", "flat", id_=15))
    assert r.status == "ok" and r.closed == [r1.ticket] and api.open_positions() == []


def test_sync_dir_without_position_warns(api):
    r = attached(api).handle(ev("sync", "buy"))
    assert r.status == "warn" and "not mirrored" in r.reason


def test_sync_dir_with_position_is_ok(api):
    w = attached(api)
    w.handle(ev("open", "sell"))
    assert w.handle(ev("sync", "sell", id_=16)).status == "ok"


def test_ping_ok(api):
    assert attached(api).handle(ev("ping")).status == "ok"


# --- review fixes: direction-aware reconcile --------------------------------

def test_open_opposite_direction_closes_then_opens(api):
    w = attached(api)
    first = w.handle(ev("open", "buy"))
    r = w.handle(ev("open", "sell", id_=20))
    assert r.status == "ok" and r.ticket != first.ticket
    positions = api.open_positions()
    assert len(positions) == 1 and positions[0].type == TYPE_SELL and r.closed == [first.ticket]


def test_sync_opposite_direction_closes_and_warns(api):
    w = attached(api)
    first = w.handle(ev("open", "buy"))
    r = w.handle(ev("sync", "sell", id_=21))
    assert r.status == "warn" and "direction mismatch" in r.reason and r.closed == [first.ticket]
    assert api.open_positions() == []


def test_sync_same_direction_is_ok(api):
    w = attached(api)
    w.handle(ev("open", "sell"))
    assert w.handle(ev("sync", "sell", id_=22)).status == "ok"


# --- review fixes: positions_get failure is an error, not "flat" ---------------

def test_close_errors_when_positions_unavailable(api):
    w = attached(api)
    w.handle(ev("open", "buy"))
    api.positions_none = True
    r = w.handle(ev("close", id_=23))
    assert r.status == "error" and "positions unavailable" in r.reason
    api.positions_none = False
    assert len(api.open_positions()) == 1


def test_open_errors_when_positions_unavailable(api):
    api.positions_none = True
    r = attached(api).handle(ev("open", "buy"))
    assert r.status == "error" and "positions unavailable" in r.reason and api.requests == []


def test_sync_flat_errors_when_positions_unavailable(api):
    api.positions_none = True
    r = attached(api).handle(ev("sync", "flat"))
    assert r.status == "error" and "positions unavailable" in r.reason


# --- review fixes: bracket reference and SLTP edge cases -----------------------

def test_open_uses_position_open_price_when_result_price_is_zero(api):
    api.report_price_zero = True
    r = attached(api).handle(ev("open", "buy"))
    assert r.status == "ok"
    pos = api.open_positions()[0]
    assert (r.sl, r.tp) == (round(pos.price_open - 20, 2), round(pos.price_open + 40, 2))
    assert pos.sl == r.sl and pos.sl > 0


def test_open_treats_sltp_no_changes_as_success(api):
    api.drop_sltp_on_open = True
    api.sltp_no_changes = True
    r = attached(api).handle(ev("open", "buy"))
    assert r.status == "ok" and len(api.open_positions()) == 1


def test_open_does_not_modify_when_bracket_already_matches(api):
    r = attached(api).handle(ev("open", "buy"))
    assert r.status == "ok" and [q["action"] for q in api.requests] == [ACTION_DEAL]


# --- phase 2: per-event risk, firm resolution, guards, preview ---------------------
from bridge.symbols import load_firms  # noqa: E402

FIRMS = load_firms(None)


def ev2(type_, dir_=None, id_=50, symbol="NQ", **risk) -> Event:
    e = ev(type_, dir_, id_=id_, symbol=symbol)
    for k, v in risk.items():
        setattr(e, k, v)
    return e


def attached2(api, confirmed=(), **over) -> AccountWorker:
    over.setdefault("symbols", {})
    over.setdefault("firm", "FundingPips")
    w = AccountWorker(cfg(**over), api, FIRMS, set(confirmed))
    assert w.attach() is None
    return w


@pytest.fixture
def fp():
    """FundingPips-like terminal: no pre-mapped symbols, resolution comes from the firm table."""
    return FakeMt5(balance=50000.0, equity=50000.0, tick=Tick(bid=29470.0, ask=29470.5))


def test_open_resolves_symbol_from_firm_table(fp):
    r = attached2(fp).handle(ev2("open", "buy"))
    assert r.status == "ok" and fp.requests[0]["symbol"] == "NDX100"
    assert r.lots == 1.25   # 1% of 50k = 500 / (20 pts x $20)


def test_open_uses_event_risk_fields(fp):
    r = attached2(fp).handle(ev2("open", "buy", risk_pct=0.5, stop_points=10, tp_points=0))
    assert r.status == "ok" and r.lots == 1.25 and r.tp is None   # 250 / (10 x 20)
    assert r.sl == round(fp.open_positions()[0].price_open - 10, 2)


def test_open_without_event_risk_uses_account_defaults(fp):
    r = attached2(fp, risk_pct=0.2, stop_points=50).handle(ev2("open", "sell"))
    assert r.status == "ok" and r.lots == 0.1   # 100 / (50 x 20)


def test_event_risk_base_equity(fp):
    fp.equity = 40000.0
    r = attached2(fp).handle(ev2("open", "buy", risk_base="equity"))
    assert r.lots == 1.0   # 400 / 400


def test_max_risk_pct_caps_event_risk(fp):
    r = attached2(fp, max_risk_pct=0.5).handle(ev2("open", "buy", risk_pct=2.0))
    assert r.status == "ok" and r.lots == 0.62   # capped to 0.5%: 250 / (20 x 20) = 0.625 floored


def test_risk_above_5_refused(fp):
    r = attached2(fp).handle(ev2("open", "buy", risk_pct=6.0))
    assert r.status == "error" and "5%" in r.reason and fp.requests == []


def test_risk_above_5_allowed_when_account_opts_in(fp):
    r = attached2(fp, allow_high_risk=True).handle(ev2("open", "buy", risk_pct=6.0))
    assert r.status == "ok"


def test_unverified_symbol_blocked_until_confirmed(fp):
    r = attached2(fp, firm="Other").handle(ev2("open", "buy", firm="Other"))
    assert r.status == "error" and "confirm symbol NDX100" in r.reason and fp.requests == []


def test_confirmed_unverified_symbol_trades(fp):
    r = attached2(fp, firm="Other", confirmed=[("NQ", "NDX100")]).handle(ev2("open", "buy", firm="Other"))
    assert r.status == "ok" and fp.requests[0]["symbol"] == "NDX100"


def test_override_from_event_wins(fp):
    r = attached2(fp).handle(ev2("open", "buy", symbol_override="SPX500", stop_points=5))
    assert r.status == "ok" and fp.requests[0]["symbol"] == "SPX500"


def test_price_scale_mismatch_blocks_open(fp):
    r = attached2(fp).handle(ev2("open", "buy", chart_price=20000.0))   # broker ~29470: 32% off
    assert r.status == "error" and "price scale mismatch" in r.reason and fp.requests == []


def test_small_basis_is_fine(fp):
    assert attached2(fp).handle(ev2("open", "buy", chart_price=29600.0)).status == "ok"


def test_stop_wider_than_5pct_refused(fp):
    r = attached2(fp).handle(ev2("open", "buy", stop_points=2000))
    assert r.status == "error" and "5%" in r.reason and fp.requests == []


def test_sync_ack_has_preview_with_lots(fp):
    r = attached2(fp).handle(ev2("sync", "flat", risk_pct=1.0, stop_points=20, chart_price=29480.0))
    assert r.status == "ok"
    p = r.preview
    assert (p.symbol, p.verified, p.source, p.lots, p.balance, p.value_per_point) == \
        ("NDX100", True, "table", 1.25, 50000.0, 20.0)
    assert p.note is None


def test_sync_preview_notes_unresolved_symbol(fp):
    r = attached2(fp, firm="FundingPips").handle(ev2("sync", "flat", symbol="ZB"))
    assert r.status == "error" and r.preview.symbol is None and "not resolved" in r.preview.note


def test_sync_preview_notes_guard(fp):
    r = attached2(fp).handle(ev2("sync", "flat", chart_price=1000.0))
    assert r.status == "warn" and "price scale mismatch" in r.preview.note


def test_sync_preview_notes_unconfirmed(fp):
    r = attached2(fp, firm="Other").handle(ev2("sync", "flat", firm="Other"))
    assert r.status == "warn" and "confirm symbol NDX100" in r.preview.note and r.preview.verified is False


def test_micro_root_trades_full_cfd(fp):
    r = attached2(fp).handle(ev2("open", "buy", symbol="MNQ"))
    assert r.status == "ok" and fp.requests[0]["symbol"] == "NDX100"


def test_sync_preview_notes_broker_lot_cap(fp):
    # gold: $1 per $1 move per lot; 1% of 50k over a $2 stop wants 250 lots, broker max is 5
    r = attached2(fp).handle(ev2("sync", "flat", symbol="GC", risk_pct=1.0, stop_points=2))
    assert r.preview.lots == 5.0 and "capped at broker max 5" in r.preview.note
    assert r.status == "warn"


def test_sync_preview_notes_missing_quote_and_does_not_skip_guards(fp):
    fp.tick = None   # symbol just added to Market Watch, no quote yet
    r = attached2(fp).handle(ev2("sync", "flat", chart_price=29470.0, stop_points=2000))
    assert r.preview.lots is None and "no quote yet" in r.preview.note
    assert r.status == "warn"


def test_open_refused_without_quote(fp):
    fp.tick = None
    r = attached2(fp).handle(ev2("open", "buy"))
    assert r.status == "error" and "no quote" in r.reason and fp.requests == []


def test_gold_sizing_from_listing(fp):
    # XAUUSD: tick 0.01 = $0.01 per lot -> $1 per $1 move per lot; 0.1% of 50k = $50 over a $2 stop -> 25 lots, capped 5
    r = attached2(fp).handle(ev2("open", "buy", symbol="GC", risk_pct=0.1, stop_points=2, tp_points=0))
    assert r.status == "ok" and fp.requests[0]["symbol"] == "XAUUSD" and r.lots == 5.0


# --- phase 3: Follow ATAS (lots + ATAS bracket offsets), modify, sync repair -------------------------
from bridge.protocol import Follow  # noqa: E402


def fev(type_, dir_=None, id_=70, lots=0.5, sl=20.0, tp=12.5, symbol="NQ"):
    e = ev2(type_, dir_, id_=id_, symbol=symbol)
    e.mode = "follow"
    e.follow = Follow(lots=lots, sl_offset=sl, tp_offset=tp, atas_entry=28775.5)
    return e


def test_follow_open_uses_lots_and_offsets(fp):
    r = attached2(fp).handle(fev("open", "buy"))
    fill = fp.open_positions()[0].price_open
    assert r.status == "ok" and r.lots == 0.5 and fp.requests[0]["volume"] == 0.5
    assert (r.sl, r.tp) == (round(fill - 20, 2), round(fill + 12.5, 2))


def test_follow_open_short_offsets(fp):
    r = attached2(fp).handle(fev("open", "sell", sl=15.0, tp=30.0))
    fill = fp.open_positions()[0].price_open
    assert (r.sl, r.tp) == (round(fill + 15, 2), round(fill - 30, 2))


def test_follow_open_no_target(fp):
    r = attached2(fp).handle(fev("open", "buy", tp=None))
    assert r.status == "ok" and r.tp is None and fp.open_positions()[0].tp == 0.0


def test_follow_open_lots_floored_capped_min(fp):
    assert attached2(fp).handle(fev("open", "buy", lots=0.567, sl=5)).lots == 0.56
    fp2 = FakeMt5(balance=50000.0, tick=Tick(bid=29470.0, ask=29470.5))
    r = attached2(fp2, allow_high_risk=True).handle(fev("open", "buy", lots=50, sl=5))
    assert r.status == "ok" and r.lots == 10.0                       # NDX100 max 10
    fp3 = FakeMt5(balance=50000.0, tick=Tick(bid=29470.0, ask=29470.5))
    r = attached2(fp3).handle(fev("open", "buy", lots=0.001))
    assert r.status == "error" and "below min lot" in r.reason


def test_follow_open_refused_above_5pct_implied(fp):
    # 10 lots x 20 pts x $20 = $4,000 = 8% of 50k
    r = attached2(fp).handle(fev("open", "buy", lots=10, sl=20))
    assert r.status == "error" and "8.00%" in r.reason and fp.requests == []


def test_follow_open_max_risk_refuses_not_resizes(fp):
    # 2 lots x 20 x 20 = $800 = 1.6% > max 1%
    r = attached2(fp, max_risk_pct=1.0).handle(fev("open", "buy", lots=2, sl=20))
    assert r.status == "error" and "1.60%" in r.reason and fp.requests == []


def test_follow_open_ignores_fixed_risk_fields(fp):
    e = fev("open", "buy", lots=0.3)
    e.risk_pct, e.stop_points = 3.0, 5.0
    assert attached2(fp).handle(e).lots == 0.3


def test_follow_stop_wider_than_5pct_refused(fp):
    r = attached2(fp).handle(fev("open", "buy", sl=2000))
    assert r.status == "error" and "5%" in r.reason


def _open_follow(fp, side="buy", **kw):
    w = attached2(fp)
    r = w.handle(fev("open", side, **kw))
    assert r.status == "ok"
    return w, fp.open_positions()[0]


def test_modify_moves_stop(fp):
    w, pos = _open_follow(fp)
    r = w.handle(fev("modify", id_=71, sl=10.0, tp=12.5))
    p = fp.open_positions()[0]
    assert r.status == "ok" and p.sl == round(pos.price_open - 10, 2) and p.tp == round(pos.price_open + 12.5, 2)


def test_modify_breakeven_long(fp):
    w, pos = _open_follow(fp)
    w.handle(fev("modify", id_=72, sl=0.0, tp=12.5))
    assert fp.open_positions()[0].sl == round(pos.price_open, 2)


def test_modify_breakeven_short(fp):
    w, pos = _open_follow(fp, "sell")
    w.handle(fev("modify", id_=73, sl=0.0, tp=12.5))
    assert fp.open_positions()[0].sl == round(pos.price_open, 2)


def test_modify_trail_into_profit(fp):
    w, pos = _open_follow(fp)
    w.handle(fev("modify", id_=74, sl=-5.0, tp=12.5))       # stop 5 points above entry on a long
    assert fp.open_positions()[0].sl == round(pos.price_open + 5, 2)


def test_modify_remove_target(fp):
    w, pos = _open_follow(fp)
    w.handle(fev("modify", id_=75, sl=20.0, tp=None))
    assert fp.open_positions()[0].tp == 0.0


def test_modify_none_stop_keeps_current(fp):
    w, pos = _open_follow(fp)
    before = fp.open_positions()[0].sl
    w.handle(fev("modify", id_=76, sl=None, tp=30.0))
    p = fp.open_positions()[0]
    assert p.sl == before and p.tp == round(pos.price_open + 30, 2)


def test_modify_rejected_warns_keeps_levels(fp):
    w, pos = _open_follow(fp)
    before = (fp.open_positions()[0].sl, fp.open_positions()[0].tp)
    fp.fail_sltp = True
    r = w.handle(fev("modify", id_=77, sl=0.0, tp=12.5))
    assert r.status == "warn" and "modify rejected" in r.reason
    assert (fp.open_positions()[0].sl, fp.open_positions()[0].tp) == before


def test_modify_no_position_is_harmless(fp):
    r = attached2(fp).handle(fev("modify", id_=78, sl=0.0, tp=12.5))
    assert r.status in ("ok", "warn") and fp.requests == []


def test_modify_stop_wider_than_5pct_refused(fp):
    w, pos = _open_follow(fp)
    r = w.handle(fev("modify", id_=79, sl=5000.0, tp=12.5))
    assert r.status == "warn" and "5%" in r.reason


def test_sync_follow_repairs_levels(fp):
    w, pos = _open_follow(fp)
    r = w.handle(fev("sync", "buy", id_=80, sl=0.0, tp=40.0))     # a lost breakeven + target move
    p = fp.open_positions()[0]
    assert r.status == "ok" and p.sl == round(pos.price_open, 2) and p.tp == round(pos.price_open + 40, 2)


def test_sync_follow_no_repair_when_equal(fp):
    w, pos = _open_follow(fp)
    n = len(fp.requests)
    w.handle(fev("sync", "buy", id_=81, sl=20.0, tp=12.5))
    assert len(fp.requests) == n


def test_sync_follow_without_position_warns(fp):
    r = attached2(fp).handle(fev("sync", "buy", id_=82))
    assert r.status == "warn" and "not mirrored" in r.reason and fp.requests == []


def test_attach_refuses_when_server_disallows_expert_trading(api):
    # 2026-11-02 FundedNext 10000003: trade_allowed True but trade_expert False -> every order 10026
    api.trade_expert = False
    reason = AccountWorker(cfg(), api).attach()
    assert reason is not None and "does not allow automated trading" in reason


# --- final review fixes ------------------------------------------------------------------------------

def test_event_refused_after_login_change(api):
    """C1: the terminal is logged into another account after attach: nothing may trade there."""
    w = attached(api)
    api.login = 999999                                   # user switched the terminal
    for e in (ev("open", "buy"), ev("close", id_=2), fev("modify", id_=3), ev("sync", "flat", id_=4)):
        r = w.handle(e)
        assert r.status == "error" and "switched to login 999999" in r.reason
    assert api.requests == []
    assert w.attach_error is not None                    # supervisor stops routing to it


def test_modify_without_bridge_position_warns(fp):
    """I3: a modify with nothing to move must not look green."""
    r = attached2(fp).handle(fev("modify", id_=90, sl=0.0, tp=12.5))
    assert r.status == "warn" and "not mirrored" in r.reason and fp.requests == []
