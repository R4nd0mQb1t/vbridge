import json

import pytest

from bridge.protocol import (Ack, AccountResult, Command, Event, Hello, ProtocolError,
                             dump_ack, parse_line)

OPEN = ('{"id":12,"type":"open","dir":"buy","symbol":"NQ","account":"demo1",'
        '"security":"NQZ6","price":20123.5,"volume":1,"ts":"2026-10-29T14:31:02.120Z"}')


def test_parse_open_event():
    ev = parse_line(OPEN)
    assert isinstance(ev, Event)
    assert (ev.id, ev.type, ev.dir, ev.symbol) == (12, "open", "buy", "NQ")
    assert (ev.account, ev.security, ev.price, ev.volume) == ("demo1", "NQZ6", 20123.5, 1.0)
    assert ev.ts == "2026-10-29T14:31:02.120Z"


def test_parse_close_event_without_dir():
    ev = parse_line('{"id":13,"type":"close","symbol":"NQ","account":"demo1","security":"NQZ6","ts":"t"}')
    assert ev.type == "close" and ev.dir is None and ev.price is None


def test_parse_sync_flat():
    ev = parse_line('{"id":14,"type":"sync","dir":"flat","symbol":"NQ","account":"demo1","security":"NQZ6","ts":"t"}')
    assert ev.type == "sync" and ev.dir == "flat"


def test_parse_hello():
    h = parse_line('{"type":"hello","token":"abc"}')
    assert isinstance(h, Hello) and h.token == "abc"


def test_parse_command():
    c = parse_line('{"type":"disable","token":"abc"}')
    assert isinstance(c, Command) and c.name == "disable" and c.token == "abc"


def test_parse_rejects_unknown_type():
    with pytest.raises(ProtocolError, match="type"):
        parse_line('{"id":1,"type":"teleport","symbol":"NQ","account":"a","security":"s","ts":"t"}')


def test_parse_rejects_open_without_dir():
    with pytest.raises(ProtocolError, match="dir"):
        parse_line('{"id":1,"type":"open","symbol":"NQ","account":"a","security":"s","ts":"t"}')


def test_parse_rejects_bad_json():
    with pytest.raises(ProtocolError):
        parse_line("{not json")


V2_OPEN = ('{"id":40,"type":"open","dir":"buy","symbol":"NQ","account":"demo1","security":"NQZ6@CME",'
           '"price":20123.5,"volume":1,"ts":"t","firm":"FTMO","symbol_override":"US100","instrument":"NQZ6@CME",'
           '"chart_price":20123.25,"risk_pct":0.5,"stop_points":10,"tp_points":0,"risk_base":"equity",'
           '"client_version":"2.0.0"}')


def test_v2_open_carries_risk_fields():
    ev = parse_line(V2_OPEN)
    assert (ev.firm, ev.symbol_override, ev.instrument) == ("FTMO", "US100", "NQZ6@CME")
    assert (ev.chart_price, ev.risk_pct, ev.stop_points, ev.tp_points) == (20123.25, 0.5, 10.0, 0.0)
    assert (ev.risk_base, ev.client_version) == ("equity", "2.0.0")


def test_v1_event_without_risk_fields_parses():
    ev = parse_line(OPEN)
    assert ev.firm is None and ev.symbol_override is None and ev.instrument is None
    assert ev.chart_price is None and ev.risk_pct is None and ev.stop_points is None
    assert ev.tp_points is None and ev.risk_base is None and ev.client_version is None


def test_bad_risk_base_rejected():
    with pytest.raises(ProtocolError, match="risk_base"):
        parse_line(V2_OPEN.replace('"risk_base":"equity"', '"risk_base":"margin"'))


def test_dump_ack_includes_service_version_and_preview():
    from bridge.protocol import Preview
    from bridge.version import VERSION
    ack = Ack(id=41, results=[AccountResult(account="a", status="ok", preview=Preview(
        symbol="NDX100", verified=True, source="table", lots=1.25, balance=50000.0, value_per_point=20.0, note=None))])
    d = json.loads(dump_ack(ack))
    assert d["service_version"] == VERSION
    p = d["results"][0]["preview"]
    assert p == {"symbol": "NDX100", "verified": True, "source": "table", "lots": 1.25, "balance": 50000.0,
                 "value_per_point": 20.0}


FOLLOW_OPEN = ('{"id":60,"type":"open","dir":"buy","symbol":"NQ","account":"a","security":"NQZ6","ts":"t",'
               '"mode":"follow","follow":{"lots":0.5,"sl_offset":20.0,"tp_offset":12.5,"atas_entry":28775.5}}')


def test_parse_follow_open():
    ev = parse_line(FOLLOW_OPEN)
    assert ev.mode == "follow"
    assert (ev.follow.lots, ev.follow.sl_offset, ev.follow.tp_offset, ev.follow.atas_entry) == (0.5, 20.0, 12.5, 28775.5)


def test_parse_modify():
    ev = parse_line('{"id":61,"type":"modify","symbol":"NQ","account":"a","security":"NQZ6","ts":"t",'
                    '"mode":"follow","follow":{"lots":0.5,"sl_offset":0.0,"tp_offset":null}}')
    assert ev.type == "modify" and ev.follow.sl_offset == 0.0 and ev.follow.tp_offset is None


def test_modify_requires_follow():
    with pytest.raises(ProtocolError, match="follow"):
        parse_line('{"id":62,"type":"modify","symbol":"NQ","account":"a","security":"s","ts":"t"}')


def test_follow_open_without_stop_rejected():
    with pytest.raises(ProtocolError, match="sl_offset"):
        parse_line(FOLLOW_OPEN.replace('"sl_offset":20.0', '"sl_offset":null'))


def test_follow_open_needs_positive_lots():
    with pytest.raises(ProtocolError, match="lots"):
        parse_line(FOLLOW_OPEN.replace('"lots":0.5', '"lots":0'))


def test_event_round_trips_through_dict():
    from dataclasses import asdict
    from bridge.protocol import Follow, event_from_dict
    ev = parse_line(FOLLOW_OPEN)
    back = event_from_dict(asdict(ev))
    assert back == ev and isinstance(back.follow, Follow)
    plain = parse_line(OPEN)
    assert event_from_dict(asdict(plain)) == plain


def test_v2_open_has_no_mode():
    ev = parse_line(OPEN)
    assert ev.mode is None and ev.follow is None


def test_dump_ack_is_single_line_json():
    ack = Ack(id=12, results=[AccountResult(account="fundingpips-demo", status="ok", ticket=123456,
                                             lots=0.8, sl=20103.5, tp=20163.5)])
    line = dump_ack(ack)
    assert line.endswith("\n") and line.count("\n") == 1
    d = json.loads(line)
    assert d["id"] == 12 and d["results"][0]["ticket"] == 123456 and d["results"][0]["status"] == "ok"
    assert "reason" not in d["results"][0]  # None fields are omitted


def test_result_round_trips_through_dict_with_preview():
    from dataclasses import asdict
    from bridge.protocol import Preview, result_from_dict
    r = AccountResult(account="a", status="warn", reason="x",
                      preview=Preview(symbol="NDX100", verified=True, source="table", lots=0.1, note="n"))
    back = result_from_dict(asdict(r))
    assert back == r and isinstance(back.preview, Preview)
    plain = AccountResult(account="a", status="ok", closed=[1])
    assert result_from_dict(asdict(plain)) == plain


# --- 2.2: Percent Follow (risk % sizes the lots, ATAS stop/target as in Follow ATAS) -----------------
PCT_OPEN = ('{"id":63,"type":"open","dir":"buy","symbol":"NQ","account":"a","security":"NQZ6","ts":"t",'
            '"risk_pct":1.0,"mode":"follow_pct","follow":{"lots":0,"sl_offset":20.0,"tp_offset":40.0}}')


def test_parse_percent_follow_open_without_lots():
    ev = parse_line(PCT_OPEN)
    assert ev.mode == "follow_pct" and ev.risk_pct == 1.0 and ev.follow.sl_offset == 20.0


def test_percent_follow_open_without_stop_rejected():
    with pytest.raises(ProtocolError, match="sl_offset"):
        parse_line(PCT_OPEN.replace('"sl_offset":20.0', '"sl_offset":null'))


def test_percent_follow_modify_parses():
    ev = parse_line('{"id":64,"type":"modify","symbol":"NQ","account":"a","security":"NQZ6","ts":"t",'
                    '"mode":"follow_pct","follow":{"lots":0,"sl_offset":0.0,"tp_offset":40.0}}')
    assert ev.type == "modify" and ev.mode == "follow_pct"
