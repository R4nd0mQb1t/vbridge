import json

from bridge.update import UpdateInfo, check_latest


def fetch_ok(payload):
    def _fetch(url, timeout=5.0):
        return json.dumps(payload).encode("utf-8")
    return _fetch


def fetch_fail(url, timeout=5.0):
    raise OSError("no network")


def test_update_available_when_newer():
    info = check_latest("http://x/latest.json", "2.0.0", fetch=fetch_ok({"version": "2.1.0", "url": "http://x/VBridge-Setup-2.1.0.exe"}))
    assert info == UpdateInfo(available=True, latest="2.1.0", url="http://x/VBridge-Setup-2.1.0.exe")


def test_update_not_available_same_or_older():
    assert check_latest("u", "2.0.0", fetch=fetch_ok({"version": "2.0.0", "url": "x"})).available is False
    assert check_latest("u", "2.0.0", fetch=fetch_ok({"version": "1.9.9", "url": "x"})).available is False
    assert check_latest("u", "2.0.10", fetch=fetch_ok({"version": "2.0.9", "url": "x"})).available is False


def test_update_network_error_is_quiet():
    info = check_latest("u", "2.0.0", fetch=fetch_fail)
    assert info.available is False and info.latest == "2.0.0" and info.url is None


def test_update_bad_payload_is_quiet():
    info = check_latest("u", "2.0.0", fetch=lambda url, timeout=5.0: b"<html>")
    assert info.available is False and info.latest == "2.0.0"
