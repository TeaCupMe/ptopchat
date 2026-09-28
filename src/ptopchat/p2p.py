"""TCP peer link: listen or dial, then exchange encrypted frames."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import logging
import os
import time
from typing import Any

from .config import Config, split_host_port
from .hub import Hub
from .protocol import (
    CHUNK_SIZE,
    HISTORY_WINDOW,
    MAX_HELLO,
    FrameDecoder,
    ProtocolError,
    build_hello,
    mac_equal,
    pack_json,
    parse_hello,
    proof,
    seal,
    session_key,
    unpack_json,
)

log = logging.getLogger("ptopchat.p2p")

PING_INTERVAL = 20
READ_TIMEOUT = 60
HANDSHAKE_TIMEOUT = 15


async def _read_json(reader: asyncio.StreamReader) -> dict[str, Any]:
    size_raw = await reader.readexactly(4)
    size = int.from_bytes(size_raw, "big")
    if size > MAX_HELLO:
        raise ProtocolError("handshake message is too large")
    payload = await reader.readexactly(size)
    return unpack_json(size_raw + payload)


async def _write_json(writer: asyncio.StreamWriter, obj: dict[str, Any]) -> None:
    writer.write(pack_json(obj))
    await writer.drain()


async def handshake_dialer(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    secret: bytes,
    site_name: str,
) -> tuple[bytes, str]:
    dialer_nonce = os.urandom(32)
    await _write_json(
        writer,
        build_hello(role="dialer", site_name=site_name, nonce=dialer_nonce),
    )
    reply = await _read_json(reader)
    listener_site, listener_nonce, listener_mac = parse_hello(reply, "listener")
    if listener_mac is None:
        raise ProtocolError("missing peer hmac")
    expected = proof(secret, "listener", dialer_nonce, listener_nonce, site_name, listener_site)
    if not mac_equal(expected, listener_mac):
        raise ProtocolError("peer authentication failed")
    mine = proof(secret, "dialer", dialer_nonce, listener_nonce, site_name, listener_site)
    await _write_json(writer, {"type": "auth", "hmac": base64.b64encode(mine).decode("ascii")})
    return session_key(secret, dialer_nonce, listener_nonce), listener_site


async def handshake_listener(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    secret: bytes,
    site_name: str,
) -> tuple[bytes, str]:
    hello = await _read_json(reader)
    dialer_site, dialer_nonce, _mac = parse_hello(hello, "dialer")
    listener_nonce = os.urandom(32)
    listener_mac = proof(secret, "listener", dialer_nonce, listener_nonce, dialer_site, site_name)
    await _write_json(
        writer,
        build_hello(role="listener", site_name=site_name, nonce=listener_nonce, mac=listener_mac),
    )
    auth = await _read_json(reader)
    if auth.get("type") != "auth" or not isinstance(auth.get("hmac"), str):
        raise ProtocolError("expected auth")
    try:
        got = base64.b64decode(auth["hmac"], validate=True)
    except Exception as exc:
        raise ProtocolError("bad auth hmac") from exc
    expected = proof(secret, "dialer", dialer_nonce, listener_nonce, dialer_site, site_name)
    if not mac_equal(expected, got):
        raise ProtocolError("peer authentication failed")
    return session_key(secret, dialer_nonce, listener_nonce), dialer_site


class PeerLink:
    def __init__(
        self,
        hub: Hub,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        key: bytes,
        peer_site: str,
    ) -> None:
        self.hub = hub
        self.reader = reader
        self.writer = writer
        self.key = key
        self.peer_site = peer_site
        self.closed = False
        self.outbox: asyncio.Queue[Any] = asyncio.Queue()
        self._decoder = FrameDecoder(key)

    async def enqueue(self, job: Any) -> None:
        if self.closed:
            return
        await self.outbox.put(job)

    async def enqueue_frame(self, header: dict[str, Any], payload: bytes = b"") -> None:
        await self.enqueue(("frame", header, payload))

    async def enqueue_file(self, message: dict[str, Any], offset: int) -> None:
        await self.enqueue(("file", message, offset))

    async def enqueue_missing(self, ids: list[str]) -> None:
        await self.enqueue(("missing", ids))

    async def enqueue_resume(self, header: dict[str, Any]) -> None:
        await self.enqueue(("resume", header))

    async def run(self) -> None:
        writer_task = asyncio.create_task(self._writer_loop(), name="p2p-writer")
        ping_task = asyncio.create_task(self._ping_loop(), name="p2p-ping")
        await self.enqueue(("sync",))
        await self.enqueue(("presence",))
        await self.enqueue(("catchup",))
        try:
            await self._read_loop()
        finally:
            self.closed = True
            ping_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await ping_task
            self.writer.close()
            await self.outbox.put(None)
            with contextlib.suppress(Exception):
                await writer_task

    async def _ping_loop(self) -> None:
        while True:
            await asyncio.sleep(PING_INTERVAL)
            await self.enqueue_frame({"type": "ping", "ts": time.time()})

    async def _read_loop(self) -> None:
        while not self.closed:
            try:
                data = await asyncio.wait_for(self.reader.read(65536), timeout=READ_TIMEOUT)
            except TimeoutError:
                log.warning("peer %s timed out", self.peer_site)
                return
            if not data:
                return
            try:
                self._decoder.feed(data)
                while True:
                    item = self._decoder.pop()
                    if item is None:
                        break
                    header, payload = item
                    await self.hub.ingest_remote(header, payload, self)
            except ProtocolError as exc:
                log.warning("peer %s protocol error: %s", self.peer_site, exc)
                return

    async def _writer_loop(self) -> None:
        try:
            while True:
                job = await self.outbox.get()
                if job is None:
                    return
                kind = job[0]
                if kind == "frame":
                    _, header, payload = job
                    await self._write(header, payload)
                elif kind == "file":
                    _, message, offset = job
                    await self._send_file(message, int(offset))
                elif kind == "missing":
                    await self._send_missing(set(job[1]))
                elif kind == "resume":
                    await self._send_resume(job[1])
                elif kind == "sync":
                    ids = self.hub.store.ids_recent(HISTORY_WINDOW)
                    await self._write({"type": "sync", "ids": ids})
                elif kind == "presence":
                    await self._write({"type": "presence", "online": len(self.hub.clients)})
                elif kind == "catchup":
                    for message in self.hub.store.incomplete_files(HISTORY_WINDOW):
                        offset = self.hub.store.file_size(message["id"])
                        await self._write(
                            {"type": "file_resume", "id": message["id"], "offset": offset}
                        )
        except (ConnectionError, asyncio.IncompleteReadError, ProtocolError) as exc:
            log.warning("peer write failed: %s", exc)
            self.closed = True
            self.writer.close()

    async def _write(self, header: dict[str, Any], payload: bytes = b"") -> None:
        self.writer.write(seal(self.key, header, payload))
        await self.writer.drain()

    async def _send_file(self, message: dict[str, Any], offset: int) -> None:
        size = int(message["size"])
        if offset <= 0:
            await self._write(
                {
                    "type": "file",
                    "id": message["id"],
                    "ts": message["ts"],
                    "site": message["site"],
                    "nick": message["nick"],
                    "name": message["filename"],
                    "size": size,
                    "sha256": message["sha256"],
                }
            )
            offset = 0
        while offset < size:
            chunk = self.hub.store.read_chunk(message["id"], offset, CHUNK_SIZE)
            if not chunk:
                break
            await self._write({"type": "file_chunk", "id": message["id"], "offset": offset}, chunk)
            offset += len(chunk)

    async def _send_missing(self, known: set[str]) -> None:
        for message in self.hub.store.recent(HISTORY_WINDOW):
            if message["id"] in known:
                continue
            if message["kind"] == "chat":
                await self._write(
                    {
                        "type": "chat",
                        "id": message["id"],
                        "ts": message["ts"],
                        "site": message["site"],
                        "nick": message["nick"],
                        "text": message["text"],
                    }
                )
            elif message["kind"] == "file" and message["file_complete"]:
                await self._send_file(message, 0)

    async def _send_resume(self, header: dict[str, Any]) -> None:
        message_id = header.get("id")
        if not isinstance(message_id, str):
            return
        try:
            message = self.hub.store.get(message_id)
            offset = int(header.get("offset", 0))
        except (ValueError, TypeError):
            return
        if message is None or message["kind"] != "file" or not message["file_complete"]:
            return
        if offset < 0 or offset > int(message["size"]):
            return
        await self._send_file(message, offset)


class PeerManager:
    def __init__(self, hub: Hub, config: Config) -> None:
        self.hub = hub
        self.config = config
        self.active: PeerLink | None = None
        self._lock = asyncio.Lock()

    async def serve(self) -> None:
        server = await asyncio.start_server(self._on_accept, self.config.p2p_host, self.config.p2p_port)
        sockets = ", ".join(str(sock.getsockname()) for sock in server.sockets or [])
        log.info("p2p listening on %s", sockets)
        tasks = [asyncio.create_task(server.serve_forever(), name="p2p-listen")]
        if self.config.dial:
            tasks.append(asyncio.create_task(self._dial_loop(), name="p2p-dial"))
        try:
            await asyncio.gather(*tasks)
        finally:
            server.close()
            await server.wait_closed()

    async def _on_accept(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = writer.get_extra_info("peername")
        try:
            key, peer_site = await asyncio.wait_for(
                handshake_listener(reader, writer, self.config.secret_bytes, self.config.site_name),
                timeout=HANDSHAKE_TIMEOUT,
            )
        except Exception as exc:
            log.warning("incoming peer %s rejected: %s", peer, exc)
            writer.close()
            return
        link = await self._adopt(reader, writer, key, peer_site)
        if link is None:
            return
        log.info("peer channel up with %s", peer_site)
        await self._run_link(link)
        log.info("peer channel down (%s)", peer_site)

    async def _dial_loop(self) -> None:
        delay = 1.0
        assert self.config.peer_addr
        while True:
            if self.active is not None and not self.active.closed:
                await asyncio.sleep(0.5)
                continue
            host, port = split_host_port(self.config.peer_addr)
            try:
                reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout=10)
                key, peer_site = await asyncio.wait_for(
                    handshake_dialer(reader, writer, self.config.secret_bytes, self.config.site_name),
                    timeout=HANDSHAKE_TIMEOUT,
                )
                link = await self._adopt(reader, writer, key, peer_site)
                if link is None:
                    await asyncio.sleep(delay)
                    delay = min(delay * 2, 30)
                    continue
                log.info("peer channel up with %s", peer_site)
                await self._run_link(link)
                log.info("peer channel down (%s)", peer_site)
                delay = 1.0
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("dial %s failed: %s", self.config.peer_addr, exc)
                await asyncio.sleep(delay)
                delay = min(delay * 2, 30)

    async def _adopt(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        key: bytes,
        peer_site: str,
    ) -> PeerLink | None:
        async with self._lock:
            if self.active is not None and not self.active.closed:
                log.info("closing extra peer socket while %s is connected", self.active.peer_site)
                writer.close()
                return None
            link = PeerLink(self.hub, reader, writer, key, peer_site)
            self.active = link
        await self.hub.set_peer(link)
        return link

    async def _run_link(self, link: PeerLink) -> None:
        try:
            await link.run()
        finally:
            link.closed = True
            async with self._lock:
                if self.active is link:
                    self.active = None
            if self.hub.peer is link:
                await self.hub.set_peer(None)
            link.writer.close()
            with contextlib.suppress(Exception):
                await link.writer.wait_closed()
