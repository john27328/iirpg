"""Characters and configurable inventory mechanics, independent of HTTP and SQLite."""

from copy import deepcopy
import json
from pathlib import Path
import re
from uuid import uuid4

DEFAULT_INVENTORY_RULES = Path(__file__).with_name("rules") / "inventory_pf2e.json"
MAX_PLAYERS = 100
MAX_CHARACTERS = 200
MAX_ITEMS = 500


def integer(value: object, label: str, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f"{label}: нужно целое число от {minimum} до {maximum}.")
    return value


def name(value: object, label: str = "Имя", limit: int = 120) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError(f"{label}: нужна непустая строка до {limit} символов.")
    return value.strip()


def member(value: object, choices: dict, label: str) -> str:
    if not isinstance(value, str) or value not in choices:
        raise ValueError(f"{label}: неизвестное значение.")
    return value


def fields(payload: dict, allowed: set[str]) -> None:
    if set(payload) - allowed:
        raise ValueError("Неизвестные поля: " + ", ".join(sorted(set(payload) - allowed)))


def find(entries: list[dict], entry_id: object) -> dict:
    for entry in entries:
        if entry["id"] == entry_id:
            return entry
    raise KeyError("Запись не найдена в этой комнате.")


class InventoryRules:
    def __init__(self, config: dict) -> None:
        self.config = deepcopy(config)
        self._validate_config()

    @classmethod
    def load(cls, path: Path = DEFAULT_INVENTORY_RULES) -> "InventoryRules":
        return cls(json.loads(path.read_text()))

    def _validate_config(self) -> None:
        c = self.config
        try:
            if c["schema_version"] != 1 or c["bulk"]["strategy"] != "pf2e":
                raise ValueError("Неподдерживаемая версия или стратегия инвентаря.")
            for key in ("id", "name", "source", "source_url", "license", "notice"):
                name(c[key], key, 1000)
            integer(c["hands"], "Руки", 1, 20)
            for key in ("states", "categories", "attributes", "sizes"):
                if not isinstance(c[key], dict) or not c[key]:
                    raise ValueError(f"Пустая конфигурация {key}.")
                for identifier in c[key]:
                    if not re.fullmatch(r"[a-z][a-z0-9_-]{0,39}", identifier):
                        raise ValueError("Некорректный ключ конфигурации.")
            member(c["default_state"], c["states"], "Состояние")
            member(c["worn_state"], c["states"], "Надетое состояние")
            member(c["default_size"], c["sizes"], "Размер")
            for label in c["sizes"].values():
                name(label)
            for state in c["states"].values():
                name(state["label"])
                if type(state["uses_hands"]) is not bool:
                    raise ValueError("uses_hands должен быть boolean.")
            if c["states"][c["worn_state"]]["uses_hands"]:
                raise ValueError("Надетое состояние не должно занимать руки.")
            for spec in c["attributes"].values():
                name(spec["label"])
                integer(spec["min"], "Минимум", -100, 100)
                integer(spec["max"], "Максимум", spec["min"], 100)
                integer(spec["default"], "По умолчанию", spec["min"], spec["max"])
            bulk = c["bulk"]
            integer(bulk["light_per_bulk"], "Light per Bulk", 1, 1000)
            integer(bulk["coins_per_bulk"], "Coins per Bulk", 1, 1000000)
            integer(bulk["encumbered_base"], "Порог нагрузки", 0, 1000)
            integer(bulk["maximum_base"], "Предел нагрузки", bulk["encumbered_base"], 1000)
            member(bulk["attribute"], c["attributes"], "Атрибут нагрузки")
            for spec in c["categories"].values():
                name(spec["label"])
                for key in ("worn_count_limit", "worn_bulk_limit", "carried_bulk_extra"):
                    if key in spec:
                        integer(spec[key], key, 0, 100000)
            ids = set()
            for item in c["catalog"]:
                identifier = name(item["id"])
                if identifier in ids:
                    raise ValueError("Повтор ID каталога.")
                ids.add(identifier)
                self.item({key: value for key, value in item.items() if key != "id"})
        except (KeyError, TypeError, AttributeError) as error:
            raise ValueError("Неполная или некорректная конфигурация инвентаря.") from error

    def bulk_units(self, value: object) -> int:
        if value == "L":
            return 1
        if value == "—":
            return 0
        if isinstance(value, str) and re.fullmatch(r"0|[1-9][0-9]{0,3}", value):
            return int(value) * self.config["bulk"]["light_per_bulk"]
        raise ValueError("Bulk: используйте целое число, L или —.")

    def item(self, payload: dict) -> dict:
        c = self.config
        fields(payload, {"name", "category", "bulk", "required_hands", "free_hands", "quantity",
                         "state", "hands", "notes", "source_url"})
        state = member(payload.get("state", c["default_state"]), c["states"], "Состояние")
        held = c["states"][state]["uses_hands"]
        bulk = payload.get("bulk", "—")
        self.bulk_units(bulk)
        notes = payload.get("notes", "")
        source = payload.get("source_url", "")
        if not isinstance(notes, str) or len(notes) > 2000:
            raise ValueError("Заметки: максимум 2000 символов.")
        if not isinstance(source, str) or len(source) > 1000 or (source and not source.startswith("https://")):
            raise ValueError("Источник: ожидается HTTPS-ссылка.")
        item = {
            "name": name(payload.get("name")),
            "category": member(payload.get("category"), c["categories"], "Категория"),
            "bulk": bulk,
            "required_hands": integer(payload.get("required_hands", 1), "Руки для использования", 1, c["hands"]),
            "free_hands": integer(payload.get("free_hands", 0), "Свободные руки для использования", 0, c["hands"]),
            "quantity": integer(payload.get("quantity", 1), "Количество", 1, 100000),
            "state": state,
            "hands": integer(payload.get("hands", 1 if held else 0), "Занятые руки", 1 if held else 0,
                             c["hands"] if held else 0),
            "notes": notes,
            "source_url": source,
        }
        if held and item["quantity"] != 1:
            raise ValueError("В руки берётся один предмет. Разделите стопку на отдельные записи.")
        if item["required_hands"] + item["free_hands"] > c["hands"]:
            raise ValueError("Для использования требуется больше рук, чем есть у персонажа.")
        return item

    def character(self, payload: dict, players: list[dict]) -> dict:
        fields(payload, {"name", "kind", "player_id", "size", "attributes", "coins", "notes"})
        kind = member(payload.get("kind", "pc"), {"pc": 1, "npc": 1}, "Тип персонажа")
        player_id = payload.get("player_id")
        if player_id is not None:
            find(players, player_id)
            if kind != "pc":
                raise ValueError("Владелец-игрок задаётся только игровому персонажу.")
        attributes = payload.get("attributes", {})
        if not isinstance(attributes, dict):
            raise ValueError("Атрибуты должны быть объектом.")
        fields(attributes, set(self.config["attributes"]))
        attributes = {
            key: integer(attributes.get(key, spec["default"]), spec["label"], spec["min"], spec["max"])
            for key, spec in self.config["attributes"].items()
        }
        notes = payload.get("notes", "")
        if not isinstance(notes, str) or len(notes) > 2000:
            raise ValueError("Заметки: максимум 2000 символов.")
        return {"name": name(payload.get("name")), "kind": kind, "player_id": player_id,
                "size": member(payload.get("size", self.config["default_size"]), self.config["sizes"], "Размер"),
                "attributes": attributes, "coins": integer(payload.get("coins", 0), "Монеты", 0, 10000000),
                "notes": notes}

    def summary(self, character: dict) -> dict:
        c = self.config
        light = c["bulk"]["light_per_bulk"]
        units = (character["coins"] // c["bulk"]["coins_per_bulk"]) * light
        used_hands = 0
        worn_counts = {}
        worn_bulk = {}
        for item in character["items"]:
            category = c["categories"][item["category"]]
            item_units = self.bulk_units(item["bulk"])
            if item["state"] == c["worn_state"]:
                worn_counts[item["category"]] = worn_counts.get(item["category"], 0) + item["quantity"]
                worn_bulk[item["category"]] = worn_bulk.get(item["category"], 0) + item_units * item["quantity"]
            elif "carried_bulk_extra" in category:
                item_units = (light if item_units <= 1 else item_units + category["carried_bulk_extra"])
            units += item_units * item["quantity"]
            used_hands += item["hands"]
        if used_hands > c["hands"]:
            raise ValueError("Недостаточно свободных рук. Сначала уберите другой предмет.")
        for key, spec in c["categories"].items():
            if worn_counts.get(key, 0) > spec.get("worn_count_limit", 100000000):
                raise ValueError(f"Превышен лимит надетых предметов: {spec['label']}.")
            if worn_bulk.get(key, 0) > spec.get("worn_bulk_limit", 100000000):
                raise ValueError(f"Превышен Bulk надетых предметов: {spec['label']}.")
        modifier = character["attributes"][c["bulk"]["attribute"]]
        bulk = units // light
        threshold = max(0, c["bulk"]["encumbered_base"] + modifier)
        maximum = max(0, c["bulk"]["maximum_base"] + modifier)
        return {"bulk": bulk, "light_remainder": units % light, "encumbered_at": threshold,
                "maximum": maximum, "encumbered": bulk > threshold,
                "over_capacity": bulk > maximum, "hands_used": used_hands,
                "hands_total": c["hands"],
                "ready_item_ids": [item["id"] for item in character["items"]
                                   if c["states"][item["state"]]["uses_hands"]
                                   and item["hands"] >= item["required_hands"]
                                   and c["hands"] - used_hands >= item["free_hands"]]}


def apply_command(state: dict, command: dict, rules: InventoryRules) -> str:
    """Mutate a transaction-local roster; the storage layer commits only on success."""
    fields(command, {"action", "revision", "id", "character_id", "data"})
    action = command.get("action")
    data = command.get("data", {})
    if not isinstance(data, dict):
        raise ValueError("data должен быть объектом.")
    if action in ("delete_player", "delete_character", "remove_item"):
        fields(data, set())
    result_id = command.get("id")
    if action == "create_player":
        fields(data, {"name"})
        if len(state["players"]) >= MAX_PLAYERS:
            raise ValueError("Достигнут лимит игроков комнаты.")
        result_id = str(uuid4())
        state["players"].append({"id": result_id, "name": name(data.get("name"))})
    elif action in ("update_player", "delete_player"):
        player = find(state["players"], result_id)
        if action == "update_player":
            fields(data, {"name"})
            player["name"] = name(data.get("name"))
        else:
            if any(actor["player_id"] == result_id for actor in state["characters"]):
                raise ValueError("Сначала отвяжите персонажей от игрока.")
            state["players"].remove(player)
    elif action == "create_character":
        if len(state["characters"]) >= MAX_CHARACTERS:
            raise ValueError("Достигнут лимит персонажей комнаты.")
        character = rules.character(data, state["players"])
        result_id = str(uuid4())
        state["characters"].append({**character, "id": result_id, "items": []})
    elif action in ("update_character", "delete_character"):
        character = find(state["characters"], result_id)
        if action == "delete_character":
            state["characters"].remove(character)
        else:
            original = {key: value for key, value in character.items() if key not in ("id", "items")}
            character.update(rules.character({**original, **data}, state["players"]))
    elif action in ("add_item", "update_item", "remove_item"):
        character = find(state["characters"], command.get("character_id"))
        if action == "add_item":
            if len(character["items"]) >= MAX_ITEMS:
                raise ValueError("Достигнут лимит предметов персонажа.")
            result_id = str(uuid4())
            character["items"].append({**rules.item(data), "id": result_id})
        else:
            item = find(character["items"], result_id)
            if action == "remove_item":
                character["items"].remove(item)
            else:
                original = {key: value for key, value in item.items() if key != "id"}
                # A state change releases hands automatically unless a new grip is supplied.
                if "state" in data and "hands" not in data:
                    original.pop("hands")
                item.update(rules.item({**original, **data}))
    else:
        raise ValueError("Неизвестная команда персонажей.")
    for character in state["characters"]:
        if rules.summary(character)["over_capacity"]:
            raise ValueError("Превышен максимальный Bulk персонажа.")
    return result_id
