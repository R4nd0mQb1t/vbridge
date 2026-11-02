"""Append-only audit trail. Never consulted for decisions; MT5 is the source of truth."""
from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone


class AuditLog:
    def __init__(self, path: str) -> None:
        self.path = path
        self._lock = threading.Lock()
        directory = os.path.dirname(os.path.abspath(path))
        os.makedirs(directory, exist_ok=True)

    def append(self, record: dict) -> None:
        record = {"ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"), **record}
        line = json.dumps(record, separators=(",", ":"), default=str) + "\n"
        with self._lock, open(self.path, "a", encoding="utf-8") as fh:
            fh.write(line)
