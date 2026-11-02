import json
import zipfile

from bridge.diagnostics import export_zip


def test_diagnostics_zip_excludes_token(tmp_path):
    sdir = tmp_path / "VBridge"
    logs = sdir / "logs"
    logs.mkdir(parents=True)
    (logs / "bridge.log").write_text("hello log", encoding="utf-8")
    (logs / "worker-1.log").write_text("worker log", encoding="utf-8")
    (sdir / "settings.json").write_text(json.dumps({"version": 1, "accounts": {"1": {"login": 1, "confirmed_symbols": ["NQ:NDX100"]}}}), encoding="utf-8")
    (sdir / "pairing.json").write_text(json.dumps({"token": "SECRET-TOKEN", "port": 5577}), encoding="utf-8")
    (sdir / "audit.jsonl").write_text('{"event":{"id":1}}\n', encoding="utf-8")
    out = export_zip(str(sdir), str(logs), str(tmp_path / "diag.zip"))
    with zipfile.ZipFile(out) as z:
        names = set(z.namelist())
        assert {"logs/bridge.log", "logs/worker-1.log", "settings.json", "audit.jsonl", "system.txt"} <= names
        assert "pairing.json" not in names
        blob = b"".join(z.read(n) for n in names)
        assert b"SECRET-TOKEN" not in blob
        assert b"NQ:NDX100" in z.read("settings.json")
        assert b"VBridge" in z.read("system.txt")
