"""Lot sizing and bracket arithmetic. Pure functions, no MT5 dependency."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_FLOOR, Decimal


@dataclass(frozen=True)
class SymbolSpec:
    name: str
    digits: int
    point: float
    tick_size: float
    tick_value_loss: float   # account currency per tick per 1 lot (loss side)
    volume_min: float
    volume_max: float
    volume_step: float
    stops_level: int         # broker minimum SL/TP distance, in points
    filling_mode: int        # bitmask: 1 FOK, 2 IOC


def value_per_point_per_lot(spec: SymbolSpec) -> float:
    """Account currency moved by a 1.0 price change on 1 lot."""
    return spec.tick_value_loss / spec.tick_size


def compute_lots(spec: SymbolSpec, risk_money: float, stop_points: float) -> float:
    """Largest lot size, floored to volume_step, whose loss at stop_points <= risk_money.
    Returns 0.0 when that size is below volume_min; never rounds up."""
    if risk_money <= 0 or stop_points <= 0:
        return 0.0
    raw = Decimal(repr(risk_money)) / (Decimal(repr(stop_points)) * Decimal(repr(value_per_point_per_lot(spec))))
    step = Decimal(repr(spec.volume_step))
    lots = (raw / step).to_integral_value(rounding=ROUND_FLOOR) * step
    lots = min(lots, Decimal(repr(spec.volume_max)))
    if lots < Decimal(repr(spec.volume_min)):
        return 0.0
    return float(lots)


def bracket_prices(spec: SymbolSpec, side: str, fill: float, stop_points: float,
                   tp_points: float) -> tuple[float, float | None]:
    sign = 1 if side == "buy" else -1
    sl = round(fill - sign * stop_points, spec.digits)
    tp = round(fill + sign * tp_points, spec.digits) if tp_points > 0 else None
    return sl, tp


def check_stops_level(spec: SymbolSpec, stop_points: float, tp_points: float) -> str | None:
    """Reason string when the configured distances violate the broker's stops level."""
    min_points = spec.stops_level * spec.point
    if stop_points < min_points:
        return f"stop below broker minimum ({stop_points} < {min_points:g})"
    if tp_points > 0 and tp_points < min_points:
        return f"tp below broker minimum ({tp_points} < {min_points:g})"
    return None


def floor_lots(spec: SymbolSpec, lots: float) -> tuple[float, bool]:
    """User-chosen lots floored to volume_step and capped at volume_max.
    Returns (lots, capped); lots is 0.0 when below volume_min. Never rounds up."""
    if lots <= 0:
        return 0.0, False
    step = Decimal(repr(spec.volume_step))
    out = (Decimal(repr(lots)) / step).to_integral_value(rounding=ROUND_FLOOR) * step
    capped = out > Decimal(repr(spec.volume_max))
    out = min(out, Decimal(repr(spec.volume_max)))
    if out < Decimal(repr(spec.volume_min)):
        return 0.0, False
    return float(out), capped
