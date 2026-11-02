"""The subset of the MetaTrader5 module the bridge uses, behind a small interface.

RealMt5Api is the only place that imports MetaTrader5, so everything else is testable
with tests/fake_mt5.FakeMt5.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from bridge.sizing import SymbolSpec

# MetaTrader5 constants (values from the MetaTrader5 Python package docs)
RET_DONE = 10009
RET_PLACED = 10008
RETRYABLE = {10004, 10020, 10021}          # requote, price changed, price off
ACTION_DEAL = 1                            # TRADE_ACTION_DEAL
ACTION_SLTP = 6                            # TRADE_ACTION_SLTP
TYPE_BUY = 0                               # ORDER_TYPE_BUY
TYPE_SELL = 1                              # ORDER_TYPE_SELL
FILLING_FOK = 0                            # ORDER_FILLING_FOK
FILLING_IOC = 1                            # ORDER_FILLING_IOC
FILLING_RETURN = 2                         # ORDER_FILLING_RETURN
TIME_GTC = 0                               # ORDER_TIME_GTC
SYMBOL_FILLING_FOK = 1                     # symbol_info.filling_mode bits
SYMBOL_FILLING_IOC = 2


@dataclass(frozen=True)
class AccountInfo:
    login: int
    balance: float
    equity: float
    currency: str
    trade_allowed: bool
    server: str
    company: str = ""
    trade_expert: bool = True      # False = the server forbids Expert Advisor trading on this account


@dataclass(frozen=True)
class TerminalInfo:
    connected: bool
    trade_allowed: bool


@dataclass(frozen=True)
class Tick:
    bid: float
    ask: float


@dataclass(frozen=True)
class PositionInfo:
    ticket: int
    symbol: str
    magic: int
    type: int          # TYPE_BUY / TYPE_SELL
    volume: float
    price_open: float
    sl: float
    tp: float
    comment: str


@dataclass(frozen=True)
class SymbolListing:
    """One row of the terminal's symbol list, enough for discovery and sizing."""
    name: str
    description: str
    trade_mode: int          # 4 = SYMBOL_TRADE_MODE_FULL
    has_tick: bool           # a live tick exists (symbol is quoted now)
    digits: int
    point: float
    tick_size: float
    tick_value_loss: float
    volume_min: float
    volume_max: float
    volume_step: float
    stops_level: int
    filling_mode: int

    def spec(self) -> SymbolSpec:
        return SymbolSpec(name=self.name, digits=self.digits, point=self.point, tick_size=self.tick_size,
                          tick_value_loss=self.tick_value_loss, volume_min=self.volume_min,
                          volume_max=self.volume_max, volume_step=self.volume_step,
                          stops_level=self.stops_level, filling_mode=self.filling_mode)


TRADE_MODE_FULL = 4


@dataclass(frozen=True)
class OrderResult:
    retcode: int
    deal: int
    order: int
    volume: float
    price: float
    comment: str


class Mt5Api(Protocol):
    def initialize(self, path: str) -> bool: ...
    def last_error(self) -> tuple[int, str]: ...
    def shutdown(self) -> None: ...
    def account_info(self) -> AccountInfo | None: ...
    def terminal_info(self) -> TerminalInfo | None: ...
    def symbol_select(self, name: str, enable: bool) -> bool: ...
    def symbol_info(self, name: str) -> SymbolSpec | None: ...
    def symbol_tick(self, name: str) -> Tick | None: ...
    def positions(self, symbol: str) -> list[PositionInfo] | None: ...
    def order_send(self, request: dict) -> OrderResult | None: ...
    def symbols_all(self) -> list[SymbolListing] | None: ...


class RealMt5Api:
    """1:1 wrapper over the MetaTrader5 module."""

    def __init__(self) -> None:
        import MetaTrader5 as mt5  # imported lazily: only workers need it
        self._mt5 = mt5

    def initialize(self, path: str) -> bool:
        return bool(self._mt5.initialize(path=path))

    def last_error(self) -> tuple[int, str]:
        code, msg = self._mt5.last_error()
        return int(code), str(msg)

    def shutdown(self) -> None:
        self._mt5.shutdown()

    def account_info(self) -> AccountInfo | None:
        a = self._mt5.account_info()
        if a is None:
            return None
        return AccountInfo(login=int(a.login), balance=float(a.balance), equity=float(a.equity),
                           currency=str(a.currency), trade_allowed=bool(a.trade_allowed), server=str(a.server),
                           company=str(getattr(a, "company", "") or ""),
                           trade_expert=bool(getattr(a, "trade_expert", True)))

    def terminal_info(self) -> TerminalInfo | None:
        t = self._mt5.terminal_info()
        if t is None:
            return None
        return TerminalInfo(connected=bool(t.connected), trade_allowed=bool(t.trade_allowed))

    def symbol_select(self, name: str, enable: bool) -> bool:
        return bool(self._mt5.symbol_select(name, enable))

    def symbol_info(self, name: str) -> SymbolSpec | None:
        s = self._mt5.symbol_info(name)
        if s is None:
            return None
        return SymbolSpec(name=str(s.name), digits=int(s.digits), point=float(s.point),
                          tick_size=float(s.trade_tick_size), tick_value_loss=float(s.trade_tick_value_loss),
                          volume_min=float(s.volume_min), volume_max=float(s.volume_max),
                          volume_step=float(s.volume_step), stops_level=int(s.trade_stops_level),
                          filling_mode=int(s.filling_mode))

    def symbol_tick(self, name: str) -> Tick | None:
        t = self._mt5.symbol_info_tick(name)
        if t is None:
            return None
        return Tick(bid=float(t.bid), ask=float(t.ask))

    def positions(self, symbol: str) -> list[PositionInfo] | None:
        """None when the terminal cannot answer (disconnected, IPC error): never confuse with 'flat'."""
        rows = self._mt5.positions_get(symbol=symbol)
        if rows is None:
            return None
        return [PositionInfo(ticket=int(p.ticket), symbol=str(p.symbol), magic=int(p.magic), type=int(p.type),
                             volume=float(p.volume), price_open=float(p.price_open), sl=float(p.sl),
                             tp=float(p.tp), comment=str(p.comment)) for p in rows]

    def order_send(self, request: dict) -> OrderResult | None:
        r = self._mt5.order_send(request)
        if r is None:
            return None
        return OrderResult(retcode=int(r.retcode), deal=int(r.deal), order=int(r.order),
                           volume=float(r.volume), price=float(r.price), comment=str(r.comment))

    def symbols_all(self) -> list[SymbolListing] | None:
        rows = self._mt5.symbols_get()
        if rows is None:
            return None
        out = []
        for s in rows:
            tick = self._mt5.symbol_info_tick(s.name)
            out.append(SymbolListing(
                name=str(s.name), description=str(s.description or ""), trade_mode=int(s.trade_mode),
                has_tick=bool(tick is not None and tick.time), digits=int(s.digits), point=float(s.point),
                tick_size=float(s.trade_tick_size), tick_value_loss=float(s.trade_tick_value_loss),
                volume_min=float(s.volume_min), volume_max=float(s.volume_max), volume_step=float(s.volume_step),
                stops_level=int(s.trade_stops_level), filling_mode=int(s.filling_mode)))
        return out
