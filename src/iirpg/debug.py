"""Administrative inspection of existing room entities and queue events."""

from .scene import SceneStore
from .ai import AIService
from .combat import CombatStore
from .application import ChatService
from .commands import describe_commands
from .roster_storage import RosterStore


def inspect_room(service: ChatService, roster: RosterStore, room_id: str, after: int = 0, combat: CombatStore | None = None, scene: SceneStore | None = None, ai: AIService | None = None) -> dict:
    snapshot = service.store.inspect_page(room_id, after)
    state = roster.read(room_id)
    return {
        **snapshot,
        "revision": state["revision"],
        "entities": {
            "room": snapshot["room"],
            "scene": scene.read(room_id) if scene else None,
            "encounter": combat.read(room_id) if combat else None,
            "ai": {'status': ai.status(), 'requests': ai.traces(room_id)} if ai else None,
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
