"""Text frames slip between file chunks, and transfer progress is reported."""

import asyncio
import hashlib
import uuid

from ptopchat.config import Config
from ptopchat.hub import Hub
from ptopchat.p2p import PeerLink
from ptopchat.protocol import CHUNK_SIZE
from ptopchat.store import Store


class _Store:
    def __init__(self, blob: bytes, messages: list[dict] | None = None) -> None:
        self.blob = blob
        self.messages = messages or []

    def read_chunk(self, message_id: str, offset: int, size: int) -> bytes:
        return self.blob[offset : offset + size]

    def recent(self, limit: int) -> list[dict]:
        return self.messages


def _link(store: _Store) -> PeerLink:
    return PeerLink(type("Hub", (), {"store": store})(), None, None, b"k" * 32, "b", "10.0.0.2:9")


def test_urgent_chat_goes_out_between_file_chunks():
    blob = b"x" * (CHUNK_SIZE + 3)
    link = _link(_Store(blob))
    written: list[str] = []

    async def spy(header: dict, payload: bytes = b"") -> None:
        written.append(header["type"])
        if header["type"] == "file_chunk" and written.count("file_chunk") == 1:
            await link.urgent.put(("frame", {"type": "chat"}, b""))

    link._write = spy
    asyncio.run(
        link._send_file(
            {
                "id": "f",
                "ts": 1,
                "site": "a",
                "nick": "Ann",
                "dest": "10.0.0.2:9",
                "filename": "blob.bin",
                "size": len(blob),
                "sha256": "abc",
            },
            0,
        )
    )
    assert written == ["file", "file_chunk", "chat", "file_chunk"]


def test_catchup_sends_text_before_file_bodies():
    messages = [
        {
            "id": "file-1",
            "kind": "file",
            "file_complete": True,
            "dest": "10.0.0.2:9",
            "filename": "a.bin",
            "size": 4,
        },
        {
            "id": "c1",
            "ts": 1,
            "site": "a",
            "nick": "Ann",
            "kind": "chat",
            "dest": "10.0.0.2:9",
            "text": "one",
        },
        {
            "id": "c2",
            "ts": 2,
            "site": "a",
            "nick": "Ann",
            "kind": "chat",
            "dest": "10.0.0.2:9",
            "text": "two",
        },
        {
            "id": "local",
            "ts": 3,
            "site": "a",
            "nick": "Ann",
            "kind": "chat",
            "dest": "10.0.0.1:9",
            "text": "stay",
        },
    ]
    link = _link(_Store(b"", messages))
    order: list[str] = []

    async def spy(header: dict, payload: bytes = b"") -> None:
        order.append(f"{header['type']}:{header.get('text', '')}")

    async def send_file(message: dict, offset: int) -> None:
        order.append(f"file:{message['id']}")

    link._write = spy
    link._send_file = send_file
    asyncio.run(link._send_missing(set()))
    assert order == ["chat:one", "chat:two", "file:file-1"]


def test_writer_sends_a_waiting_chat_before_a_queued_file():
    link = _link(_Store(b"abcd"))
    order: list[str] = []

    async def spy(header: dict, payload: bytes = b"") -> None:
        order.append(header["type"])

    async def send_file(message: dict, offset: int) -> None:
        order.append("file-body")
        link._stopping = True

    link._write = spy
    link._send_file = send_file

    async def run() -> None:
        await link.files.put(("file", {"id": "f"}, 0))
        await link.urgent.put(("frame", {"type": "chat"}, b""))
        await link._writer_loop()

    asyncio.run(run())
    assert order == ["chat", "file-body"]


def test_chunk_progress_is_announced_and_ref_is_not_stored(tmp_path):
    store = Store(tmp_path)
    hub = Hub(
        Config(
            site_name="a",
            name="Ada",
            address="127.0.0.1:1",
            http_host="127.0.0.1",
            http_port=1,
            p2p_host="127.0.0.1",
            p2p_port=2,
            dial=False,
            peer_addr="127.0.0.1:9",
            shared_secret="secret",
            data_dir=tmp_path,
        ),
        store,
    )
    seen: list[dict] = []

    async def broadcast(payload: dict, first: object = None) -> None:
        seen.append(payload)

    hub.broadcast = broadcast

    class Peer:
        closed = False

        def __init__(self) -> None:
            self.frames: list[dict] = []

        async def enqueue_frame(self, header: dict, payload: bytes = b"") -> None:
            self.frames.append(header)

    peer = Peer()
    raw = b"hello!!"
    file_id = str(uuid.uuid4())
    store.insert(
        {
            "id": file_id,
            "ts": 1,
            "site": "b",
            "nick": "Ben",
            "dest": "127.0.0.1:1",
            "kind": "file",
            "text": "",
            "filename": "note.txt",
            "size": len(raw),
            "sha256": hashlib.sha256(raw).hexdigest(),
            "file_complete": False,
        }
    )

    async def run() -> None:
        message = await hub.post_chat("hello", "127.0.0.1:1", ref="abc-123")
        assert seen[-1]["ref"] == "abc-123"
        assert "ref" not in store.get(message["id"])
        await hub.post_chat("nope", "127.0.0.1:1", ref="has space")
        assert "ref" not in seen[-1]
        await hub.ingest_remote({"type": "file_chunk", "id": file_id, "offset": 0}, raw[:3], peer)
        await hub.ingest_remote({"type": "file_progress", "id": file_id, "received": 3}, b"", peer)

    asyncio.run(run())
    assert peer.frames == [{"type": "file_progress", "id": file_id, "received": 3}]
    progress = [item for item in seen if item["type"] == "file_progress"]
    assert progress[0]["received"] == 3
    assert progress[0]["size"] == len(raw)
    stored = next(item for item in hub.history(10) if item["id"] == file_id)
    assert stored["received"] == 3
    store.close()


def test_outgoing_file_starts_at_zero_until_the_peer_acks(tmp_path):
    store = Store(tmp_path)
    hub = Hub(
        Config(
            site_name="a",
            name="Ada",
            address="127.0.0.1:1",
            http_host="127.0.0.1",
            http_port=1,
            p2p_host="127.0.0.1",
            p2p_port=2,
            dial=False,
            peer_addr="127.0.0.1:9",
            shared_secret="secret",
            data_dir=tmp_path,
        ),
        store,
    )
    seen: list[dict] = []

    async def broadcast(payload: dict, first: object = None) -> None:
        seen.append(payload)

    hub.broadcast = broadcast

    class Peer:
        closed = False
        peer_address = "127.0.0.1:9"

        def __init__(self) -> None:
            self.files: list[tuple] = []

        async def enqueue_file(self, message: dict, offset: int) -> None:
            self.files.append((message["id"], offset))

    hub.peer = Peer()

    async def run() -> None:
        await hub.post_file("note.txt", b"abcdef", "127.0.0.1:9")

    asyncio.run(run())
    message = seen[-1]["message"]
    assert message["file_complete"] is True
    assert message["received"] == 0
    assert hub.peer.files[0][1] == 0
    store.close()
