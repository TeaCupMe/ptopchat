import uuid

from ptopchat.store import Store, display_filename


def test_insert_dedup_and_recent_order(tmp_path):
    store = Store(tmp_path)
    first = _chat("11111111-1111-1111-1111-111111111111", 1, "one")
    second = _chat("22222222-2222-2222-2222-222222222222", 2, "two")
    assert store.insert(first) is True
    assert store.insert(first) is False
    assert store.insert(second) is True
    recent = store.recent(10)
    assert [item["text"] for item in recent] == ["one", "two"]
    assert store.get(first["id"])["nick"] == "Ann"
    store.close()


def test_display_filename_drops_directories():
    assert display_filename("../../etc/passwd") == "passwd"
    assert display_filename("..\\secret.txt") == "secret.txt"
    assert display_filename("  ") == "file"


def test_file_chunks_are_contiguous(tmp_path):
    store = Store(tmp_path)
    file_id = str(uuid.uuid4())
    assert store.write_chunk(file_id, 0, b"ab") == "wrote"
    assert store.write_chunk(file_id, 2, b"cd") == "wrote"
    assert store.write_chunk(file_id, 0, b"zz") == "duplicate"
    assert store.write_chunk(file_id, 8, b"x") == "gap"
    assert store.read_chunk(file_id, 0, 10) == b"abcd"
    assert store.file_size(file_id) == 4
    store.close()


def _chat(message_id: str, ts: float, text: str) -> dict:
    return {
        "id": message_id,
        "ts": ts,
        "site": "Office A",
        "nick": "Ann",
        "kind": "chat",
        "text": text,
        "filename": None,
        "size": None,
        "sha256": None,
        "file_complete": False,
    }
