"""In-process bus between browser clients and the peer link."""

from __future__ import annotations

import hashlib
import json
import logging
import time
import uuid
from typing import Any

from .config import Config
from .store import Store, display_filename

log = logging.getLogger("ptopchat.hub")

MAX_TEXT = 4000
MAX_NICK = 32


class HubError(ValueError):
    """A browser request the host will not accept."""


def clean_nick(nick: str) -> str:
    cleaned = " ".join(str(nick).split())
    if not cleaned or len(cleaned) > MAX_NICK:
        raise HubError("bad nick")
    return cleaned


def clean_text(text: str) -> str:
    cleaned = str(text).replace("\x00", "")
    if not cleaned.strip() or len(cleaned) > MAX_TEXT:
        raise HubError("bad text")
    return cleaned


class Hub:
    def __init__(self, config: Config, store: Store) -> None:
        self.config = config
        self.store = store
        self.clients: set[Any] = set()
        self.peer: Any = None
        self.peer_online = False
        self.peer_users = 0

    async def broadcast(self, payload: dict[str, Any]) -> None:
        text = json.dumps(payload, ensure_ascii=False)
        dead: list[Any] = []
        for client in list(self.clients):
            try:
                await client.send(text)
            except Exception:
                dead.append(client)
        for client in dead:
            self.clients.discard(client)

    async def note_clients_changed(self) -> None:
        await self.broadcast({"type": "presence", "local_online": len(self.clients)})
        peer = self.peer
        if peer is not None and not peer.closed:
            await peer.enqueue_frame({"type": "presence", "online": len(self.clients)})

    async def set_peer(self, peer: Any | None) -> None:
        self.peer = peer
        self.peer_online = peer is not None
        if peer is None:
            self.peer_users = 0
        await self.broadcast({"type": "peer", "online": self.peer_online, "users": self.peer_users})

    async def post_chat(self, nick: str, text: str) -> dict[str, Any]:
        message = {
            "id": str(uuid.uuid4()),
            "ts": time.time(),
            "site": self.config.site_name,
            "nick": clean_nick(nick),
            "kind": "chat",
            "text": clean_text(text),
            "filename": None,
            "size": None,
            "sha256": None,
            "file_complete": False,
        }
        self.store.insert(message)
        await self.broadcast({"type": "message", "message": message})
        await self._forward_chat(message)
        return message

    async def post_file(self, nick: str, filename: str, data: bytes) -> dict[str, Any]:
        if len(data) > self.config.max_file_bytes:
            raise HubError("file too large")
        file_id = str(uuid.uuid4())
        digest = hashlib.sha256(data).hexdigest()
        self.store.save_bytes(file_id, data)
        message = {
            "id": file_id,
            "ts": time.time(),
            "site": self.config.site_name,
            "nick": clean_nick(nick),
            "kind": "file",
            "text": "",
            "filename": display_filename(filename),
            "size": len(data),
            "sha256": digest,
            "file_complete": True,
        }
        self.store.insert(message)
        await self.broadcast({"type": "message", "message": message})
        peer = self.peer
        if peer is not None and not peer.closed:
            await peer.enqueue_file(message, 0)
        return message

    async def _forward_chat(self, message: dict[str, Any]) -> None:
        peer = self.peer
        if peer is not None and not peer.closed:
            await peer.enqueue_frame(
                {
                    "type": "chat",
                    "id": message["id"],
                    "ts": message["ts"],
                    "site": message["site"],
                    "nick": message["nick"],
                    "text": message["text"],
                }
            )

    async def ingest_remote(self, header: dict[str, Any], payload: bytes, peer: Any) -> None:
        kind = header.get("type")
        if kind == "ping":
            return
        if kind == "presence":
            try:
                self.peer_users = max(0, int(header.get("online", 0)))
            except (TypeError, ValueError):
                return
            await self.broadcast({"type": "peer", "online": True, "users": self.peer_users})
            return
        if kind == "sync":
            ids = header.get("ids")
            if not isinstance(ids, list):
                return
            await peer.enqueue_missing([str(item) for item in ids])
            return
        if kind == "file_resume":
            await peer.enqueue_resume(header)
            return
        if kind == "chat":
            await self._ingest_chat(header)
            return
        if kind == "file":
            await self._ingest_file_meta(header)
            return
        if kind == "file_chunk":
            await self._ingest_chunk(header, payload, peer)
            return
        log.debug("ignored frame %s", kind)

    async def _ingest_chat(self, header: dict[str, Any]) -> None:
        try:
            message = {
                "id": str(header["id"]),
                "ts": float(header["ts"]),
                "site": str(header["site"]),
                "nick": clean_nick(str(header["nick"])),
                "kind": "chat",
                "text": clean_text(str(header["text"])),
                "filename": None,
                "size": None,
                "sha256": None,
                "file_complete": False,
            }
        except (KeyError, TypeError, ValueError, HubError):
            log.warning("dropped remote chat frame")
            return
        if len(message["site"]) > 80:
            return
        if self.store.insert(message):
            await self.broadcast({"type": "message", "message": message})

    async def _ingest_file_meta(self, header: dict[str, Any]) -> None:
        try:
            size = int(header["size"])
            digest = str(header["sha256"]).lower()
            message_id = str(header["id"])
            nick = clean_nick(str(header["nick"]))
            site = str(header["site"])
            filename = display_filename(str(header["name"]))
            ts = float(header["ts"])
        except (KeyError, TypeError, ValueError, HubError):
            log.warning("dropped remote file frame")
            return
        if size < 0 or size > self.config.max_file_bytes:
            log.warning("dropped remote file outside size limit")
            return
        if len(digest) != 64 or any(ch not in "0123456789abcdef" for ch in digest):
            return
        if len(site) > 80:
            return
        try:
            message = {
                "id": message_id,
                "ts": ts,
                "site": site,
                "nick": nick,
                "kind": "file",
                "text": "",
                "filename": filename,
                "size": size,
                "sha256": digest,
                "file_complete": False,
            }
        except ValueError:
            return
        inserted = self.store.insert(message)
        stored = self.store.get(message["id"])
        if stored is None:
            return
        if size == 0 and digest == hashlib.sha256(b"").hexdigest():
            self.store.save_bytes(stored["id"], b"")
            self.store.mark_complete(stored["id"])
            stored = self.store.get(stored["id"])
        if inserted and stored is not None:
            await self.broadcast({"type": "message", "message": stored})

    async def _ingest_chunk(self, header: dict[str, Any], payload: bytes, peer: Any) -> None:
        try:
            message_id = str(header["id"])
            offset = int(header["offset"])
        except (KeyError, TypeError, ValueError):
            return
        if offset < 0 or len(payload) > 256 * 1024:
            return
        message = self.store.get(message_id) if _looks_like_id(message_id) else None
        if message is None or message["kind"] != "file" or message["file_complete"]:
            return
        if message["size"] is None or offset + len(payload) > message["size"]:
            return
        result = self.store.write_chunk(message_id, offset, payload)
        if result != "wrote":
            return
        current = self.store.file_size(message_id)
        if current != message["size"]:
            return
        digest = self.store.sha256(message_id)
        if digest != message["sha256"]:
            log.warning("file %s failed checksum; discarding bytes", message_id)
            self.store.truncate(message_id)
            return
        self.store.mark_complete(message_id)
        updated = self.store.get(message_id)
        if updated is not None:
            await self.broadcast({"type": "message", "message": updated})


def _looks_like_id(value: str) -> bool:
    try:
        from .store import canonical_id

        canonical_id(value)
    except ValueError:
        return False
    return True
