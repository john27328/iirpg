"""Generic dice arithmetic and criteria; system presets live in a rules file."""

from dataclasses import dataclass
import json
import operator
from pathlib import Path
import re
import secrets
from typing import Callable

DEFAULT_RULES = Path(__file__).with_name("rules") / "dice_systems.json"
COMPARATORS = {">=": operator.ge, ">": operator.gt, "<=": operator.le,
               "<": operator.lt, "==": operator.eq, "!=": operator.ne}
SYMBOLS = {">=": "≥", ">": ">", "<=": "≤", "<": "<", "==": "=", "!=": "≠"}
EXPRESSION = re.compile(r"[+-]?(?:\d*d\d+|\d+)(?:[+-](?:\d*d\d+|\d+))*\Z")
TERM = re.compile(r"([+-]?)(?:(\d*)d(\d+)|(\d+))")


@dataclass(frozen=True, slots=True)
class DiceTerm:
    sign: int
    count: int
    sides: int


@dataclass(frozen=True, slots=True)
class DiceRequest:
    arguments: dict
    terms: tuple[DiceTerm, ...]
    modifier: int
    aggregation: str
    system_label: str


def parse_roll(value: object) -> tuple[str, tuple[DiceTerm, ...], int]:
    if not isinstance(value, str) or len(value) > 120:
        raise ValueError("Формула броска должна быть строкой до 120 символов.")
    expression = "".join(value.lower().split())
    if not EXPRESSION.fullmatch(expression):
        raise ValueError("Формат броска: 1d20+5, 2d6 или 2d6+1d4-2.")
    terms = []
    modifier = 0
    matches = list(TERM.finditer(expression))
    if len(matches) > 12:
        raise ValueError("Слишком много слагаемых в формуле.")
    for match in matches:
        sign = -1 if match[1] == "-" else 1
        if match[3] is not None:
            count, sides = int(match[2] or "1"), int(match[3])
            if not 1 <= count <= 100 or not 2 <= sides <= 10000:
                raise ValueError("Допускаются 1–100 костей с 2–10000 гранями.")
            terms.append(DiceTerm(sign, count, sides))
        else:
            modifier += sign * int(match[4])
    if not terms or sum(term.count for term in terms) > 100 or abs(modifier) > 10000:
        raise ValueError("Нужна хотя бы одна кость; максимум 100 костей и модификатор ±10000.")
    return expression, tuple(terms), modifier


def condition(value: object) -> dict:
    if not isinstance(value, dict) or set(value) != {"operator", "target"}:
        raise ValueError("Критерий должен содержать operator и target.")
    comparison, target = value["operator"], value["target"]
    if not isinstance(comparison, str) or comparison not in COMPARATORS:
        raise ValueError("Операторы успеха: >=, >, <=, <, ==, !=.")
    if type(target) is not int or abs(target) > 1000000:
        raise ValueError("Порог должен быть целым числом в пределах ±1000000.")
    return {"operator": comparison, "target": target}


def matches(value: int, criterion: dict) -> bool:
    return COMPARATORS[criterion["operator"]](value, criterion["target"])


class DiceTool:
    def __init__(self, rules_path: Path = DEFAULT_RULES,
                 roll_die: Callable[[int], int] | None = None) -> None:
        self.roll_die = roll_die or (lambda sides: secrets.randbelow(sides) + 1)
        content = json.loads(rules_path.read_text())
        if not isinstance(content, dict) or content.get("version") != 1:
            raise ValueError("Неизвестная версия форматов костей.")
        systems = content.get("systems")
        if not isinstance(systems, dict) or not 1 <= len(systems) <= 32:
            raise ValueError("В правилах требуется от 1 до 32 форматов костей.")
        self.systems = {}
        for key, definition in systems.items():
            if not isinstance(key, str) or not re.fullmatch(r"[a-zA-Z0-9_.-]{1,64}", key):
                raise ValueError("Некорректный идентификатор системы костей.")
            if not isinstance(definition, dict) or set(definition) != {"label", "aggregation", "roll", "success"}:
                raise ValueError("Формат должен содержать label, aggregation, roll и success.")
            if definition.get("aggregation") not in ("sum", "count"):
                raise ValueError("Агрегация костей: sum или count.")
            label = definition.get("label")
            if not isinstance(label, str) or not label.strip() or len(label) > 80:
                raise ValueError("Требуется название формата до 80 символов.")
            self.systems[key] = definition
            self.prepare({"system": key, "roll": definition.get("roll"),
                          "success": definition.get("success")})

    def prepare(self, payload: object) -> DiceRequest:
        if not isinstance(payload, dict) or set(payload) - {"system", "roll", "success", "label"}:
            raise ValueError("Аргументы костей: system, roll, success и необязательный label.")
        system = payload.get("system")
        if not isinstance(system, str) or system not in self.systems:
            raise ValueError("Неизвестный формат костей. Список доступен в /api/tools/dice.")
        definition = self.systems[system]
        expression, terms, modifier = parse_roll(payload.get("roll", definition["roll"]))
        success = payload.get("success")
        if not isinstance(success, dict) or set(success) - {"operator", "target", "die"}:
            raise ValueError("Задайте критерий success с operator и target.")
        criterion = condition({key: success[key] for key in ("operator", "target") if key in success})
        if definition["aggregation"] == "count":
            criterion["die"] = condition(success.get("die", definition["success"].get("die")))
        elif "die" in success:
            raise ValueError("Критерий отдельной кости используется только для пула.")
        label = payload.get("label", "Бросок костей")
        if not isinstance(label, str) or not label.strip() or len(label) > 100:
            raise ValueError("Название проверки должно содержать 1–100 символов.")
        return DiceRequest(
            {"system": system, "roll": expression, "success": criterion, "label": label},
            terms, modifier, definition["aggregation"], definition["label"],
        )

    def execute(self, request: DiceRequest) -> dict:
        groups = []
        total = request.modifier
        for term in request.terms:
            values = [self.roll_die(term.sides) for _ in range(term.count)]
            if any(type(value) is not int or not 1 <= value <= term.sides for value in values):
                raise RuntimeError("Генератор вернул недопустимый результат кости.")
            score = (sum(values) if request.aggregation == "sum" else
                     sum(matches(value, request.arguments["success"]["die"]) for value in values))
            total += term.sign * score
            groups.append({"dice": f"{term.count}d{term.sides}", "sign": term.sign,
                           "values": values, "score": score})
        success = matches(total, request.arguments["success"])
        result = {"system": request.arguments["system"], "system_label": request.system_label,
                  "aggregation": request.aggregation, "roll": request.arguments["roll"],
                  "rolls": groups, "modifier": request.modifier, "result": total,
                  "success": success, "criterion": request.arguments["success"]}
        parts = []
        for group in groups:
            values = ", ".join(map(str, group["values"]))
            sign = "−" if group["sign"] == -1 else ("+" if parts else "")
            parts.append(f"{sign}[{values}]")
        if request.modifier:
            parts.append(f"{'+' if request.modifier > 0 else '−'} {abs(request.modifier)}")
        details = " ".join(parts)
        criterion = result["criterion"]
        calculation = f"{result['roll']} → {details} = {total}"
        if request.aggregation == "count":
            die = criterion["die"]
            calculation = (f"{result['roll']} → {details}\n"
                           f"Счёт успешных костей ({SYMBOLS[die['operator']]} {die['target']}), "
                           f"с модификатором: {total}.")
        result["summary"] = (
            f"{request.arguments['label']} · {request.system_label}\n"
            f"{calculation}\n"
            f"Нужно {SYMBOLS[criterion['operator']]} {criterion['target']} — "
            f"{'успех' if success else 'неудача'}."
        )
        return result

    def roll(self, payload: dict) -> dict:
        return self.execute(self.prepare(payload))

    def describe(self) -> dict:
        comparison = {"type": "object", "properties": {
            "operator": {"type": "string", "enum": list(COMPARATORS)},
            "target": {"type": "integer", "minimum": -1000000, "maximum": 1000000},
        }, "required": ["operator", "target"], "additionalProperties": False}
        return {"name": "dice.roll", "description": "Бросить кости и проверить критерий успеха.",
                "systems": self.systems, "input_schema": {
                    "type": "object", "properties": {
                        "system": {"type": "string", "enum": list(self.systems)},
                        "roll": {"type": "string", "maxLength": 120},
                        "success": {**comparison, "properties": {**comparison["properties"], "die": comparison}},
                        "label": {"type": "string", "minLength": 1, "maxLength": 100},
                    }, "required": ["system", "success"], "additionalProperties": False}}
