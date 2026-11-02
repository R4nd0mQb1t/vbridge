"""pairing.json: the shared token the indicator reads so nobody has to type it.

Written to the settings dir and mirrored into %AppData%\\ATAS X\\VBridge: ATAS X cannot see
%LocalAppData% from inside an indicator (observed 2026-10-29), but it reads its own Roaming folder.
"""
from __future__ import annotations

import json
import os
import secrets
import stat

FILE = "pairing.json"


def default_mirror_dirs() -> list[str]:
    appdata = os.environ.get("APPDATA")
    if not appdata:
        return []
    return [os.path.join(appdata, "ATAS X", "VBridge")]


def _write_file(directory: str, token: str, port: int) -> None:
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, FILE)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump({"token": token, "port": int(port)}, fh)
    os.replace(tmp, path)
    try:
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass


def write(settings_dir: str, port: int, mirror_dirs: list[str] | None = None) -> str:
    """Create the token once; keep it across restarts; always refresh the port. Returns the token."""
    existing = read(settings_dir)
    token = existing[0] if existing else secrets.token_hex(16)
    write_token(settings_dir, token, port, mirror_dirs)
    return token


def write_token(settings_dir: str, token: str, port: int, mirror_dirs: list[str] | None = None) -> None:
    """Write a known token (legacy config.yaml import) to the settings dir and every mirror."""
    _write_file(settings_dir, token, port)
    for d in (default_mirror_dirs() if mirror_dirs is None else mirror_dirs):
        try:
            _write_file(d, token, port)
        except OSError:
            pass


def read(settings_dir: str) -> tuple[str, int] | None:
    path = os.path.join(settings_dir, FILE)
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        token = str(data.get("token") or "")
        port = int(data.get("port") or 0)
        if not token or not port:
            return None
        return token, port
    except (OSError, ValueError):
        return None
