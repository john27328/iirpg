"""Administrative inspection of existing room entities and queue events."""

from .application import ChatService
from .commands import describe_commands
from .roster_storage import RosterStore


def inspect_room(service: ChatService, roster: RosterStore, room_id: str, after: int = 0) -> dict:
    snapshot = service.store.inspect_page(room_id, after)
    state = roster.read(room_id)
    return {
        **snapshot,
        "revision": state["revision"],
        "entities": {
            "room": snapshot["room"],
            "players": state["players"],
            "characters": [{key: value for key, value in character.items() if key != "items"}
                           for character in state["characters"]],
            "items": [{**item, "character_id": character["id"]}
                      for character in state["characters"] for item in character["items"]],
            "rules": state["rules"],
            "character_rules": state["character_rules"],
            "subscribers": [{"id": chat.key, "name": chat.name} for chat in service.chats.values()],
            "tools": {"dice": service.dice.describe(), "gm_commands": describe_commands()},
        },
    }
