"""Small GM commands, separate from queue envelopes and transport identifiers."""

COMMON_FIELDS = {"action", "audience", "metadata"}
DICE_FIELDS = {"roll", "target", "operator", "system", "label", "die_target", "die_operator"}
ACTION_FIELDS = {
    "say": {"text", "choices"},
    "offer_roll": DICE_FIELDS | {"text"},
    "roll": DICE_FIELDS,
}
AUDIENCES = {"all": None, "player": "first", "gm": "second"}


def command_payload(command: dict, request_id: str) -> dict:
    """Add the actor and transport ID around a validated, model-written command."""
    if not isinstance(command, dict):
        raise ValueError("Команда должна быть объектом JSON.")
    action = command.get("action", "say")
    if not isinstance(action, str) or action not in ACTION_FIELDS:
        raise ValueError("action: say, offer_roll или roll.")
    unknown = set(command) - COMMON_FIELDS - ACTION_FIELDS[action]
    if unknown:
        raise ValueError("Неизвестные поля команды: " + ", ".join(sorted(unknown)) + ".")
    audience = command.get("audience", "all")
    if not isinstance(audience, str) or audience not in AUDIENCES:
        raise ValueError("audience: all, player или gm.")
    target_chat = AUDIENCES[audience]
    payload = {"sender": "second", "request_id": request_id,
               "visibility": {"mode": "selected" if target_chat else "all",
                              "chat_ids": [target_chat] if target_chat else []}}
    if "metadata" in command:
        payload["metadata"] = command["metadata"]
    if action == "say":
        return {**payload, "text": command.get("text"), "choices": command.get("choices", [])}
    if "roll" not in command or "target" not in command:
        raise ValueError("Для броска нужны roll (например, 1d20+5) и target (например, 15).")
    success = {"operator": command.get("operator", ">="), "target": command["target"]}
    if "die_operator" in command and "die_target" not in command:
        raise ValueError("Для условия на кости нужен die_target.")
    if "die_target" in command:
        success["die"] = {"operator": command.get("die_operator", ">="), "target": command["die_target"]}
    arguments = {"system": command.get("system", "d20"), "roll": command["roll"], "success": success}
    if "label" in command:
        arguments["label"] = command["label"]
    payload.update({"type": "tool.offer" if action == "offer_roll" else "tool.call",
                    "tool": "dice.roll", "arguments": arguments})
    if action == "offer_roll":
        if audience == "gm":
            raise ValueError("Предложение игроку должно быть ему видно: audience all или player.")
        payload.update({"recipient": "first", "text": command.get("text")})
    return payload


def describe_commands() -> dict:
    return {
        "description": "Команды ведущего. Служебные ID добавляет клиент, автор — ведущий, игрок — получатель предложения.",
        "schema": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": list(ACTION_FIELDS), "default": "say"},
                "text": {"type": "string", "minLength": 1, "maxLength": 2000,
                         "description": "Обязательно для say и offer_roll."},
                "choices": {"type": "array", "maxItems": 10,
                            "items": {"type": "string", "minLength": 1, "maxLength": 200}},
                "roll": {"type": "string", "maxLength": 120,
                         "description": "Формула: 1d20+5, 2d6. Обязательно для offer_roll и roll."},
                "target": {"type": "integer", "minimum": -1000000, "maximum": 1000000,
                           "description": "Порог успеха. Обязательно для offer_roll и roll."},
                "operator": {"type": "string", "enum": [">=", ">", "<=", "<", "==", "!="], "default": ">="},
                "system": {"type": "string", "default": "d20",
                           "description": "Формат из каталога /api/tools/dice. Для простых сумм можно оставить d20."},
                "label": {"type": "string", "minLength": 1, "maxLength": 100},
                "audience": {"type": "string", "enum": list(AUDIENCES), "default": "all"},
                "die_target": {"type": "integer", "minimum": -1000000, "maximum": 1000000},
                "die_operator": {"type": "string", "enum": [">=", ">", "<=", "<", "==", "!="], "default": ">="},
                "metadata": {"type": "object"},
            },
            "additionalProperties": False,
        },
        "examples": [
            {"action": "say", "text": "Перед вами закрытая дверь.", "choices": ["Осмотреться", "Постучать"]},
            {"action": "offer_roll", "text": "Проверь скрытность.", "roll": "1d20+5", "target": 15},
            {"action": "roll", "roll": "1d20+5", "target": 15},
        ],
    }
