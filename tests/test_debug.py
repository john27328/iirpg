"""The administrative inspector is room scoped and preserves subscriber filtering."""

from pathlib import Path
import tempfile
import unittest
from uuid import uuid4

from iirpg.application import ChatService
from iirpg.debug import inspect_room
from iirpg.dice import DiceTool
from iirpg.roster import InventoryRules
from iirpg.roster_storage import RosterStore
from iirpg.storage import ChatStore, PAGE_SIZE


class DebugTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = ChatStore(Path(self.directory.name) / "chat.sqlite3")
        self.service = ChatService(self.store, DiceTool(roll_die=lambda sides: 3))
        self.roster = RosterStore(self.store, InventoryRules.load())
        self.room = self.store.create_room()

    def send(self, room: str, text: str, chat_ids: list[str] | None = None) -> dict:
        return self.service.send(room, {
            "sender": "first", "text": text, "request_id": str(uuid4()),
            "visibility": {"mode": "selected", "chat_ids": chat_ids} if chat_ids else {"mode": "all"},
        })

    def command(self, action: str, data: dict, **extra: str) -> str:
        return self.roster.execute(self.room, {
            "action": action, "data": data, "revision": self.roster.read(self.room)["revision"], **extra,
        }, str(uuid4()))["entity_id"]

    def test_full_log_contains_restricted_events_without_changing_subscriber_views(self) -> None:
        shared = self.send(self.room, "Всем")
        first_only = self.send(self.room, "Только игроку", ["first"])
        second_only = self.send(self.room, "Только ведущему", ["second"])
        first_before = self.service.messages(self.room, "first")
        second_before = self.service.messages(self.room, "second")
        snapshot = inspect_room(self.service, self.roster, self.room)
        self.assertEqual(snapshot["queue"]["messages"], [shared, first_only, second_only])
        self.assertEqual(snapshot["queue_counts"], {"chat.message": 3})
        self.assertEqual(self.service.messages(self.room, "first"), first_before)
        self.assertEqual(self.service.messages(self.room, "second"), second_before)
        self.assertNotIn(second_only, first_before["messages"])
        self.assertNotIn(first_only, second_before["messages"])

    def test_inspector_entities_preserve_ownership_items_rules_and_revision(self) -> None:
        player = self.command("create_player", {"name": "Игрок"})
        actor = self.command("create_character", {"name": "Герой", "player_id": player})
        self.command("create_character", {"name": "Купец", "kind": "npc"})
        item = self.command("add_item", {"name": "Кинжал", "category": "weapon", "bulk": "L"}, character_id=actor)
        before = self.roster.read(self.room)
        snapshot = inspect_room(self.service, self.roster, self.room)
        entities = snapshot["entities"]
        self.assertEqual(snapshot["revision"], before["revision"])
        self.assertEqual(entities["room"]["id"], self.room)
        self.assertEqual(entities["players"], [{"id": player, "name": "Игрок"}])
        self.assertEqual(entities["characters"][0]["player_id"], player)
        self.assertEqual(entities["characters"][1]["kind"], "npc")
        self.assertEqual(entities["items"][0]["character_id"], actor)
        self.assertEqual(entities["items"][0]["id"], item)
        self.assertEqual(entities["rules"], before["rules"])
        self.assertEqual([chat["id"] for chat in entities["subscribers"]], ["first", "second"])
        self.assertEqual(entities["tools"]["dice"]["name"], "dice.roll")
        self.assertEqual(self.roster.read(self.room), before)
        self.assertEqual(self.store.page(self.room)["messages"], [])

    def test_log_pages_are_ordered_room_scoped_and_include_hidden_events(self) -> None:
        other = self.store.create_room()
        messages = []
        for index in range(PAGE_SIZE + 2):
            messages.append(self.send(self.room, str(index), ["first" if index % 2 else "second"]))
            if index == 4:
                self.send(other, "Не из этой комнаты")
        page = inspect_room(self.service, self.roster, self.room)
        self.assertEqual(page["queue"]["messages"], messages[:PAGE_SIZE])
        self.assertTrue(page["queue"]["has_more"])
        next_page = inspect_room(self.service, self.roster, self.room, page["queue"]["next_cursor"])
        self.assertEqual(next_page["queue"]["messages"], messages[PAGE_SIZE:])
        self.assertFalse(next_page["queue"]["has_more"])
        self.assertEqual(next_page["queue_counts"], {"chat.message": PAGE_SIZE + 2})
        empty = inspect_room(self.service, self.roster, self.room, next_page["queue"]["next_cursor"])
        self.assertEqual(empty["queue"]["messages"], [])
        with self.assertRaises(KeyError):
            inspect_room(self.service, self.roster, str(uuid4()))

    def test_tool_call_and_result_are_separate_log_events(self) -> None:
        result = self.service.send(self.room, {
            "sender": "second", "request_id": str(uuid4()), "type": "tool.call", "tool": "dice.roll",
            "arguments": {"system": "d20", "roll": "1d20+5", "success": {"operator": ">=", "target": 10}},
        })
        snapshot = inspect_room(self.service, self.roster, self.room)
        call, logged_result = snapshot["queue"]["messages"]
        self.assertEqual(logged_result, result)
        self.assertEqual(logged_result["tool_call_id"], call["request_id"])
        self.assertEqual(snapshot["queue_counts"], {"tool.call": 1, "tool.result": 1})


if __name__ == "__main__":
    unittest.main()
