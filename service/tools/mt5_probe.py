"""Print account, terminal and symbol specs for one MT5 terminal.

  python tools/mt5_probe.py --terminal "C:/Program Files/MetaTrader 5/terminal64.exe" --find US100,USTEC,NAS,NQ
"""
from __future__ import annotations

import argparse

import MetaTrader5 as mt5


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--terminal", required=True)
    ap.add_argument("--find", default="US100,USTEC,NAS,NQ,TEC", help="comma-separated name fragments")
    a = ap.parse_args()

    if not mt5.initialize(path=a.terminal):
        print("initialize failed:", mt5.last_error())
        return 1
    acc = mt5.account_info()
    term = mt5.terminal_info()
    print(f"account   : login={acc.login} server={acc.server} currency={acc.currency} "
          f"balance={acc.balance} equity={acc.equity} trade_allowed={acc.trade_allowed}")
    print(f"terminal  : connected={term.connected} trade_allowed(AlgoTrading)={term.trade_allowed} "
          f"build={term.build} path={term.path}")
    frags = [f.strip().upper() for f in a.find.split(",") if f.strip()]
    syms = mt5.symbols_get() or ()
    rows = [s for s in syms if any(f in s.name.upper() for f in frags)]
    print(f"symbols   : {len(syms)} on server, {len(rows)} match {frags}")
    hdr = f"{'name':<14}{'digits':>6}{'point':>8}{'tick_sz':>9}{'tick_val':>10}{'tick_val_loss':>14}" \
          f"{'vmin':>7}{'vmax':>8}{'vstep':>7}{'stops':>6}{'fill':>5}  description"
    print(hdr)
    for s in rows:
        print(f"{s.name:<14}{s.digits:>6}{s.point:>8g}{s.trade_tick_size:>9g}{s.trade_tick_value:>10g}"
              f"{s.trade_tick_value_loss:>14g}{s.volume_min:>7g}{s.volume_max:>8g}{s.volume_step:>7g}"
              f"{s.trade_stops_level:>6}{s.filling_mode:>5}  {s.description}")
    mt5.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
