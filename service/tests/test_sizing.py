from dataclasses import replace

from bridge.sizing import SymbolSpec, bracket_prices, check_stops_level, compute_lots

# FundingPips-like US100: 1 lot = $1 per index point, lot step 0.01, max 100 lots
SPEC = SymbolSpec(name="US100", digits=2, point=0.01, tick_size=0.01, tick_value_loss=0.01,
                  volume_min=0.01, volume_max=100.0, volume_step=0.01, stops_level=0, filling_mode=1)


def test_one_percent_of_100k_over_20_points_is_50_lots():
    assert compute_lots(SPEC, 1000.0, 20) == 50.0


def test_floors_to_step():
    assert compute_lots(replace(SPEC, volume_max=1000.0), 1000.0, 7) == 142.85  # 142.857 floored


def test_caps_at_volume_max():
    assert compute_lots(SPEC, 1000.0, 7) == 100.0


def test_below_min_returns_zero():
    assert compute_lots(SPEC, 0.05, 20) == 0.0


def test_step_floor_no_float_drift():
    assert compute_lots(SPEC, 0.07 * 20, 20) == 0.07


def test_coarser_step_and_min():
    spec = replace(SPEC, volume_min=0.1, volume_step=0.1)
    assert compute_lots(spec, 1000.0, 7) == 100.0
    assert compute_lots(spec, 3.0, 20) == 0.1
    assert compute_lots(spec, 1.0, 20) == 0.0


def test_tick_value_scales_lots():
    spec = replace(SPEC, tick_value_loss=0.1)  # $10 per point per lot
    assert compute_lots(spec, 1000.0, 20) == 5.0


def test_bracket_buy():
    assert bracket_prices(SPEC, "buy", 20123.456, 20, 40) == (20103.46, 20163.46)


def test_bracket_sell_no_tp():
    assert bracket_prices(SPEC, "sell", 20123.45, 20, 0) == (20143.45, None)


def test_bracket_sell_with_tp():
    assert bracket_prices(SPEC, "sell", 20123.45, 20, 40) == (20143.45, 20083.45)


def test_stops_level_ok_when_zero():
    assert check_stops_level(SPEC, 20, 40) is None


def test_stops_level_violation_stop():
    reason = check_stops_level(replace(SPEC, stops_level=3000), 20, 40)
    assert reason is not None and "stop below broker minimum" in reason


def test_stops_level_violation_tp_only():
    reason = check_stops_level(replace(SPEC, stops_level=3000), 40, 20)
    assert reason is not None and "tp below broker minimum" in reason


def test_stops_level_ignores_tp_zero():
    assert check_stops_level(replace(SPEC, stops_level=1500), 20, 0) is None
