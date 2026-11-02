"""AccountWorker: one MT5 account, attached to one terminal, driven by bridge events.

MT5 is the source of truth: "what the bridge owns" is always a live positions query
filtered on the account's magic number. Phase 2: symbols are resolved per account
(override -> firm table -> discovery), risk comes from the event (chart) with account
defaults/caps, and every sync carries a sizing preview.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from bridge.config import MAX_RISK_PCT, AccountConfig
from bridge.mt5_api import (ACTION_DEAL, ACTION_SLTP, FILLING_FOK, FILLING_IOC, FILLING_RETURN, RET_DONE,
                            RET_PLACED, RETRYABLE, SYMBOL_FILLING_FOK, SYMBOL_FILLING_IOC, TIME_GTC, TYPE_BUY,
                            TYPE_SELL, Mt5Api, OrderResult, PositionInfo)
from bridge.protocol import AccountResult, Event, Preview
from bridge.sizing import (SymbolSpec, bracket_prices, check_stops_level, compute_lots, floor_lots,
                           value_per_point_per_lot)
from bridge.symbols import Firms, Resolution, load_firms, resolve

log = logging.getLogger("bridge.worker")

MAX_ATTEMPTS = 3
RET_NO_CHANGES = 10025      # SLTP modify with identical levels: already what we want
PRICE_SCALE_TOLERANCE = 0.03
MAX_STOP_FRACTION = 0.05


class PositionsUnavailable(Exception):
    """positions_get() returned None: the terminal cannot answer right now."""


class Refused(Exception):
    """A guard refused the event; the message is the ack reason."""


@dataclass(frozen=True)
class RiskParams:
    risk_pct: float
    stop_points: float
    tp_points: float
    risk_base: str


@dataclass
class Resolved:
    root: str
    symbol: str
    spec: SymbolSpec
    verified: bool
    source: str
    reason: str


class AccountWorker:
    def __init__(self, cfg: AccountConfig, api: Mt5Api, firms: Firms | None = None,
                 confirmed: set[tuple[str, str]] | None = None) -> None:
        self.cfg = cfg
        self.api = api
        self.firms = firms or load_firms(None)
        self.confirmed = confirmed if confirmed is not None else set()   # (root, symbol) pairs acknowledged by the user
        self.specs: dict[str, SymbolSpec] = {}          # pre-mapped root -> spec (config symbols)
        self._resolved: dict[tuple[str, str | None], Resolved] = {}   # (root, override) -> resolution cache
        self.attach_error: str | None = "not attached"

    # ------------------------------------------------------------------ attach
    def attach(self) -> str | None:
        """Attach to the running terminal and verify it is the configured account. Returns a reason on failure."""
        self.attach_error = self._attach()
        if self.attach_error:
            log.error("%s: attach failed: %s", self.cfg.name, self.attach_error)
        else:
            log.info("%s: attached (login %s, firm %s)", self.cfg.name, self.cfg.login, self.cfg.firm or "-")
        return self.attach_error

    def _attach(self) -> str | None:
        if not self.api.initialize(self.cfg.terminal):
            return f"initialize failed: {self.api.last_error()}"
        acc = self.api.account_info()
        if acc is None:
            return "account_info unavailable (terminal not logged in?)"
        if acc.login != self.cfg.login:
            return f"login mismatch: terminal has {acc.login}, config says {self.cfg.login}"
        term = self.api.terminal_info()
        if term is None or not term.trade_allowed:
            return "terminal: Algo Trading is disabled"
        if not acc.trade_allowed:
            return "account: trading not allowed"
        if not acc.trade_expert:
            return ("the broker does not allow automated trading on this account (Expert Advisors disabled by "
                    "the server); ask the prop firm, or use an account type that allows EAs")
        self.specs.clear()
        self._resolved.clear()
        for alias, broker in self.cfg.symbols.items():
            if not self.api.symbol_select(broker, True):
                return f"symbol {broker} (alias {alias}) not found on {acc.server}"
            spec = self.api.symbol_info(broker)
            if spec is None:
                return f"symbol {broker} (alias {alias}) has no info"
            self.specs[self.firms.canonical_root(alias)] = spec
            self._log_spec(alias, spec)
        return None

    def _log_spec(self, alias: str, spec: SymbolSpec) -> None:
        log.info("%s: %s -> %s digits=%s point=%s tick_size=%s tick_value_loss=%s vol=[%s..%s step %s] "
                 "stops_level=%s filling=%s", self.cfg.name, alias, spec.name, spec.digits, spec.point,
                 spec.tick_size, spec.tick_value_loss, spec.volume_min, spec.volume_max, spec.volume_step,
                 spec.stops_level, spec.filling_mode)

    # ------------------------------------------------------------------ dispatch
    def handle(self, event: Event) -> AccountResult:
        if self.attach_error:
            return self._err(f"not attached: {self.attach_error}")
        if event.type == "ping":
            return self._ok()
        acc = self.api.account_info()
        if acc is not None and acc.login != self.cfg.login:
            # The MT5 session belongs to the TERMINAL: after a login switch order_send would trade the new account.
            self.attach_error = f"terminal switched to login {acc.login}; refresh accounts"
            log.error("%s: %s; refusing event %s", self.cfg.name, self.attach_error, event.id)
            return self._err(self.attach_error)
        try:
            if event.type == "open":
                return self._open(event)
            if event.type == "close":
                return self._close(event)
            if event.type == "sync":
                return self._sync(event)
            if event.type == "modify":
                return self._modify(event)
        except PositionsUnavailable as exc:
            log.error("%s: event %s: %s", self.cfg.name, event.id, exc)
            return self._err(f"positions unavailable: {exc}")
        except Refused as exc:
            log.warning("%s: event %s refused: %s", self.cfg.name, event.id, exc)
            return self._err(str(exc))
        except Exception as exc:  # noqa: BLE001 - every failure must become a visible ack
            log.exception("%s: event %s failed", self.cfg.name, event.id)
            return self._err(f"exception: {exc}")
        return self._err(f"unknown event type {event.type}")

    # ------------------------------------------------------------------ helpers
    def _ok(self, **kw) -> AccountResult:
        return AccountResult(account=self.cfg.name, status="ok", **kw)

    def _warn(self, reason: str, **kw) -> AccountResult:
        return AccountResult(account=self.cfg.name, status="warn", reason=reason, **kw)

    def _err(self, reason: str, **kw) -> AccountResult:
        return AccountResult(account=self.cfg.name, status="error", reason=reason, **kw)

    def _root(self, event: Event) -> str:
        return self.firms.canonical_root(event.symbol)

    def _resolve(self, event: Event) -> Resolved:
        """Broker symbol for this event's root: config pre-map > override > firm table > discovery."""
        root = self._root(event)
        override = event.symbol_override or None
        key = (root, override)
        cached = self._resolved.get(key)
        if cached is not None:
            return cached
        if override is None and root in self.specs:
            spec = self.specs[root]
            res = Resolved(root, spec.name, spec, True, "config", f"config maps {root} -> {spec.name}")
        else:
            firm = self.cfg.firm or event.firm
            r: Resolution = resolve(self.api, self.firms, firm, root, override)
            log.info("%s: resolve %s @ %s override=%s -> %s (%s): %s", self.cfg.name, root, firm, override,
                     r.symbol, r.source, r.reason)
            if r.symbol is None:
                raise Refused(r.reason)
            spec = self.api.symbol_info(r.symbol)
            if spec is None:
                raise Refused(f"symbol {r.symbol} has no info on this server")
            self._log_spec(root, spec)
            res = Resolved(root, r.symbol, spec, r.verified, r.source, r.reason)
        self._resolved[key] = res
        return res

    def _check_confirmed(self, res: Resolved) -> None:
        if not res.verified and (res.root, res.symbol) not in self.confirmed:
            raise Refused(f"confirm symbol {res.symbol} for {res.root} in the tray before the first trade")

    def _risk(self, event: Event) -> RiskParams:
        risk_pct = event.risk_pct if event.risk_pct is not None else self.cfg.risk_pct
        if self.cfg.max_risk_pct is not None:
            risk_pct = min(risk_pct, self.cfg.max_risk_pct)
        if risk_pct > MAX_RISK_PCT and not self.cfg.allow_high_risk:
            raise Refused(f"risk_pct {risk_pct:g} above the {MAX_RISK_PCT:g}% cap (account must allow high risk)")
        if risk_pct <= 0:
            raise Refused("risk_pct must be > 0")
        stop = event.stop_points if event.stop_points is not None else self.cfg.stop_points
        tp = event.tp_points if event.tp_points is not None else self.cfg.tp_points
        base = event.risk_base or self.cfg.risk_base
        if stop <= 0:
            raise Refused("stop_points must be > 0")
        return RiskParams(risk_pct=risk_pct, stop_points=stop, tp_points=max(tp, 0.0), risk_base=base)

    @staticmethod
    def _price_guard(event: Event, res: Resolved, bid: float) -> None:
        if event.chart_price and bid > 0:
            gap = abs(event.chart_price - bid) / bid
            if gap > PRICE_SCALE_TOLERANCE:
                raise Refused(f"price scale mismatch (chart {event.chart_price:g} vs broker {res.symbol} {bid:g}, "
                              f"{gap:.1%} apart)")

    def _follow_plan(self, event: Event, res: Resolved, bid: float):
        """Follow-ATAS open: (lots, levels(fill) -> (sl, tp), note). Raises Refused on any guard."""
        f = event.follow
        if f is None or f.sl_offset is None:
            raise Refused("Follow ATAS: no stop in ATAS, trade not mirrored")
        self._price_guard(event, res, bid)
        if bid > 0 and abs(f.sl_offset) > MAX_STOP_FRACTION * bid:
            raise Refused(f"stop offset {f.sl_offset:g} wider than 5% of price ({bid:g}); mis-set?")
        if f.sl_offset > 0:
            reason = check_stops_level(res.spec, f.sl_offset, f.tp_offset or 0.0)
            if reason:
                raise Refused(reason)
        lots, capped = floor_lots(res.spec, f.lots)
        if lots <= 0:
            raise Refused(f"below min lot ({f.lots:g} < {res.spec.volume_min:g} on {res.symbol})")
        acc = self.api.account_info()
        if acc is None:
            raise Refused("account_info unavailable")
        base = acc.equity if (event.risk_base or self.cfg.risk_base) == "equity" else acc.balance
        implied = lots * max(f.sl_offset, 0.0) * value_per_point_per_lot(res.spec)
        pct = implied / base * 100.0 if base > 0 else 0.0
        if self.cfg.max_risk_pct is not None and pct > self.cfg.max_risk_pct:
            raise Refused(f"implied risk {pct:.2f}% (${implied:,.0f}) above the account max {self.cfg.max_risk_pct:g}%")
        if pct > MAX_RISK_PCT and not self.cfg.allow_high_risk:
            raise Refused(f"implied risk {pct:.2f}% (${implied:,.0f}) above the {MAX_RISK_PCT:g}% cap; "
                          f"lots typo? (account must allow high risk)")
        side = 1 if event.dir == "buy" else -1
        digits = res.spec.digits

        def levels(fill: float) -> tuple[float, float | None]:
            sl = round(fill - side * f.sl_offset, digits)
            tp = round(fill + side * f.tp_offset, digits) if f.tp_offset is not None else None
            return sl, tp

        note = f"capped at broker max {res.spec.volume_max:g} lots" if capped else None
        return lots, levels, note

    def _guards(self, event: Event, res: Resolved, risk: RiskParams, bid: float) -> None:
        self._price_guard(event, res, bid)
        if bid > 0 and risk.stop_points > MAX_STOP_FRACTION * bid:
            raise Refused(f"stop {risk.stop_points:g} wider than 5% of price ({bid:g}); mis-set?")
        reason = check_stops_level(res.spec, risk.stop_points, risk.tp_points)
        if reason:
            raise Refused(reason)

    def _risk_money(self, risk: RiskParams) -> tuple[float, float]:
        acc = self.api.account_info()
        if acc is None:
            raise Refused("account_info unavailable")
        base = acc.balance if risk.risk_base == "balance" else acc.equity
        return base, base * risk.risk_pct / 100.0

    def _bridge_positions(self, symbol: str) -> list[PositionInfo]:
        rows = self.api.positions(symbol)
        if rows is None:
            raise PositionsUnavailable(f"positions_get failed: {self.api.last_error()}")
        return [p for p in rows if p.magic == self.cfg.magic]

    @staticmethod
    def _side_of(pos: PositionInfo) -> str:
        return "buy" if pos.type == TYPE_BUY else "sell"

    def _filling(self, spec: SymbolSpec) -> int:
        if spec.filling_mode & SYMBOL_FILLING_FOK:
            return FILLING_FOK
        if spec.filling_mode & SYMBOL_FILLING_IOC:
            return FILLING_IOC
        return FILLING_RETURN

    def _deviation(self, spec: SymbolSpec) -> int:
        return int(round(self.cfg.slippage_points / spec.point))

    def _send_with_retry(self, build_request) -> tuple[OrderResult | None, str | None]:
        """build_request() -> dict, re-evaluated each attempt (fresh tick). Returns (result, error)."""
        last = "no attempt"
        for attempt in range(1, MAX_ATTEMPTS + 1):
            req = build_request()
            res = self.api.order_send(req)
            if res is None:
                last = f"order_send returned None: {self.api.last_error()}"
                log.warning("%s: attempt %d: %s", self.cfg.name, attempt, last)
                continue
            log.info("%s: attempt %d action=%s %s retcode=%s price=%s order=%s comment=%r", self.cfg.name,
                     attempt, req.get("action"), req.get("symbol"), res.retcode, res.price, res.order, res.comment)
            if res.retcode in (RET_DONE, RET_PLACED):
                return res, None
            last = f"retcode {res.retcode} {res.comment}"
            if res.retcode not in RETRYABLE:
                break
        return None, last

    # ------------------------------------------------------------------ open
    def _open(self, event: Event) -> AccountResult:
        res = self._resolve(event)
        self._check_confirmed(res)
        spec, broker = res.spec, res.symbol
        tick = self.api.symbol_tick(broker)
        if tick is None:
            raise Refused(f"no quote for {broker}")
        follow = event.mode == "follow"
        if follow:
            lots, levels, plan_note = self._follow_plan(event, res, tick.bid)
        else:
            risk = self._risk(event)
            self._guards(event, res, risk, tick.bid)

            def levels(fill: float) -> tuple[float, float | None]:
                return bracket_prices(spec, event.dir, fill, risk.stop_points, risk.tp_points)
        closed_opposite: list[int] = []
        existing = self._bridge_positions(broker)
        if any(self._side_of(p) == event.dir for p in existing):
            return self._warn("already mirrored")
        for pos in existing:  # ATAS is the master: an opposite-side CFD must go before the new open
            ok, err = self._close_position(pos, event.id)
            if not ok:
                return self._err(f"direction mismatch: could not close opposite #{pos.ticket}: {err}")
            closed_opposite.append(pos.ticket)
        if not follow:
            _, risk_money = self._risk_money(risk)
            lots = compute_lots(spec, risk_money, risk.stop_points)
            if lots <= 0:
                return self._err(f"below min lot (risk {risk_money:.2f} over {risk.stop_points:g} pts on {broker})")
        is_buy = event.dir == "buy"
        order_type = TYPE_BUY if is_buy else TYPE_SELL

        def build() -> dict:
            t = self.api.symbol_tick(broker)
            guess = t.ask if is_buy else t.bid
            sl, tp = levels(guess)
            return {"action": ACTION_DEAL, "symbol": broker, "volume": lots, "type": order_type, "price": guess,
                    "sl": sl, "tp": tp if tp is not None else 0.0, "deviation": self._deviation(spec),
                    "magic": self.cfg.magic, "comment": f"atas-{event.id}", "type_time": TIME_GTC,
                    "type_filling": self._filling(spec)}

        result, err = self._send_with_retry(build)
        if result is None:
            return self._err(f"open failed: {err}")
        pos = self._find_position(broker, result.order)
        if pos is None:
            return self._err(f"open filled (order {result.order}) but position not found; check the terminal")
        # The position's own open price is the fill of record; result.price can be 0 on some servers.
        fill = pos.price_open if pos.price_open > 0 else result.price
        if fill <= 0:
            closed, cerr = self._close_position(pos, event.id)
            return self._err(f"bracket failed: no fill price reported{'' if closed else '; AND close failed: ' + str(cerr)}",
                             ticket=pos.ticket)
        sl, tp = levels(fill)
        want_tp = tp if tp is not None else 0.0
        tol = (10 ** -spec.digits) / 2
        if abs(pos.sl - sl) > tol or abs(pos.tp - want_tp) > tol:
            mod = self.api.order_send({"action": ACTION_SLTP, "symbol": broker, "position": pos.ticket,
                                       "sl": sl, "tp": want_tp, "magic": self.cfg.magic})
            log.info("%s: SLTP modify #%s sl=%s tp=%s -> %s", self.cfg.name, pos.ticket, sl, want_tp,
                     "None" if mod is None else f"{mod.retcode} {mod.comment!r}")
            if mod is None or mod.retcode not in (RET_DONE, RET_PLACED, RET_NO_CHANGES):
                why = "None" if mod is None else f"{mod.retcode} {mod.comment}"
                log.error("%s: SLTP modify failed (%s); closing %s", self.cfg.name, why, pos.ticket)
                closed, cerr = self._close_position(pos, event.id)
                extra = "" if closed else f"; AND close failed: {cerr}"
                return self._err(f"bracket failed: {why}{extra}", ticket=pos.ticket)
        note = plan_note if follow else None
        return self._ok(ticket=pos.ticket, lots=lots, sl=sl, tp=tp, closed=closed_opposite or None, reason=note)

    # ------------------------------------------------------------------ follow: modify / repair
    def _apply_follow(self, pos: PositionInfo, spec: SymbolSpec, sl_offset: float | None,
                      tp_offset: float | None) -> AccountResult:
        """Set the MT5 bracket of one bridge position from ATAS offsets around its own open price.
        sl_offset None keeps the current stop (never unprotect); tp_offset None removes the target."""
        side = 1 if pos.type == TYPE_BUY else -1
        tick = self.api.symbol_tick(pos.symbol)
        bid = tick.bid if tick else 0.0
        if sl_offset is not None and bid > 0 and abs(sl_offset) > MAX_STOP_FRACTION * bid:
            return self._warn(f"stop offset {sl_offset:g} wider than 5% of price; not applied", ticket=pos.ticket)
        new_sl = round(pos.price_open - side * sl_offset, spec.digits) if sl_offset is not None else pos.sl
        new_tp = round(pos.price_open + side * tp_offset, spec.digits) if tp_offset is not None else 0.0
        tol = (10 ** -spec.digits) / 2
        if abs(pos.sl - new_sl) <= tol and abs(pos.tp - new_tp) <= tol:
            return self._ok(ticket=pos.ticket, sl=new_sl, tp=new_tp or None)
        mod = self.api.order_send({"action": ACTION_SLTP, "symbol": pos.symbol, "position": pos.ticket,
                                   "sl": new_sl, "tp": new_tp, "magic": self.cfg.magic})
        log.info("%s: follow SLTP #%s sl=%s tp=%s -> %s", self.cfg.name, pos.ticket, new_sl, new_tp,
                 "None" if mod is None else f"{mod.retcode} {mod.comment!r}")
        if mod is None or mod.retcode not in (RET_DONE, RET_PLACED, RET_NO_CHANGES):
            why = "None" if mod is None else f"{mod.retcode} {mod.comment}"
            return self._warn(f"modify rejected ({why}); MT5 levels unchanged, will retry on sync", ticket=pos.ticket,
                              sl=pos.sl or None, tp=pos.tp or None)
        return self._ok(ticket=pos.ticket, sl=new_sl, tp=new_tp or None)

    def _modify(self, event: Event) -> AccountResult:
        res = self._resolve(event)
        f = event.follow
        mine = self._bridge_positions(res.symbol)
        if not mine:
            # bracket already hit, or the open never went through: never show this as green
            return self._warn("not mirrored (nothing to move)", closed=[])
        result = self._ok()
        for pos in mine:
            result = self._apply_follow(pos, res.spec, f.sl_offset, f.tp_offset)
            if result.status != "ok":
                return result
        return result

    def _find_position(self, broker: str, order_ticket: int) -> PositionInfo | None:
        mine = self._bridge_positions(broker)
        for p in mine:
            if p.ticket == order_ticket:
                return p
        return max(mine, key=lambda p: p.ticket) if mine else None

    # ------------------------------------------------------------------ close
    def _close_position(self, pos: PositionInfo, event_id: int) -> tuple[bool, str | None]:
        spec = self.api.symbol_info(pos.symbol)
        closing_type = TYPE_SELL if pos.type == TYPE_BUY else TYPE_BUY

        def build() -> dict:
            tick = self.api.symbol_tick(pos.symbol)
            price = tick.bid if closing_type == TYPE_SELL else tick.ask
            return {"action": ACTION_DEAL, "symbol": pos.symbol, "position": pos.ticket, "volume": pos.volume,
                    "type": closing_type, "price": price, "deviation": self._deviation(spec) if spec else 0,
                    "magic": self.cfg.magic, "comment": f"atas-{event_id}-close", "type_time": TIME_GTC,
                    "type_filling": self._filling(spec) if spec else FILLING_RETURN}

        result, err = self._send_with_retry(build)
        return (result is not None), err

    def _close(self, event: Event) -> AccountResult:
        res = self._resolve(event)
        closed: list[int] = []
        failures: list[str] = []
        for pos in self._bridge_positions(res.symbol):
            ok, err = self._close_position(pos, event.id)
            if ok:
                closed.append(pos.ticket)
            else:
                failures.append(f"#{pos.ticket}: {err}")
        if failures:
            return self._err("close failed: " + "; ".join(failures), closed=closed)
        return self._ok(closed=closed)

    # ------------------------------------------------------------------ sync
    def _preview(self, event: Event) -> tuple[Preview, str | None]:
        """Sizing preview for the status box. Returns (preview, blocking_note)."""
        try:
            res = self._resolve(event)
        except Refused as exc:
            return Preview(symbol=None, verified=False, source="none", note=str(exc)), str(exc)
        note = None
        if event.mode == "follow" and event.follow is not None:
            return self._follow_preview(event, res)
        try:
            self._check_confirmed(res)
            risk = self._risk(event)
            tick = self.api.symbol_tick(res.symbol)
            if tick is None or tick.bid <= 0:
                raise Refused(f"no quote yet for {res.symbol} (added to Market Watch; retrying)")
            self._guards(event, res, risk, tick.bid)
            balance, risk_money = self._risk_money(risk)
            lots = compute_lots(res.spec, risk_money, risk.stop_points)
            if lots <= 0:
                note = f"below min lot (risk {risk_money:.2f} over {risk.stop_points:g} pts)"
            else:
                uncapped = risk_money / (risk.stop_points * value_per_point_per_lot(res.spec))
                if uncapped > res.spec.volume_max:
                    note = (f"capped at broker max {res.spec.volume_max:g} lots (risk wanted {uncapped:.2f}); "
                            f"real risk is lower than {risk.risk_pct:g}%")
        except Refused as exc:
            note = str(exc)
            balance, lots = None, None
            try:
                balance, _ = self._risk_money(RiskParams(1.0, 1.0, 0.0, "balance"))
            except Refused:
                pass
        return Preview(symbol=res.symbol, verified=res.verified, source=res.source, lots=lots, balance=balance,
                       value_per_point=value_per_point_per_lot(res.spec), note=note), note

    def _follow_preview(self, event: Event, res: Resolved) -> tuple[Preview, str | None]:
        """Follow mode: lots are the user's; preview shows them floored and any blocking reason."""
        note = None
        lots, balance = None, None
        try:
            self._check_confirmed(res)
            tick = self.api.symbol_tick(res.symbol)
            if tick is None or tick.bid <= 0:
                raise Refused(f"no quote yet for {res.symbol} (added to Market Watch; retrying)")
            self._price_guard(event, res, tick.bid)
            lots, capped = floor_lots(res.spec, event.follow.lots)
            if lots <= 0:
                note = f"below min lot ({event.follow.lots:g} < {res.spec.volume_min:g})"
                lots = None
            elif capped:
                note = f"capped at broker max {res.spec.volume_max:g} lots"
            acc = self.api.account_info()
            balance = acc.balance if acc else None
        except Refused as exc:
            note = str(exc)
        return Preview(symbol=res.symbol, verified=res.verified, source=res.source, lots=lots, balance=balance,
                       value_per_point=value_per_point_per_lot(res.spec), note=note), note

    def _sync(self, event: Event) -> AccountResult:
        preview, blocking = self._preview(event)
        if preview.symbol is None:
            return self._err(blocking or "symbol not resolved", preview=preview)
        mine = self._bridge_positions(preview.symbol)
        if event.dir == "flat":
            if mine:
                r = self._close(event)
                r.preview = preview
                return r
            return self._warn(blocking, closed=[], preview=preview) if blocking else self._ok(closed=[], preview=preview)
        same = [p for p in mine if self._side_of(p) == event.dir]
        opposite = [p for p in mine if self._side_of(p) != event.dir]
        closed: list[int] = []
        for pos in opposite:  # CFD on the wrong side of the ATAS position: close it, never leave it
            ok, err = self._close_position(pos, event.id)
            if not ok:
                return self._err(f"direction mismatch: could not close #{pos.ticket}: {err}", closed=closed,
                                 preview=preview)
            closed.append(pos.ticket)
        if not same:
            reason = "direction mismatch: closed opposite CFD; not mirrored" if closed else "not mirrored"
            return self._warn(reason, closed=closed or None, preview=preview)
        f = event.follow
        if event.mode == "follow" and f is not None and (f.sl_offset is not None or f.tp_offset is not None):
            # repair a lost modify: bring the MT5 bracket back to the ATAS offsets
            spec = self.api.symbol_info(preview.symbol)
            for pos in same:
                r = self._apply_follow(pos, spec, f.sl_offset, f.tp_offset)
                if r.status != "ok":
                    r.preview, r.closed = preview, closed or None
                    return r
        if blocking:
            return self._warn(blocking, ticket=same[0].ticket, closed=closed or None, preview=preview)
        return self._ok(ticket=same[0].ticket, closed=closed or None, preview=preview)
