import json

import pytest

from bridge.mt5_api import SymbolListing
from bridge.symbols import Resolution, discover, load_firms, resolve
from tests.fake_mt5 import FUNDINGPIPS_LISTINGS, FakeMt5


def L(name, desc, mode=4, tick=True, **kw):
    base = dict(digits=2, point=0.01, tick_size=0.01, tick_value_loss=0.2, volume_min=0.01, volume_max=10.0,
                volume_step=0.01, stops_level=0, filling_mode=2)
    base.update(kw)
    return SymbolListing(name=name, description=desc, trade_mode=mode, has_tick=tick, **base)


@pytest.fixture
def firms():
    return load_firms(None)


@pytest.fixture
def api():
    return FakeMt5()  # ships with FUNDINGPIPS_LISTINGS and matching specs


# --- firm table -----------------------------------------------------------------

def test_shipped_table_has_the_six_firms_and_other(firms):
    assert {"FundingPips", "FTMO", "The5ers", "FundedNext", "GoatFunded", "TradingPit", "Other"} <= set(firms.names())


def test_firm_for_server_prefix(firms):
    assert firms.firm_for_server("FundingPips-Trial") == "FundingPips"
    assert firms.firm_for_server("FTMO-Demo") == "FTMO"
    assert firms.firm_for_server("Exness-MT5Trial") is None


def test_micro_root_maps_to_full(firms):
    assert firms.canonical_root("MNQ") == "NQ" and firms.canonical_root("NQ") == "NQ"
    assert firms.canonical_root("MGC") == "GC" and firms.canonical_root("ES") == "ES"


def test_user_firms_overlay(tmp_path, firms):
    (tmp_path / "firms.json").write_text(json.dumps({"firms": {"MyFirm": {"server_prefixes": ["My"],
                                                     "symbols": {"NQ": {"name": "NAS", "verified": True}}}}}))
    f = load_firms(str(tmp_path))
    assert "MyFirm" in f.names() and f.firm_for_server("My-Live") == "MyFirm"
    assert f.firm_for_server("FundingPips-Trial") == "FundingPips"  # shipped entries kept


# --- resolution order ------------------------------------------------------------

def test_override_wins_when_present_on_server(api, firms):
    r = resolve(api, firms, "FundingPips", "NQ", "SPX500")
    assert r == Resolution(symbol="SPX500", verified=True, source="override", reason=r.reason)


def test_override_missing_on_server_is_error_not_fallback(api, firms):
    r = resolve(api, firms, "FundingPips", "NQ", "US100.cash")
    assert r.symbol is None and r.source == "none" and "US100.cash" in r.reason


def test_table_entry_verified(api, firms):
    r = resolve(api, firms, "FundingPips", "NQ", None)
    assert (r.symbol, r.verified, r.source) == ("NDX100", True, "table")
    assert resolve(api, firms, "FundingPips", "GC", None).symbol == "XAUUSD"
    assert resolve(api, firms, "FundingPips", "CL", None).symbol == "USOIL"
    assert resolve(api, firms, "FundingPips", "ES", None).symbol == "SPX500"


def test_table_entry_missing_on_server_falls_to_discovery(api, firms):
    r = resolve(api, firms, "FTMO", "NQ", None)   # table says US100.cash; server (FundingPips set) lacks it
    assert (r.symbol, r.verified, r.source) == ("NDX100", False, "discovery")


def test_other_firm_uses_discovery_only(api, firms):
    r = resolve(api, firms, "Other", "NQ", None)
    assert (r.symbol, r.verified, r.source) == ("NDX100", False, "discovery")


def test_unknown_firm_treated_as_other(api, firms):
    assert resolve(api, firms, None, "ES", None).symbol == "SPX500"


def test_unknown_root_is_none(api, firms):
    r = resolve(api, firms, "FundingPips", "ZB", None)
    assert r.symbol is None and r.source == "none"


def test_micro_resolves_like_full(api, firms):
    assert resolve(api, firms, "FundingPips", "MNQ", None).symbol == "NDX100"


# --- discovery --------------------------------------------------------------------

def test_discovery_skips_dated_futures(firms):
    listings = [L("NQM26", "Nasdaq 100 Index Futures - June 2026", tick=False), L("NDX100", "US Tech 100 Cash Index")]
    assert discover(listings, firms, "NQ").name == "NDX100"
    assert discover([L("NQM26", "Nasdaq 100 Index Futures - June 2026")], firms, "NQ") is None


def test_discovery_excludes_brent_for_cl(firms):
    listings = [L("UKOIL", "Crude Oil Brent - Spot"), L("USOIL", "Crude Oil West Texas - Spot")]
    assert discover(listings, firms, "CL").name == "USOIL"


def test_discovery_prefers_live_tick_then_shortest_name(firms):
    listings = [L("US100.cash", "US Tech 100", tick=False), L("USTEC", "US Tech 100", tick=True),
                L("NAS100m", "US Tech 100", tick=True)]
    assert discover(listings, firms, "NQ").name == "USTEC"
    listings = [L("US100.cash", "US Tech 100", tick=True), L("NAS100", "US Tech 100", tick=True)]
    assert discover(listings, firms, "NQ").name == "NAS100"


def test_discovery_requires_full_trade_mode(firms):
    assert discover([L("NDX100", "US Tech 100 Cash Index", mode=0)], firms, "NQ") is None


def test_discovery_matches_by_name_and_description(firms):
    assert discover([L("GER40", "Germany 40 Cash Index"), L("XAUUSD", "Gold Spot")], firms, "GC").name == "XAUUSD"
    assert discover([L("US500", "US SPX 500 Cash Index")], firms, "ES").name == "US500"


def test_fundingpips_listing_fixture_discovers_all_four(firms):
    for root, want in (("NQ", "NDX100"), ("ES", "SPX500"), ("GC", "XAUUSD"), ("CL", "USOIL")):
        assert discover(FUNDINGPIPS_LISTINGS, firms, root).name == want


def test_symbols_all_none_gives_none_resolution(api, firms):
    api.listings_none = True
    r = resolve(api, firms, "Other", "NQ", None)
    assert r.symbol is None and "symbol list unavailable" in r.reason


def test_fundednext_table_verified_2026_11_02(firms):
    # read from a live FundedNext-Server 3 terminal on 2026-11-02
    want = {"NQ": "NDX100", "ES": "SPX500", "GC": "XAUUSD", "CL": "USOUSD"}
    for root, name in want.items():
        entry = firms.table_entry("FundedNext", root)
        assert entry == {"name": name, "verified": True}


def test_ftmo_us100_cash_verified_by_live_order(firms):
    # a real FTMO-Demo order executed on US100.cash on 2026-11-02
    assert firms.table_entry("FTMO", "NQ") == {"name": "US100.cash", "verified": True}


def test_known_firm_names_need_no_confirmation():
    """NQ/ES/GC on FundingPips, FundedNext and FTMO are known: trade without a tray confirmation."""
    firms = load_firms(None)
    for firm in ("FundingPips", "FundedNext", "FTMO"):
        for root in ("NQ", "ES", "GC"):
            entry = firms.table_entry(firm, root)
            assert entry and entry["verified"] is True, (firm, root)
