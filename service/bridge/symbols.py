"""Broker symbol resolution: override -> firm table -> discovery -> none (spec 6.3)."""
from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field

from bridge.mt5_api import TRADE_MODE_FULL, Mt5Api, SymbolListing

log = logging.getLogger("bridge.symbols")

SHIPPED_FIRMS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "firms.json")
MONTH_CODE = re.compile(r"[FGHJKMNQUVXZ]\d\d$")


@dataclass
class FirmEntry:
    server_prefixes: list[str]
    symbols: dict[str, dict]      # root -> {"name": str, "verified": bool}


@dataclass
class Firms:
    firms: dict[str, FirmEntry]
    roots: dict[str, str] = field(default_factory=dict)        # micro -> full
    keywords: dict[str, list[str]] = field(default_factory=dict)
    exclude: dict[str, list[str]] = field(default_factory=dict)

    def names(self) -> list[str]:
        return list(self.firms.keys())

    def canonical_root(self, root: str) -> str:
        root = (root or "").upper()
        return self.roots.get(root, root)

    def firm_for_server(self, server: str) -> str | None:
        s = (server or "").lower()
        for name, entry in self.firms.items():
            for prefix in entry.server_prefixes:
                if prefix and s.startswith(prefix.lower()):
                    return name
        return None

    def table_entry(self, firm: str | None, root: str) -> dict | None:
        entry = self.firms.get(firm or "")
        if entry is None:
            return None
        return entry.symbols.get(root)


def _merge(base: dict, overlay: dict) -> dict:
    out = dict(base)
    for key in ("roots", "keywords", "exclude"):
        out[key] = {**base.get(key, {}), **overlay.get(key, {})}
    out["firms"] = {**base.get("firms", {}), **overlay.get("firms", {})}
    return out


def load_firms(user_dir: str | None) -> Firms:
    with open(SHIPPED_FIRMS, encoding="utf-8") as fh:
        data = json.load(fh)
    if user_dir:
        user_path = os.path.join(user_dir, "firms.json")
        if os.path.exists(user_path):
            try:
                with open(user_path, encoding="utf-8") as fh:
                    data = _merge(data, json.load(fh))
                log.info("firms overlay loaded from %s", user_path)
            except (OSError, ValueError) as exc:
                log.error("ignoring bad user firms.json at %s: %s", user_path, exc)
    firms = {name: FirmEntry(server_prefixes=list(e.get("server_prefixes", [])), symbols=dict(e.get("symbols", {})))
             for name, e in data.get("firms", {}).items()}
    return Firms(firms=firms, roots={k.upper(): v.upper() for k, v in data.get("roots", {}).items()},
                 keywords=data.get("keywords", {}), exclude=data.get("exclude", {}))


@dataclass(frozen=True)
class Resolution:
    symbol: str | None
    verified: bool
    source: str          # override | table | discovery | none
    reason: str


def _is_dated_future(l: SymbolListing) -> bool:
    return bool(MONTH_CODE.search(l.name)) or "futures" in l.description.lower()


def discover(listings: list[SymbolListing], firms: Firms, root: str) -> SymbolListing | None:
    """Best cash CFD for a root by name/description keywords; never a dated future."""
    root = firms.canonical_root(root)
    keys = [k.lower() for k in firms.keywords.get(root, [])]
    bad = [k.lower() for k in firms.exclude.get(root, [])]
    if not keys:
        return None
    candidates = []
    for l in listings:
        hay = f"{l.name} {l.description}".lower()
        if not any(k in hay for k in keys):
            continue
        if any(b in hay for b in bad):
            continue
        if _is_dated_future(l) or l.trade_mode != TRADE_MODE_FULL:
            continue
        candidates.append(l)
    if not candidates:
        return None
    candidates.sort(key=lambda l: (0 if l.has_tick else 1, len(l.name), l.name))
    return candidates[0]


def resolve(api: Mt5Api, firms: Firms, firm: str | None, root: str, override: str | None) -> Resolution:
    root = firms.canonical_root(root)
    if override:
        if api.symbol_select(override, True) and api.symbol_info(override) is not None:
            return Resolution(override, True, "override", f"override {override} present on server")
        return Resolution(None, False, "none", f"override symbol {override} not found on this server")
    entry = firms.table_entry(firm, root)
    if entry and entry.get("name"):
        name = entry["name"]
        if api.symbol_select(name, True) and api.symbol_info(name) is not None:
            verified = bool(entry.get("verified", False))
            return Resolution(name, verified, "table", f"{firm} table: {root} -> {name}"
                              + ("" if verified else " (unverified)"))
        log.warning("firm table %s says %s for %s but the server lacks it; trying discovery", firm, name, root)
    listings = api.symbols_all()
    if listings is None:
        return Resolution(None, False, "none", f"symbol list unavailable: {api.last_error()}")
    found = discover(listings, firms, root)
    if found is None:
        return Resolution(None, False, "none",
                          f"symbol not resolved for {root} at {firm or 'Other'}; set Broker symbol override")
    return Resolution(found.name, False, "discovery", f"discovered {found.name} ({found.description}) for {root}")
