import json

from bridge import pairing


def test_pairing_write_read_round_trip(tmp_path):
    token = pairing.write(str(tmp_path), 5577)
    assert len(token) == 32
    assert pairing.read(str(tmp_path)) == (token, 5577)
    data = json.loads((tmp_path / "pairing.json").read_text(encoding="utf-8"))
    assert data == {"token": token, "port": 5577}


def test_pairing_token_persists(tmp_path):
    first = pairing.write(str(tmp_path), 5577)
    second = pairing.write(str(tmp_path), 5578)       # port may change, token must not
    assert first == second and pairing.read(str(tmp_path)) == (first, 5578)


def test_pairing_read_missing(tmp_path):
    assert pairing.read(str(tmp_path)) is None


def test_pairing_mirrors_to_extra_dirs(tmp_path):
    mirror = tmp_path / "ATAS X" / "VBridge"
    token = pairing.write(str(tmp_path / "main"), 5577, mirror_dirs=[str(mirror)])
    assert pairing.read(str(mirror)) == (token, 5577)
    pairing.write_token(str(tmp_path / "main"), "seeded", 5577, mirror_dirs=[str(mirror)])
    assert pairing.read(str(mirror)) == ("seeded", 5577)


def test_default_mirror_dirs_point_at_atas_x_roaming(monkeypatch):
    monkeypatch.setenv("APPDATA", r"C:\Users\u\AppData\Roaming")
    assert pairing.default_mirror_dirs() == [r"C:\Users\u\AppData\Roaming\ATAS X\VBridge"]


def test_pairing_write_token_seeds_a_known_token(tmp_path):
    pairing.write_token(str(tmp_path), "legacy-token-from-config", 5577)
    assert pairing.read(str(tmp_path)) == ("legacy-token-from-config", 5577)
    assert pairing.write(str(tmp_path), 5577) == "legacy-token-from-config"   # write() keeps it
