"""Room-scoped characters, players and items in the existing SQLite database."""

import json
import sqlite3
from uuid import UUID

from .character_sheet import CharacterRules
from .roster import InventoryRules, apply_command, integer
from .storage import ChatStore, MessageConflict


def encode(value: object, *, sort_keys: bool = True) -> str:
    return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=sort_keys)


class RosterStore:
    def __init__(self, store: ChatStore, rules: InventoryRules, character_rules: CharacterRules | None = None) -> None:
        self.store = store
        self.rules = rules
        self.character_rules = character_rules or CharacterRules.load()
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
            connection.execute("BEGIN IMMEDIATE")
            columns = {row["name"] for row in connection.execute("PRAGMA table_info(room_rosters)")}
            if "character_rules" not in columns:
                connection.execute("ALTER TABLE room_rosters ADD COLUMN character_rules TEXT")

    def _load(self, connection: sqlite3.Connection, room_id: str) -> dict:
        self.store._require_room(connection, room_id)
        connection.execute(
            "INSERT INTO room_rosters (room_id, rules) VALUES (?, ?) ON CONFLICT DO NOTHING",
            (room_id, encode(self.rules.config, sort_keys=False)),
        )
        connection.execute(
            "UPDATE room_rosters SET character_rules = ? WHERE room_id = ? AND character_rules IS NULL",
            (encode(self.character_rules.config, sort_keys=False), room_id),
        )
        roster = connection.execute("SELECT * FROM room_rosters WHERE room_id = ?", (room_id,)).fetchone()
        sheet_rules = CharacterRules(json.loads(roster["character_rules"]))
        inventory_rules = InventoryRules(json.loads(roster["rules"]))
        players = [dict(row) for row in connection.execute(
            "SELECT id, name FROM players WHERE room_id = ? ORDER BY rowid", (room_id,),
        )]
        characters = [{**json.loads(row["body"]), "id": row["id"], "items": []} for row in connection.execute(
            "SELECT id, body FROM characters WHERE room_id = ? ORDER BY rowid", (room_id,),
        )]
        by_id = {character["id"]: character for character in characters}
        for character in characters:
            # Existing records gain defaults without duplicating attributes or changing IDs/revisions.
            character.update(inventory_rules.character(
                {key: value for key, value in character.items() if key not in ("id", "items")}, players, sheet_rules))
        for row in connection.execute(
            "SELECT * FROM inventory_items WHERE room_id = ? ORDER BY rowid", (room_id,),
        ):
            by_id[row["character_id"]]["items"].append({**json.loads(row["body"]), "id": row["id"]})
        return {"revision": roster["revision"], "rules": json.loads(roster["rules"]),
                "character_rules": sheet_rules.config,
                "players": players, "characters": characters}

    def read(self, room_id: str) -> dict:
        with self.store._connection() as connection:
            # A consistent snapshot, including concurrent first initialization.
            connection.execute("BEGIN IMMEDIATE")
            state = self._load(connection, room_id)
        rules = InventoryRules(state["rules"])
        sheet_rules = CharacterRules(state["character_rules"])
        for character in state["characters"]:
            character["summary"] = rules.summary(character)
            character["sheet_statistics"] = sheet_rules.statistics(character["sheet"], character["attributes"])
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
            entity_id = apply_command(state, command, InventoryRules(state["rules"]),
                                      CharacterRules(state["character_rules"]))
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
