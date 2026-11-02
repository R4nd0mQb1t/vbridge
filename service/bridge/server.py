"""asyncio TCP server speaking the NDJSON protocol; one Router behind it."""
from __future__ import annotations

import asyncio
import json
import logging
import threading
import uuid

from bridge.protocol import Command, Event, Hello, ProtocolError, dump_ack, dump_error, parse_line
from bridge.router import Router

log = logging.getLogger("bridge.server")

MAX_LINE = 64 * 1024


async def _handle_conn(router: Router, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    conn_id = uuid.uuid4().hex[:8]
    peer = writer.get_extra_info("peername")
    loop = asyncio.get_running_loop()
    try:
        first = await reader.readline()
        try:
            hello = parse_line(first.decode("utf-8", "replace").rstrip("\r\n")) if first else None
        except ProtocolError:
            hello = None
        if not isinstance(hello, Hello) or not router.authenticate(hello):
            if isinstance(hello, Hello):
                got, want = hello.token or "", router.cfg.token or ""
                log.warning("conn %s from %s: hello token mismatch (got %s..%s len %d, want %s..%s len %d), closing",
                            conn_id, peer, got[:4], got[-2:], len(got), want[:4], want[-2:], len(want))
            else:
                log.warning("conn %s from %s: no valid hello (first line %d bytes), closing", conn_id, peer,
                            len(first or b""))
            return
        writer.write(b'{"id":null,"hello":"ok"}\n')
        await writer.drain()
        log.info("conn %s from %s: authenticated", conn_id, peer)
        while True:
            raw = await reader.readline()
            if not raw:
                break
            if len(raw) > MAX_LINE:
                writer.write(dump_error("line too long").encode())
                await writer.drain()
                continue
            line = raw.decode("utf-8", "replace").rstrip("\r\n")
            if not line:
                continue
            try:
                msg = parse_line(line)
            except ProtocolError as exc:
                writer.write(dump_error(str(exc)).encode())
                await writer.drain()
                continue
            if isinstance(msg, Hello):
                writer.write(b'{"id":null,"hello":"ok"}\n')
            elif isinstance(msg, Command):
                ok = router.command(msg)
                writer.write((json.dumps({"id": None, "command": msg.name, "ok": ok}) + "\n").encode())
            else:
                ack = await loop.run_in_executor(None, router.dispatch, conn_id, msg)
                writer.write(dump_ack(ack).encode())
            await writer.drain()
    except (ConnectionResetError, asyncio.IncompleteReadError, BrokenPipeError):
        pass
    except Exception:  # noqa: BLE001
        log.exception("conn %s: unexpected error", conn_id)
    finally:
        router.unregister(conn_id)
        log.info("conn %s closed", conn_id)
        try:
            writer.close()
        except Exception:  # noqa: BLE001
            pass


async def serve(router: Router, host: str, port: int) -> asyncio.base_events.Server:
    server = await asyncio.start_server(lambda r, w: _handle_conn(router, r, w), host, port)
    bound = server.sockets[0].getsockname()
    log.info("listening on %s:%s", bound[0], bound[1])
    return server


class BridgeServer:
    """Runs serve() on its own event loop in a background thread. start()/stop() for tests and __main__."""

    def __init__(self, router: Router, host: str, port: int) -> None:
        self.router = router
        self.host = host
        self.port = port
        self._loop: asyncio.AbstractEventLoop | None = None
        self._server: asyncio.base_events.Server | None = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="bridge-server", daemon=True)
        self._thread.start()
        if not self._ready.wait(5):
            raise RuntimeError("server did not start")

    def _run(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._server = self._loop.run_until_complete(serve(self.router, self.host, self.port))
        self.port = self._server.sockets[0].getsockname()[1]
        self._ready.set()
        try:
            self._loop.run_forever()
        finally:
            self._server.close()
            self._loop.run_until_complete(self._server.wait_closed())
            self._loop.close()

    def stop(self) -> None:
        if self._loop and self._loop.is_running():
            self._loop.call_soon_threadsafe(self._loop.stop)
        if self._thread:
            self._thread.join(5)
