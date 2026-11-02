"""In-memory stand-in for the MetaTrader5 module, shaped like bridge.mt5_api.Mt5Api."""
from __future__ import annotations

from dataclasses import dataclass, field

from bridge.mt5_api import (ACTION_DEAL, ACTION_SLTP, RET_DONE, TYPE_BUY, AccountInfo, OrderResult,
                            PositionInfo, SymbolListing, TerminalInfo, Tick)
from bridge.sizing import SymbolSpec

RET_INVALID_STOPS = 10016


def _listing(name, desc, tick_value_loss=0.2, has_tick=True, trade_mode=4, volume_max=10.0, digits=2,
             point=0.01, tick_size=0.01, filling_mode=2):
    return SymbolListing(name=name, description=desc, trade_mode=trade_mode, has_tick=has_tick, digits=digits,
                         point=point, tick_size=tick_size, tick_value_loss=tick_value_loss, volume_min=0.01,
                         volume_max=volume_max, volume_step=0.01, stops_level=0, filling_mode=filling_mode)


# The FundingPips-Trial symbol set as probed on 2026-10-29 (descriptions verbatim).
FUNDINGPIPS_LISTINGS = [
    _listing("NDX100", "US Tech 100 Cash Index", 0.2),
    _listing("NQM26", "Nasdaq 100 Index Futures - June 2026", 0.2, has_tick=False, volume_max=5.0),
    _listing("SPX500", "US SPX 500 Cash Index", 0.5),
    _listing("ESM26", "SPX 500 Index Futures - June 2026", 0.5, has_tick=False, volume_max=5.0),
    _listing("XAUUSD", "Gold Spot (1 Lot = 100 oz)", 0.01, volume_max=5.0, filling_mode=7),
    _listing("GCM26", "Gold Futures - June 2026", 1.0, has_tick=False, volume_max=5.0),
    _listing("USOIL", "Crude Oil West Texas - Spot", 0.001, volume_max=1.0, digits=3, point=0.001, tick_size=0.001),
    _listing("UKOIL", "Crude Oil Brent - Spot", 0.001, volume_max=1.0, digits=3, point=0.001, tick_size=0.001),
    _listing("CLK26", "WTI Futures - May 2026", 1.0, has_tick=False, trade_mode=3, volume_max=1.0),
    _listing("DJI30", "US Wall Street 30 Cash Index", 0.05),
    _listing("EURUSD", "Euro vs US Dollar", 1.0, digits=5, point=0.00001, tick_size=0.00001, volume_max=20.0),
]


@dataclass
class FakeMt5:
    login: int = 10000001
    balance: float = 100000.0
    equity: float = 99500.0
    specs: dict[str, SymbolSpec] = field(default_factory=dict)
    tick: Tick = Tick(bid=20120.0, ask=20120.5)
    trade_allowed: bool = True
    trade_expert: bool = True
    terminal_trade_allowed: bool = True
    init_ok: bool = True
    drop_sltp_on_open: bool = False   # broker ignores sl/tp on the opening deal
    fail_sltp: bool = False           # TRADE_ACTION_SLTP is rejected
    retcodes: list[int] = field(default_factory=list)  # queued retcodes returned before DONE
    fill_offset: float = 0.0          # fill = ask + offset (buy) / bid - offset (sell)
    positions_none: bool = False      # positions_get() fails (returns None) like a disconnected terminal
    listings: list[SymbolListing] = field(default_factory=lambda: list(FUNDINGPIPS_LISTINGS))
    listings_none: bool = False       # symbols_get() fails
    report_price_zero: bool = False   # broker answers the DEAL with price=0 (exchange execution style)
    sltp_no_changes: bool = False     # TRADE_ACTION_SLTP answers 10025 NO_CHANGES
    requests: list[dict] = field(default_factory=list)
    _positions: dict[int, PositionInfo] = field(default_factory=dict)
    _next_ticket: int = 1000

    # --- Mt5Api ---
    def initialize(self, path: str) -> bool:
        return self.init_ok

    def last_error(self):
        return (-1, "fake init failure") if not self.init_ok else (1, "ok")

    def shutdown(self):
        pass

    def account_info(self):
        return AccountInfo(login=self.login, balance=self.balance, equity=self.equity, currency="USD",
                           trade_allowed=self.trade_allowed, server="FundingPips-Trial",
                           trade_expert=self.trade_expert)

    def terminal_info(self):
        return TerminalInfo(connected=True, trade_allowed=self.terminal_trade_allowed)

    def _listed(self, name: str):
        return next((l for l in self.listings if l.name == name), None)

    def symbol_select(self, name: str, enable: bool) -> bool:
        return name in self.specs or self._listed(name) is not None

    def symbol_info(self, name: str):
        if name in self.specs:
            return self.specs[name]
        listed = self._listed(name)
        return listed.spec() if listed else None

    def symbols_all(self):
        return None if self.listings_none else list(self.listings)

    def symbol_tick(self, name: str):
        return self.tick

    def positions(self, symbol: str):
        if self.positions_none:
            return None
        return [p for p in self._positions.values() if p.symbol == symbol]

    def order_send(self, request: dict) -> OrderResult:
        self.requests.append(dict(request))
        if self.retcodes:
            rc = self.retcodes.pop(0)
            return OrderResult(retcode=rc, deal=0, order=0, volume=0.0, price=0.0, comment=f"fake rc {rc}")
        action = request["action"]
        if action == ACTION_SLTP:
            if self.fail_sltp:
                return OrderResult(retcode=RET_INVALID_STOPS, deal=0, order=0, volume=0.0, price=0.0,
                                   comment="Invalid stops")
            if self.sltp_no_changes:
                return OrderResult(retcode=10025, deal=0, order=0, volume=0.0, price=0.0, comment="No changes")
            p = self._positions[request["position"]]
            self._positions[p.ticket] = PositionInfo(p.ticket, p.symbol, p.magic, p.type, p.volume, p.price_open,
                                                     request.get("sl", 0.0), request.get("tp", 0.0), p.comment)
            return OrderResult(retcode=RET_DONE, deal=0, order=0, volume=p.volume, price=p.price_open, comment="ok")
        if action == ACTION_DEAL and "position" in request:
            p = self._positions.pop(request["position"])
            return OrderResult(retcode=RET_DONE, deal=p.ticket + 1, order=p.ticket + 2, volume=p.volume,
                               price=self.tick.bid if p.type == TYPE_BUY else self.tick.ask, comment="closed")
        if action == ACTION_DEAL:
            is_buy = request["type"] == TYPE_BUY
            fill = self.tick.ask + self.fill_offset if is_buy else self.tick.bid - self.fill_offset
            ticket = self._next_ticket
            self._next_ticket += 1
            sl = 0.0 if self.drop_sltp_on_open else request.get("sl", 0.0)
            tp = 0.0 if self.drop_sltp_on_open else request.get("tp", 0.0)
            self._positions[ticket] = PositionInfo(ticket, request["symbol"], request["magic"], request["type"],
                                                   request["volume"], fill, sl, tp, request.get("comment", ""))
            return OrderResult(retcode=RET_DONE, deal=ticket + 1, order=ticket, volume=request["volume"],
                               price=0.0 if self.report_price_zero else fill, comment="done")
        raise AssertionError(f"unexpected request {request}")

    # --- test helpers ---
    def add_position(self, symbol: str, magic: int, type_: int = TYPE_BUY, volume: float = 1.0) -> int:
        ticket = self._next_ticket
        self._next_ticket += 1
        self._positions[ticket] = PositionInfo(ticket, symbol, magic, type_, volume, self.tick.ask, 0.0, 0.0, "")
        return ticket

    def remove_position(self, ticket: int):
        self._positions.pop(ticket)

    def open_positions(self):
        return list(self._positions.values())
