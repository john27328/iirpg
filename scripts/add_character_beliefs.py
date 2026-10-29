"""Explicitly add the Remaster belief fields to saved room sheet configurations."""

import argparse
from contextlib import closing
from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
from uuid import uuid4

from iirpg.character_sheet import CharacterRules

BELIEF_FIELDS = ("edicts", "anathema")


def upgrade_snapshot(snapshot: dict, target: dict) -> dict:
    """Only add this feature; preserve existing fields, formulas and customizations."""
    if snapshot.get("id") != target["id"]:
        return deepcopy(snapshot)
    result = deepcopy(snapshot)
    group = target["profiles"]["pc"]["fields"]["edicts"]["group"]
    if group not in result["groups"]:
        groups = {}
        for key, label in result["groups"].items():
            groups[key] = label
            if key == "identity":
                groups[group] = target["groups"][group]
        groups.setdefault(group, target["groups"][group])
        result["groups"] = groups
    for key in ("pc", "npc"):
        if key not in result["profiles"]:
            continue
        existing = result["profiles"][key]["fields"]
        for field in BELIEF_FIELDS:
            definition = target["profiles"][key]["fields"][field]
            if field in existing and existing[field] != definition:
                raise ValueError(f"Поле {key}.{field} уже определено иначе; снимок не изменён.")
        fields = {}
        additions = {field: deepcopy(target["profiles"][key]["fields"][field]) for field in BELIEF_FIELDS}
        for field, definition in existing.items():
            fields[field] = definition
            if field == "class_name":
                fields.update({key: value for key, value in additions.items() if key not in existing})
        for field, definition in additions.items():
            fields.setdefault(field, definition)
        result["profiles"][key]["fields"] = fields
    CharacterRules(result)
    return result


def migrate(database: Path, target: dict, apply: bool = False, room_id: str | None = None) -> tuple[int, Path | None]:
    """Dry-run by default; applying creates a consistent backup and bumps revisions."""
    if not database.is_file():
        raise ValueError("Файл базы не найден.")
    backup_path = None
    with closing(sqlite3.connect(database, timeout=10)) as connection:
        connection.row_factory = sqlite3.Row
        if apply:
            suffix = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid4().hex[:8]
            backup_path = database.with_name(database.stem + "-before-beliefs-" + suffix + database.suffix)
            with closing(sqlite3.connect(backup_path)) as backup:
                connection.backup(backup)
                if backup.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                    raise ValueError("Проверка резервной копии не прошла; данные не изменены.")
        with connection:
            connection.execute("BEGIN IMMEDIATE" if apply else "BEGIN")
            query = "SELECT room_id, character_rules FROM room_rosters WHERE character_rules IS NOT NULL"
            rows = connection.execute(query + (" AND room_id = ?" if room_id else ""), (room_id,) if room_id else ())
            planned = []
            for row in rows:
                snapshot = json.loads(row["character_rules"])
                upgraded = upgrade_snapshot(snapshot, target)
                if upgraded != snapshot:
                    planned.append((row["room_id"], upgraded))
            if apply:
                for room, snapshot in planned:
                    connection.execute("UPDATE room_rosters SET character_rules = ?, revision = revision + 1 WHERE room_id = ?",
                                       (json.dumps(snapshot, ensure_ascii=False, allow_nan=False), room))
    return len(planned), backup_path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, default=Path(".local/chat.sqlite3"))
    parser.add_argument("--room", help="Only upgrade this room; default is all matching snapshots")
    parser.add_argument("--apply", action="store_true", help="Create backup and apply; default only reports changes")
    arguments = parser.parse_args()
    count, backup = migrate(arguments.database, CharacterRules.load().config, arguments.apply, arguments.room)
    print(f"{'Обновлено' if arguments.apply else 'Будет обновлено'} комнат: {count}")
    if backup:
        print(f"Резервная копия: {backup}")


if __name__ == "__main__":
    main()
