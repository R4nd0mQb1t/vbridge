"""Update check: GET <url> -> {"version": "x.y.z", "url": "..."}; never raises, never installs."""
from __future__ import annotations

import json
import logging
import urllib.request
from dataclasses import dataclass

log = logging.getLogger("bridge.update")

# latest.json is attached to every GitHub release; /releases/latest/download/ always points at the newest.
UPDATE_URL = "https://github.com/R4nd0mQb1t/vbridge/releases/latest/download/latest.json"


@dataclass(frozen=True)
class UpdateInfo:
    available: bool
    latest: str
    url: str | None


def _default_fetch(url: str, timeout: float = 5.0) -> bytes:
    with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310 - fixed https URL
        return resp.read()


def _key(version: str) -> tuple[int, ...]:
    parts = []
    for piece in version.strip().split("."):
        digits = "".join(ch for ch in piece if ch.isdigit())
        parts.append(int(digits) if digits else 0)
    while len(parts) < 3:
        parts.append(0)
    return tuple(parts)


def check_latest(url: str, current: str, fetch=_default_fetch) -> UpdateInfo:
    try:
        data = json.loads(fetch(url, timeout=5.0).decode("utf-8"))
        latest = str(data["version"])
        link = data.get("url")
    except Exception as exc:  # noqa: BLE001 - an update check must never hurt the service
        log.info("update check skipped: %s", exc)
        return UpdateInfo(available=False, latest=current, url=None)
    if _key(latest) > _key(current):
        return UpdateInfo(available=True, latest=latest, url=str(link) if link else None)
    return UpdateInfo(available=False, latest=latest, url=None)
