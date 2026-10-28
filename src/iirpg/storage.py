"""An append-only queue of JSON envelopes, persisted transactionally in SQLite."""

from contextlib import closing, contextmanager
import json
from pathlib import Path
import sqlite3
from typing import Iterator
from uuid import uuid4

from .domain import Message

PAGE_SIZE = 200


class MessageConflict(ValueError):
    """The same request identifier was reused for different content."""


class ChatStore:
    def __init__(self, database: Path) -> None:
        self.database = database
        database.parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS rooms (
                    id TEXT PRIMARY KEY,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS queue_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    room_id TEXT NOT NULL REFERENCES rooms(id),
                    request_id TEXT NOT NULL,
                    envelope TEXT NOT NULL,
                    UNIQUE(room_id, request_id)
                );
                CREATE INDEX IF NOT EXISTS queue_events_room_id
                    ON queue_events(room_id, id);
            """)
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version == 0:
                legacy = connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'messages'",
                ).fetchone()
                if legacy:
                    for row in connection.execute("SELECT * FROM messages ORDER BY id").fetchall():
                        envelope = {
                            "schema_version": 1, "sender": row["sender"], "type": "chat.message",
                            "text": row["text"], "visibility": {"mode": "all", "chat_ids": []},
                            "metadata": {}, "created_at": row["created_at"],
                        }
                        connection.execute(
                            "INSERT INTO queue_events (id, room_id, request_id, envelope) VALUES (?, ?, ?, ?)",
                            (row["id"], row["room_id"], row["request_id"], json.dumps(envelope, ensure_ascii=False)),
                        )
                connection.execute("PRAGMA user_version = 1")

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        with closing(sqlite3.connect(self.database, timeout=10)) as connection:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            with connection:
                yield connection

    def create_room(self) -> str:
        room_id = str(uuid4())
        with self._connection() as connection:
            connection.execute("INSERT INTO rooms (id) VALUES (?)", (room_id,))
        return room_id

    @staticmethod
    def _require_room(connection: sqlite3.Connection, room_id: str) -> None:
        if not connection.execute("SELECT 1 FROM rooms WHERE id = ?", (room_id,)).fetchone():
            raise KeyError("Комната не найдена.")

    @staticmethod
    def _decode(row: sqlite3.Row) -> dict:
        return {"choices": [], **json.loads(row["envelope"]), "id": row["id"],
                "room_id": row["room_id"], "request_id": row["request_id"]}

    def page(self, room_id: str, after: int = 0) -> dict:
        with self._connection() as connection:
            self._require_room(connection, room_id)
            rows = connection.execute(
                "SELECT id, room_id, request_id, envelope FROM queue_events "
                "WHERE room_id = ? AND id > ? ORDER BY id LIMIT ?",
                (room_id, after, PAGE_SIZE + 1),
            ).fetchall()
        batch = rows[:PAGE_SIZE]
        return {"messages": [self._decode(row) for row in batch],
                "next_cursor": batch[-1]["id"] if batch else after,
                "has_more": len(rows) > PAGE_SIZE}

    def append(self, room_id: str, request_id: str, message: Message) -> dict:
        with self._connection() as connection:
            self._require_room(connection, room_id)
            connection.execute(
                "INSERT INTO queue_events (room_id, request_id, envelope) VALUES (?, ?, ?) "
                "ON CONFLICT(room_id, request_id) DO NOTHING",
                (room_id, request_id, json.dumps(message.to_dict(), ensure_ascii=False, allow_nan=False)),
            )
            row = connection.execute(
                "SELECT id, room_id, request_id, envelope FROM queue_events "
                "WHERE room_id = ? AND request_id = ?", (room_id, request_id),
            ).fetchone()
            original = self._decode(row)
            expected = message.to_dict()
            if any(original[key] != expected[key] for key in
                   ("sender", "type", "text", "visibility", "metadata", "choices", "schema_version")):
                raise MessageConflict("Идентификатор сообщения уже использован.")
            return original
