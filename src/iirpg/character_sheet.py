"""Configurable character sheets and bounded arithmetic expressions; no game-specific code."""

from copy import deepcopy
import json
from pathlib import Path
import re
from typing import Any

DEFAULT_CHARACTER_RULES = Path(__file__).with_name("rules") / "character_pf2e.json"
IDENTIFIER = re.compile(r"[a-z][a-z0-9_]{0,39}\Z")


def _object(value: object, label: str) -> dict:
    if not isinstance(value, dict):
        raise ValueError(f"{label}: ожидается объект.")
    return value


def _integer(value: object, low: int, high: int, label: str) -> int:
    if type(value) is not int or not low <= value <= high:
        raise ValueError(f"{label}: нужно целое число от {low} до {high}.")
    return value


def _text(value: object, limit: int, label: str, required: bool = False) -> str:
    if not isinstance(value, str) or len(value) > limit or (required and not value.strip()):
        raise ValueError(f"{label}: ожидается {'непустой ' if required else ''}текст до {limit} символов.")
    return value


class CharacterRules:
    def __init__(self, config: dict) -> None:
        self.config = deepcopy(_object(config, "Правила персонажа"))
        try:
            self._validate()
        except (KeyError, TypeError, AttributeError) as error:
            raise ValueError("Неполная или некорректная конфигурация листа персонажа.") from error

    @classmethod
    def load(cls, path: Path = DEFAULT_CHARACTER_RULES) -> "CharacterRules":
        return cls(json.loads(path.read_text()))

    def _keys(self, values: object, label: str, limit: int) -> dict:
        mapping = _object(values, label)
        if not 1 <= len(mapping) <= limit or any(not IDENTIFIER.fullmatch(key) for key in mapping):
            raise ValueError(f"Некорректные ключи или размер {label}.")
        return mapping

    def _validate(self) -> None:
        c = self.config
        if type(c.get("schema_version")) is not int or c["schema_version"] != 1:
            raise ValueError("Неподдерживаемая версия листа персонажа.")
        for key in ("id", "name", "source", "source_url", "license", "notice"):
            _text(c[key], 1000, key, True)
        _text(c.get("help", ""), 2000, "Подсказка")
        self._keys(c["groups"], "Разделы", 30)
        for label in c["groups"].values():
            _text(label, 120, "Раздел", True)
        attributes = self._keys(c["attributes"], "Характеристики", 30)
        for spec in attributes.values():
            self._validate_spec(spec, attribute=True)
        profiles = self._keys(c["profiles"], "Профили", 10)
        for key, profile in profiles.items():
            _text(profile["label"], 120, "Профиль", True)
            _text(profile["description"], 2000, "Описание")
            if not isinstance(profile["kinds"], list) or not profile["kinds"] or any(kind not in ("pc", "npc") for kind in profile["kinds"]):
                raise ValueError("Профиль должен указывать допустимые типы персонажей.")
            for spec in self._keys(profile["fields"], "Поля", 250).values():
                self._validate_spec(spec)
            for stat in self._keys(profile["statistics"], "Показатели", 100).values():
                _text(stat["label"], 120, "Показатель", True)
                self._group(stat)
                if self._expression_type(stat["expression"], profile) != "integer":
                    raise ValueError("Итоговый показатель должен быть целым числом.")
                if "signed" in stat and type(stat["signed"]) is not bool:
                    raise ValueError("signed должен быть boolean.")
            constraints = profile.get("constraints", [])
            if not isinstance(constraints, list) or len(constraints) > 30:
                raise ValueError("Некорректные ограничения листа.")
            for constraint in constraints:
                if constraint["operator"] != "<=":
                    raise ValueError("Неподдерживаемое ограничение листа.")
                for side in ("left", "right"):
                    if self._expression_type(constraint[side], profile) != "integer":
                        raise ValueError("Ограничения сравнивают целые числа.")
                _text(constraint["message"], 500, "Сообщение", True)
        for kind in ("pc", "npc"):
            default = c["default_profiles"][kind]
            if default not in profiles or kind not in profiles[default]["kinds"]:
                raise ValueError("Некорректный профиль по умолчанию.")
        defaults = {key: spec["default"] for key, spec in attributes.items()}
        for key, profile in profiles.items():
            self.statistics(self.normalize({}, profile["kinds"][0], profile_id=key), defaults)

    def _group(self, spec: dict) -> None:
        if spec["group"] not in self.config["groups"]:
            raise ValueError("Неизвестный раздел листа.")

    def _validate_spec(self, spec: dict, attribute: bool = False) -> None:
        _text(spec["label"], 120, "Название поля", True)
        kind = "integer" if attribute else spec["type"]
        if not attribute:
            self._group(spec)
        if "overview" in spec and type(spec["overview"]) is not bool:
            raise ValueError("overview должен быть boolean.")
        if kind == "integer":
            _integer(spec["min"], -1000000, 1000000, "Минимум")
            _integer(spec["max"], spec["min"], 1000000, "Максимум")
        elif kind == "text":
            _integer(spec["max_length"], 1, 4000, "Длина текста")
        elif kind == "choice":
            options = self._keys(spec["options"], "Варианты", 50)
            for label in options.values():
                _text(label, 120, "Вариант", True)
        else:
            raise ValueError("Неизвестный тип поля листа.")
        self._value(spec["default"], {**spec, "type": kind})

    @staticmethod
    def _value(value: object, spec: dict) -> Any:
        if spec["type"] == "integer":
            return _integer(value, spec["min"], spec["max"], spec["label"])
        if spec["type"] == "text":
            return _text(value, spec["max_length"], spec["label"])
        if not isinstance(value, str) or value not in spec["options"]:
            raise ValueError(f"{spec['label']}: неизвестный вариант.")
        return value

    def normalize(self, value: object, kind: str, profile_id: str | None = None) -> dict:
        if not isinstance(kind, str) or kind not in ("pc", "npc"):
            raise ValueError("Неизвестный тип персонажа.")
        sheet = _object(value, "Лист персонажа")
        if set(sheet) - {"profile", "values"}:
            raise ValueError("Неизвестные поля листа персонажа.")
        profile_id = profile_id or sheet.get("profile", self.config["default_profiles"][kind])
        if not isinstance(profile_id, str) or profile_id not in self.config["profiles"]:
            raise ValueError("Неизвестный профиль листа.")
        profile = self.config["profiles"][profile_id]
        if kind not in profile["kinds"]:
            raise ValueError("Профиль листа не подходит типу персонажа. Сначала смените профиль листа.")
        values = _object(sheet.get("values", {}), "Поля листа")
        if set(values) - set(profile["fields"]):
            raise ValueError("Неизвестные поля выбранного профиля листа.")
        return {"profile": profile_id, "values": {
            key: self._value(values.get(key, spec["default"]), spec)
            for key, spec in profile["fields"].items()
        }}

    def update(self, original: dict, patch: object, kind: str) -> dict:
        patch = _object(patch, "Лист персонажа")
        values = _object(patch.get("values", {}), "Поля листа")
        if patch.get("profile", original["profile"]) != original["profile"]:
            return self.normalize(patch, kind)
        return self.normalize({**original, **patch, "values": {**original["values"], **values}}, kind)

    def _expression_type(self, expression: object, profile: dict, depth: int = 0) -> str:
        if depth > 16:
            raise ValueError("Слишком сложная формула листа.")
        if type(expression) is int:
            _integer(expression, -1000000, 1000000, "Константа")
            return "integer"
        if isinstance(expression, str):
            return "text"
        if not isinstance(expression, dict) or len(expression) != 1:
            raise ValueError("Некорректная формула листа.")
        operation, argument = next(iter(expression.items()))
        if operation == "ref":
            if not isinstance(argument, str) or len(argument.split(".")) != 2:
                raise ValueError("Некорректная ссылка в формуле.")
            source, key = argument.split(".")
            specs = self.config["attributes"] if source == "attributes" else profile["fields"] if source == "sheet" else {}
            if key not in specs:
                raise ValueError("Неизвестная ссылка в формуле.")
            return "integer" if source == "attributes" or specs[key]["type"] == "integer" else "text"
        if not isinstance(argument, list) or not 1 <= len(argument) <= 16:
            raise ValueError("Некорректные аргументы формулы.")
        check = lambda value: self._expression_type(value, profile, depth + 1)
        if operation in ("add", "min", "max", "multiply"):
            if any(check(value) != "integer" for value in argument):
                raise ValueError("Арифметика допускает только целые числа.")
            return "integer"
        if operation == "lookup" and len(argument) == 2:
            if check(argument[0]) != "text":
                raise ValueError("Ключ таблицы должен быть строкой.")
            table = self._keys(argument[1], "Таблица формулы", 50)
            types = {check(value) for value in table.values()}
            if len(types) != 1:
                raise ValueError("Таблица должна содержать значения одного типа.")
            return types.pop()
        if operation == "if_equal" and len(argument) == 4:
            if check(argument[0]) != check(argument[1]) or check(argument[2]) != check(argument[3]):
                raise ValueError("Типы в условии должны совпадать.")
            return check(argument[2])
        raise ValueError("Неизвестная операция формулы листа.")

    def _evaluate(self, expression: object, values: dict, attributes: dict) -> Any:
        if type(expression) is int or isinstance(expression, str):
            return expression
        operation, argument = next(iter(expression.items()))
        evaluate = lambda value: self._evaluate(value, values, attributes)
        if operation == "ref":
            source, key = argument.split(".")
            return (attributes if source == "attributes" else values)[key]
        if operation == "lookup":
            key = evaluate(argument[0])
            if key not in argument[1]:
                raise ValueError("Значение отсутствует в таблице формулы листа.")
            return evaluate(argument[1][key])
        if operation == "if_equal":
            return evaluate(argument[2] if evaluate(argument[0]) == evaluate(argument[1]) else argument[3])
        numbers = [evaluate(value) for value in argument]
        if operation == "add":
            return sum(numbers)
        if operation == "min":
            return min(numbers)
        if operation == "max":
            return max(numbers)
        result = 1
        for number in numbers:
            result *= number
        return result

    def statistics(self, sheet: dict, attributes: dict) -> dict:
        profile = self.config["profiles"][sheet["profile"]]
        for constraint in profile.get("constraints", []):
            if self._evaluate(constraint["left"], sheet["values"], attributes) > self._evaluate(constraint["right"], sheet["values"], attributes):
                raise ValueError(constraint["message"])
        return {key: self._evaluate(spec["expression"], sheet["values"], attributes)
                for key, spec in profile["statistics"].items()}
