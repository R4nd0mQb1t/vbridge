import json
import socket

import pytest

from bridge.config import Config
from bridge.protocol import AccountResult
from bridge.router import Router
from bridge.server import BridgeServer
from bridge.state import AuditLog


class RecordingHandle:
    def __init__(self, name="rec"):
        self.name = name
        self.events = []

    def call(self, event, timeout=10.0):
        self.events.append(event)
        return AccountResult(account=self.name, status="ok", ticket=1 if event.type == "open" else None)


@pytest.fixture
def server(tmp_path):
    cfg = Config(host="127.0.0.1", port=0, token="secret", accounts=[], state_dir=str(tmp_path))
    handle = RecordingHandle()
    router = Router(cfg, [handle], AuditLog(str(tmp_path / "audit.jsonl")), str(tmp_path / "DISABLE"))
    srv = BridgeServer(router, "127.0.0.1", 0)
    srv.start()
    yield srv, handle
    srv.stop()


class Client:
    def __init__(self, port):
        self.sock = socket.create_connection(("127.0.0.1", port), timeout=5)
        self.buf = b""

    def send(self, obj):
        self.sock.sendall((json.dumps(obj) + "\n").encode())

    def recv(self):
        while b"\n" not in self.buf:
            chunk = self.sock.recv(4096)
            if not chunk:
                return None
            self.buf += chunk
        line, self.buf = self.buf.split(b"\n", 1)
        return json.loads(line)

    def close(self):
        self.sock.close()


def event(id_, type_, dir_=None, account="atas-demo"):
    return {"id": id_, "type": type_, "dir": dir_, "symbol": "NQ", "account": account, "security": "NQZ6",
            "price": 20123.5, "volume": 1, "ts": "t"}


def test_server_requires_hello(server):
    srv, handle = server
    c = Client(srv.port)
    c.send(event(1, "ping"))
    assert c.recv() is None  # closed without an ack
    assert handle.events == []


def test_server_rejects_bad_token(server):
    srv, _ = server
    c = Client(srv.port)
    c.send({"type": "hello", "token": "nope"})
    assert c.recv() is None


def test_server_round_trip_open_close(server):
    srv, handle = server
    c = Client(srv.port)
    c.send({"type": "hello", "token": "secret"})
    assert c.recv() == {"id": None, "hello": "ok"}
    c.send(event(1, "open", "buy"))
    ack = c.recv()
    assert ack["id"] == 1 and ack["results"][0]["status"] == "ok" and ack["results"][0]["ticket"] == 1
    c.send(event(2, "close"))
    assert c.recv()["id"] == 2
    assert [e.type for e in handle.events] == ["open", "close"]


def test_server_protocol_error_keeps_connection(server):
    srv, handle = server
    c = Client(srv.port)
    c.send({"type": "hello", "token": "secret"})
    c.recv()
    c.sock.sendall(b"{not json\n")
    err = c.recv()
    assert err["id"] is None and "error" in err
    c.send(event(3, "ping"))
    assert c.recv()["id"] == 3


def test_server_command_disable(server):
    srv, handle = server
    c = Client(srv.port)
    c.send({"type": "hello", "token": "secret"})
    c.recv()
    c.send({"type": "disable", "token": "secret"})
    assert c.recv() == {"id": None, "command": "disable", "ok": True}
    c.send(event(4, "open", "buy"))
    assert c.recv()["results"][0]["reason"] == "disabled"
    assert handle.events == []


def test_server_unregisters_source_on_disconnect(server):
    srv, handle = server
    c1 = Client(srv.port)
    c1.send({"type": "hello", "token": "secret"})
    c1.recv()
    c1.send(event(1, "open", "buy"))
    c1.recv()
    c1.close()
    c2 = Client(srv.port)
    c2.send({"type": "hello", "token": "secret"})
    c2.recv()
    for _ in range(20):  # give the server a moment to notice the disconnect
        c2.send(event(2, "close"))
        ack = c2.recv()
        if ack["results"][0]["status"] == "ok":
            break
    assert ack["results"][0]["status"] == "ok"
