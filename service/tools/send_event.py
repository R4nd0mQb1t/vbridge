"""Fake indicator client for testing the service without ATAS.

  python tools/send_event.py --token T open buy
  python tools/send_event.py --token T close
  python tools/send_event.py --token T sync flat|buy|sell
  python tools/send_event.py --token T ping | disable | enable
"""
from __future__ import annotations

import argparse
import json
import socket
import time
from datetime import datetime, timezone


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=5577)
    ap.add_argument("--token", required=True)
    ap.add_argument("--symbol", default="NQ")
    ap.add_argument("--account", default="manual")
    ap.add_argument("--security", default="NQZ6")
    ap.add_argument("--id", type=int, default=int(time.time()) % 1_000_000)
    ap.add_argument("type", choices=["open", "close", "sync", "ping", "disable", "enable"])
    ap.add_argument("dir", nargs="?", choices=["buy", "sell", "flat"])
    a = ap.parse_args()

    if a.type == "open" and a.dir not in ("buy", "sell"):
        ap.error("open needs buy|sell")
    if a.type == "sync" and a.dir not in ("buy", "sell", "flat"):
        ap.error("sync needs buy|sell|flat")

    sock = socket.create_connection((a.host, a.port), timeout=15)
    f = sock.makefile("rwb")

    def send(obj):
        f.write((json.dumps(obj) + "\n").encode())
        f.flush()
        line = f.readline()
        return json.loads(line) if line else None

    hello = send({"type": "hello", "token": a.token})
    if not hello or hello.get("hello") != "ok":
        print("hello rejected:", hello)
        return 1
    if a.type in ("disable", "enable"):
        msg = {"type": a.type, "token": a.token}
    else:
        msg = {"id": a.id, "type": a.type, "dir": a.dir, "symbol": a.symbol, "account": a.account,
               "security": a.security, "price": 0, "volume": 1,
               "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds")}
    t0 = time.perf_counter()
    ack = send(msg)
    dt = (time.perf_counter() - t0) * 1000
    print(json.dumps(ack, indent=2))
    print(f"round trip {dt:.0f} ms")
    sock.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
