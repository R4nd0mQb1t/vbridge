"""Export a support zip: logs, settings, audit trail, system info. Never the token."""
from __future__ import annotations

import os
import platform
import sys
import zipfile
from datetime import datetime, timezone

from bridge.version import VERSION

EXCLUDED = {"pairing.json", "pairing.json.tmp", "settings.json.tmp"}


def export_zip(settings_dir: str, logs_dir: str, out_path: str) -> str:
    os.makedirs(os.path.dirname(os.path.abspath(out_path)) or ".", exist_ok=True)
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as z:
        if os.path.isdir(logs_dir):
            for name in sorted(os.listdir(logs_dir)):
                path = os.path.join(logs_dir, name)
                if os.path.isfile(path):
                    z.write(path, f"logs/{name}")
        for name in ("settings.json", "audit.jsonl", "firms.json"):
            path = os.path.join(settings_dir, name)
            if os.path.isfile(path) and name not in EXCLUDED:
                z.write(path, name)
        info = "\n".join([
            f"VBridge service {VERSION}",
            f"exported {datetime.now(timezone.utc).isoformat(timespec='seconds')}",
            f"python {sys.version.split()[0]} {platform.architecture()[0]}",
            f"os {platform.platform()}",
            f"settings_dir {settings_dir}",
        ])
        z.writestr("system.txt", info)
    return out_path
