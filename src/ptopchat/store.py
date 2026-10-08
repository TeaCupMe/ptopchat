"""SQLite message history and on-disk file blobs."""

from __future__ import annotations

import hashlib
import sqlite3
import threading
import uuid
from pathlib import Path
from typing import Any


def canonical_id(value: str) -> str:
    try:
        parsed = uuid.UUID(str(value))
    except (ValueError, AttributeError, TypeError) as exc:
        raise ValueError("bad id") from exc
    return str(parsed)


def display_filename(name: str) -> str:
    cleaned = str(name).replace("\\", "/").split("/")[-1].replace("\x00", "").strip()
    if not cleaned:
        cleaned = "file"
    return cleaned[:200]


class Store:
    def __init__(self, data_dir: str | Path) -> None:
        self.data_dir = Path(data_dir)
        self.files_dir = self.data_dir / "files"
        self.files_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self.data_dir / "chat.sqlite", check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS messages (
                id TEXT PRIMARY KEY,
                ts REAL NOT NULL,
                site TEXT NOT NULL,
                nick TEXT NOT NULL,
                kind TEXT NOT NULL,
                text TEXT NOT NULL DEFAULT '',
                filename TEXT,
                size INTEGER,
                sha256 TEXT,
                file_complete INTEGER NOT NULL DEFAULT 0,
                dest TEXT NOT NULL DEFAULT ''
            )
            """
        )
        columns = {row[1] for row in self._conn.execute("PRAGMA table_info(messages)")}
        if "dest" not in columns:
            self._conn.execute("ALTER TABLE messages ADD COLUMN dest TEXT NOT NULL DEFAULT ''")
        self._conn.execute("CREATE INDEX IF NOT EXISTS idx_messages_ts ON messages(ts)")
        self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def insert(self, message: dict[str, Any]) -> bool:
        message_id = canonical_id(message["id"])
        with self._lock:
            cursor = self._conn.execute(
                """
                INSERT OR IGNORE INTO messages
                    (id, ts, site, nick, kind, text, filename, size, sha256, file_complete, dest)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    message_id,
                    float(message["ts"]),
                    str(message["site"]),
                    str(message["nick"]),
                    str(message["kind"]),
                    str(message.get("text") or ""),
                    message.get("filename"),
                    message.get("size"),
                    message.get("sha256"),
                    1 if message.get("file_complete") else 0,
                    str(message.get("dest") or ""),
                ),
            )
            self._conn.commit()
            return cursor.rowcount == 1

    def get(self, message_id: str) -> dict[str, Any] | None:
        message_id = canonical_id(message_id)
        with self._lock:
            row = self._conn.execute("SELECT * FROM messages WHERE id = ?", (message_id,)).fetchone()
        return _row_to_dict(row) if row is not None else None

    def recent(self, limit: int) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM messages ORDER BY ts DESC, id DESC LIMIT ?",
                (int(limit),),
            ).fetchall()
        return [_row_to_dict(row) for row in reversed(rows)]

    def ids_recent(self, limit: int) -> list[str]:
        return [message["id"] for message in self.recent(limit)]

    def incomplete_files(self, limit: int) -> list[dict[str, Any]]:
        return [
            message
            for message in self.recent(limit)
            if message["kind"] == "file" and not message["file_complete"]
        ]

    def mark_complete(self, message_id: str) -> None:
        message_id = canonical_id(message_id)
        with self._lock:
            self._conn.execute(
                "UPDATE messages SET file_complete = 1 WHERE id = ?",
                (message_id,),
            )
            self._conn.commit()

    def file_path(self, file_id: str) -> Path:
        return self.files_dir / canonical_id(file_id)

    def file_size(self, file_id: str) -> int:
        path = self.file_path(file_id)
        with self._lock:
            if not path.exists():
                return 0
            return path.stat().st_size

    def write_chunk(self, file_id: str, offset: int, data: bytes) -> str:
        """Write a contiguous chunk. Returns wrote, duplicate, or gap."""
        path = self.file_path(file_id)
        with self._lock:
            current = path.stat().st_size if path.exists() else 0
            if offset < current:
                return "duplicate"
            if offset > current:
                return "gap"
            with path.open("ab") as handle:
                handle.write(data)
            return "wrote"

    def read_chunk(self, file_id: str, offset: int, size: int) -> bytes:
        path = self.file_path(file_id)
        with self._lock:
            with path.open("rb") as handle:
                handle.seek(offset)
                return handle.read(size)

    def save_bytes(self, file_id: str, data: bytes) -> None:
        path = self.file_path(file_id)
        with self._lock:
            path.write_bytes(data)

    def truncate(self, file_id: str) -> None:
        path = self.file_path(file_id)
        with self._lock:
            path.write_bytes(b"")

    def sha256(self, file_id: str) -> str:
        path = self.file_path(file_id)
        digest = hashlib.sha256()
        with self._lock:
            with path.open("rb") as handle:
                while True:
                    block = handle.read(1024 * 1024)
                    if not block:
                        break
                    digest.update(block)
        return digest.hexdigest()


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "ts": row["ts"],
        "site": row["site"],
        "nick": row["nick"],
        "kind": row["kind"],
        "text": row["text"],
        "filename": row["filename"],
        "size": row["size"],
        "sha256": row["sha256"],
        "file_complete": bool(row["file_complete"]),
        "dest": row["dest"] if "dest" in row.keys() else "",
    }
