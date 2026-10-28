"""Room-scoped characters, players and items in the existing SQLite database."""

import json
import sqlite3
from uuid import UUID

from .roster import InventoryRules, apply_command, integer
from .storage import ChatStore, MessageConflict


def encode(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True)


class RosterStore:
    def __init__(self, store: ChatStore, rules: InventoryRules) -> None:
        self.store = store
        self.rules = rules
        with store._connection() as connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS room_rosters (
                    room_id TEXT PRIMARY KEY REFERENCES rooms(id),
                    revision INTEGER NOT NULL DEFAULT 0,
                    rules TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS players (
                    id TEXT NOT NULL,
                    room_id TEXT NOT NULL REFERENCES room_rosters(room_id),
                    name TEXT NOT NULL,
                    PRIMARY KEY (room_id, id)
                );
                CREATE TABLE IF NOT EXISTS characters (
                    id TEXT NOT NULL,
                    room_id TEXT NOT NULL REFERENCES room_rosters(room_id),
                    player_id TEXT,
                    body TEXT NOT NULL,
                    PRIMARY KEY (room_id, id),
                    FOREIGN KEY (room_id, player_id) REFERENCES players(room_id, id)
                );
                CREATE TABLE IF NOT EXISTS inventory_items (
                    id TEXT NOT NULL,
                    room_id TEXT NOT NULL,
                    character_id TEXT NOT NULL,
                    body TEXT NOT NULL,
                    PRIMARY KEY (room_id, id),
                    FOREIGN KEY (room_id, character_id) REFERENCES characters(room_id, id)
                );
                CREATE TABLE IF NOT EXISTS roster_requests (
                    room_id TEXT NOT NULL REFERENCES room_rosters(room_id),
                    request_id TEXT NOT NULL,
                    command TEXT NOT NULL,
                    response TEXT NOT NULL,
                    PRIMARY KEY (room_id, request_id)
                );
            """)

    def _load(self, connection: sqlite3.Connection, room_id: str) -> dict:
        self.store._require_room(connection, room_id)
        connection.execute(
            "INSERT INTO room_rosters (room_id, rules) VALUES (?, ?) ON CONFLICT DO NOTHING",
            (room_id, encode(self.rules.config)),
        )
        roster = connection.execute("SELECT * FROM room_rosters WHERE room_id = ?", (room_id,)).fetchone()
        players = [dict(row) for row in connection.execute(
            "SELECT id, name FROM players WHERE room_id = ? ORDER BY rowid", (room_id,),
        )]
        characters = [{**json.loads(row["body"]), "id": row["id"], "items": []} for row in connection.execute(
            "SELECT id, body FROM characters WHERE room_id = ? ORDER BY rowid", (room_id,),
        )]
        by_id = {character["id"]: character for character in characters}
        for row in connection.execute(
            "SELECT * FROM inventory_items WHERE room_id = ? ORDER BY rowid", (room_id,),
        ):
            by_id[row["character_id"]]["items"].append({**json.loads(row["body"]), "id": row["id"]})
        return {"revision": roster["revision"], "rules": json.loads(roster["rules"]),
                "players": players, "characters": characters}

    def read(self, room_id: str) -> dict:
        with self.store._connection() as connection:
            # A consistent snapshot, including concurrent first initialization.
            connection.execute("BEGIN IMMEDIATE")
            state = self._load(connection, room_id)
        rules = InventoryRules(state["rules"])
        for character in state["characters"]:
            character["summary"] = rules.summary(character)
        return state

    def execute(self, room_id: str, command: dict, request_id: str) -> dict:
        try:
            if str(UUID(request_id)) != request_id:
                raise ValueError
        except (ValueError, TypeError, AttributeError):
            raise ValueError("Нужен UUID в заголовке Idempotency-Key.") from None
        encoded = encode(command)
        with self.store._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            state = self._load(connection, room_id)
            previous = connection.execute(
                "SELECT command, response FROM roster_requests WHERE room_id = ? AND request_id = ?",
                (room_id, request_id),
            ).fetchone()
            if previous:
                if previous["command"] != encoded:
                    raise MessageConflict("Идентификатор операции уже использован с другой командой.")
                return json.loads(previous["response"])
            revision = integer(command.get("revision"), "Ревизия", 0, 9223372036854775806)
            if revision != state["revision"]:
                raise MessageConflict("Данные изменились в другом окне. Обновите список и повторите изменение.")
            entity_id = apply_command(state, command, InventoryRules(state["rules"]))
            # Replace only this room's rows within one transaction. Foreign keys also
            # enforce that ownership and inventory never cross room boundaries.
            for table in ("inventory_items", "characters", "players"):
                connection.execute(f"DELETE FROM {table} WHERE room_id = ?", (room_id,))
            for player in state["players"]:
                connection.execute("INSERT INTO players (id, room_id, name) VALUES (?, ?, ?)",
                                   (player["id"], room_id, player["name"]))
            for character in state["characters"]:
                body = {key: value for key, value in character.items() if key not in ("id", "items")}
                connection.execute("INSERT INTO characters (id, room_id, player_id, body) VALUES (?, ?, ?, ?)",
                                   (character["id"], room_id, character["player_id"], encode(body)))
                for item in character["items"]:
                    body = {key: value for key, value in item.items() if key != "id"}
                    connection.execute(
                        "INSERT INTO inventory_items (id, room_id, character_id, body) VALUES (?, ?, ?, ?)",
                        (item["id"], room_id, character["id"], encode(body)),
                    )
            response = {"revision": revision + 1, "entity_id": entity_id}
            connection.execute("UPDATE room_rosters SET revision = ? WHERE room_id = ?", (revision + 1, room_id))
            connection.execute(
                "INSERT INTO roster_requests (room_id, request_id, command, response) VALUES (?, ?, ?, ?)",
                (room_id, request_id, encoded, encode(response)),
            )
            return response
