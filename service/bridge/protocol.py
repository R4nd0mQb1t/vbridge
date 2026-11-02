"""Newline-delimited JSON protocol between the ATAS indicator and the service."""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field

from bridge.version import VERSION

EVENT_TYPES = ("open", "close", "sync", "ping", "modify")
MODES = ("fixed", "follow", "follow_pct")
FOLLOW_MODES = ("follow", "follow_pct")   # both mirror the ATAS stop/target; follow_pct sizes from risk_pct
COMMANDS = ("disable", "enable")
DIRS = ("buy", "sell")


class ProtocolError(Exception):
    pass


@dataclass
class Event:
    id: int
    type: str
    symbol: str
    account: str
    security: str
    dir: str | None = None
    price: float | None = None
    volume: float | None = None
    ts: str = ""
    # v2 optional fields (absent from phase-1 indicators; the service falls back to account defaults)
    firm: str | None = None
    symbol_override: str | None = None
    instrument: str | None = None
    chart_price: float | None = None
    risk_pct: float | None = None
    stop_points: float | None = None
    tp_points: float | None = None
    risk_base: str | None = None
    client_version: str | None = None
    # v3: execution mode (fixed | follow | follow_pct) and the ATAS bracket as signed offsets from the ATAS entry
    mode: str | None = None
    follow: "Follow | None" = None
    arm_ts: float | None = None     # when this indicator copy armed (unix ms): the newest copy owns the chart


@dataclass
class Follow:
    lots: float
    sl_offset: float | None = None    # side*(entry - SL): >0 loss side, 0 breakeven, <0 stop in profit
    tp_offset: float | None = None    # side*(TP - entry); None = no target
    atas_entry: float | None = None


@dataclass
class Preview:
    symbol: str | None
    verified: bool
    source: str
    lots: float | None = None
    balance: float | None = None
    value_per_point: float | None = None
    note: str | None = None


@dataclass
class Hello:
    token: str


@dataclass
class Command:
    name: str
    token: str


@dataclass
class AccountResult:
    account: str
    status: str  # ok | warn | error
    ticket: int | None = None
    lots: float | None = None
    sl: float | None = None
    tp: float | None = None
    closed: list[int] | None = None
    reason: str | None = None
    preview: Preview | None = None


@dataclass
class Ack:
    id: int | None
    results: list[AccountResult] = field(default_factory=list)
    service_version: str = VERSION


def _opt_float(d: dict, key: str) -> float | None:
    v = d.get(key)
    return None if v is None else float(v)


def parse_line(line: str) -> Event | Hello | Command:
    try:
        d = json.loads(line)
    except (ValueError, TypeError) as exc:
        raise ProtocolError(f"bad json: {exc}") from exc
    if not isinstance(d, dict):
        raise ProtocolError("message must be an object")
    typ = d.get("type")
    if typ == "hello":
        return Hello(token=str(d.get("token", "")))
    if typ in COMMANDS:
        return Command(name=typ, token=str(d.get("token", "")))
    if typ not in EVENT_TYPES:
        raise ProtocolError(f"unknown type: {typ!r}")
    for key in ("id", "symbol", "account", "security"):
        if key not in d:
            raise ProtocolError(f"missing field '{key}'")
    direction = d.get("dir")
    if typ == "open" and direction not in DIRS:
        raise ProtocolError("open requires dir buy|sell")
    if typ == "sync" and direction not in DIRS + ("flat",):
        raise ProtocolError("sync requires dir buy|sell|flat")
    if direction is not None and direction not in DIRS + ("flat",):
        raise ProtocolError(f"bad dir: {direction!r}")
    risk_base = d.get("risk_base")
    if risk_base is not None and risk_base not in ("balance", "equity"):
        raise ProtocolError(f"bad risk_base: {risk_base!r}")
    mode = d.get("mode")
    if mode is not None and mode not in MODES:
        raise ProtocolError(f"bad mode: {mode!r}")
    follow = None
    raw_follow = d.get("follow")
    if raw_follow is not None:
        if not isinstance(raw_follow, dict):
            raise ProtocolError("follow must be an object")
        follow = Follow(lots=float(raw_follow.get("lots") or 0), sl_offset=_opt_float(raw_follow, "sl_offset"),
                        tp_offset=_opt_float(raw_follow, "tp_offset"),
                        atas_entry=_opt_float(raw_follow, "atas_entry"))
    if typ == "modify" and follow is None:
        raise ProtocolError("modify requires a follow object")
    if typ == "open" and mode in FOLLOW_MODES:
        if follow is None:
            raise ProtocolError("follow open requires a follow object")
        if mode == "follow" and follow.lots <= 0:
            raise ProtocolError("follow open requires lots > 0")
        if follow.sl_offset is None:
            raise ProtocolError("follow open requires sl_offset (a stop in ATAS)")
    return Event(
        id=int(d["id"]), type=typ, symbol=str(d["symbol"]), account=str(d["account"]),
        security=str(d["security"]), dir=direction, price=_opt_float(d, "price"),
        volume=_opt_float(d, "volume"), ts=str(d.get("ts", "")),
        firm=_opt_str(d, "firm"), symbol_override=_opt_str(d, "symbol_override"),
        instrument=_opt_str(d, "instrument"), chart_price=_opt_float(d, "chart_price"),
        risk_pct=_opt_float(d, "risk_pct"), stop_points=_opt_float(d, "stop_points"),
        tp_points=_opt_float(d, "tp_points"), risk_base=risk_base, client_version=_opt_str(d, "client_version"),
        mode=mode, follow=follow, arm_ts=_opt_float(d, "arm_ts"),
    )


def result_from_dict(d: dict) -> AccountResult:
    """Rebuild an AccountResult from asdict() output (worker Pipe), restoring the nested Preview."""
    d = dict(d)
    if isinstance(d.get("preview"), dict):
        d["preview"] = Preview(**d["preview"])
    return AccountResult(**d)


def event_from_dict(d: dict) -> Event:
    """Rebuild an Event from dataclasses.asdict() output (worker Pipe), restoring the nested Follow."""
    d = dict(d)
    if isinstance(d.get("follow"), dict):
        d["follow"] = Follow(**d["follow"])
    return Event(**d)


def _opt_str(d: dict, key: str) -> str | None:
    v = d.get(key)
    if v is None:
        return None
    s = str(v).strip()
    return s or None


def _strip_none(obj):
    if isinstance(obj, dict):
        return {k: _strip_none(v) for k, v in obj.items() if v is not None}
    return obj


def dump_ack(ack: Ack) -> str:
    results = [_strip_none(asdict(r)) for r in ack.results]
    return json.dumps({"id": ack.id, "results": results, "service_version": ack.service_version},
                      separators=(",", ":")) + "\n"


def dump_error(message: str) -> str:
    return json.dumps({"id": None, "error": message}, separators=(",", ":")) + "\n"
