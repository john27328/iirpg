"""An append-only queue of JSON envelopes, persisted transactionally in SQLite."""

from contextlib import closing, contextmanager
import json
from pathlib import Path
import sqlite3
from typing import Callable, Iterator
from uuid import UUID, uuid4, uuid5

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
        return self.append_event(room_id, request_id, message.to_dict())

    def event(self, room_id: str, request_id: str) -> dict:
        with self._connection() as connection:
            self._require_room(connection, room_id)
            row = connection.execute(
                "SELECT id, room_id, request_id, envelope FROM queue_events "
                "WHERE room_id = ? AND request_id = ?", (room_id, request_id),
            ).fetchone()
            if row is None:
                raise KeyError("Запись не найдена.")
            return self._decode(row)

    def append_event(self, room_id: str, request_id: str, envelope: dict) -> dict:
        with self._connection() as connection:
            self._require_room(connection, room_id)
            connection.execute(
                "INSERT INTO queue_events (room_id, request_id, envelope) VALUES (?, ?, ?) "
                "ON CONFLICT(room_id, request_id) DO NOTHING",
                (room_id, request_id, json.dumps(envelope, ensure_ascii=False, allow_nan=False)),
            )
            row = connection.execute(
                "SELECT id, room_id, request_id, envelope FROM queue_events "
                "WHERE room_id = ? AND request_id = ?", (room_id, request_id),
            ).fetchone()
            original = self._decode(row)
            stored = {key: value for key, value in original.items()
                      if key not in ("id", "room_id", "request_id", "created_at")}
            expected = {key: value for key, value in envelope.items() if key != "created_at"}
            if stored != expected:
                raise MessageConflict("Идентификатор сообщения уже использован.")
            return original

    def append_tool_exchange(self, room_id: str, request_id: str, call: dict,
                             expected: dict, generate: Callable[[], dict]) -> dict:
        """Persist call and result atomically; retries never execute the tool again."""
        result_id = str(uuid5(UUID(request_id), "tool.result"))
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._require_room(connection, room_id)
            row = connection.execute(
                "SELECT id, room_id, request_id, envelope FROM queue_events "
                "WHERE room_id = ? AND request_id = ?", (room_id, request_id),
            ).fetchone()
            if row:
                original = self._decode(row)
                # Before calls were recorded, the client ID identified the result itself.
                if original["type"] == "tool.result" and "tool_call_id" not in original:
                    if any(original.get(key) != value for key, value in expected.items()):
                        raise MessageConflict("Идентификатор запроса уже использован с другими аргументами.")
                    return original
                if any(original.get(key) != value for key, value in call.items() if key != "created_at"):
                    raise MessageConflict("Идентификатор запроса уже использован с другими аргументами.")
                result = connection.execute(
                    "SELECT id, room_id, request_id, envelope FROM queue_events "
                    "WHERE room_id = ? AND request_id = ?", (room_id, result_id),
                ).fetchone()
                if result is None:
                    raise RuntimeError("У сохранённого вызова отсутствует результат.")
                return self._decode(result)
            if connection.execute(
                "SELECT 1 FROM queue_events WHERE room_id = ? AND request_id = ?", (room_id, result_id),
            ).fetchone():
                raise MessageConflict("Идентификатор результата уже использован.")
            connection.execute(
                "INSERT INTO queue_events (room_id, request_id, envelope) VALUES (?, ?, ?)",
                (room_id, request_id, json.dumps(call, ensure_ascii=False, allow_nan=False)),
            )
            envelope = {**generate(), **expected, "tool_call_id": request_id}
            connection.execute(
                "INSERT INTO queue_events (room_id, request_id, envelope) VALUES (?, ?, ?)",
                (room_id, result_id, json.dumps(envelope, ensure_ascii=False, allow_nan=False)),
            )
            row = connection.execute(
                "SELECT id, room_id, request_id, envelope FROM queue_events "
                "WHERE room_id = ? AND request_id = ?", (room_id, result_id),
            ).fetchone()
            return self._decode(row)
