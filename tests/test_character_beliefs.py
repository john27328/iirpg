"""Optional Remaster beliefs and explicit, non-destructive snapshot upgrades."""

from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from uuid import uuid4

from iirpg.character_sheet import CharacterRules
from iirpg.roster import InventoryRules
from iirpg.roster_storage import RosterStore
from iirpg.storage import ChatStore, MessageConflict
from scripts.add_character_beliefs import migrate, upgrade_snapshot


class CharacterBeliefsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.database = Path(self.directory.name) / "game.sqlite3"
        self.target = CharacterRules.load().config
        self.old = deepcopy(self.target)
        self.old["groups"].pop("beliefs")
        for profile in self.old["profiles"].values():
            for field in ("edicts", "anathema"):
                profile["fields"].pop(field)
        self.old["profiles"]["pc"]["fields"]["speed"]["default"] = 30
        self.chat = ChatStore(self.database)
        self.store = RosterStore(self.chat, InventoryRules.load(), CharacterRules(self.old))
        self.room = self.chat.create_room()

    def command(self, action: str, data: dict, **extra: str) -> dict:
        state = self.store.read(self.room)
        return self.store.execute(self.room, {"action": action, "revision": state["revision"], "data": data, **extra}, str(uuid4()))

    def test_upgrade_preserves_characters_items_custom_rules_and_retries(self) -> None:
        command = {"action": "create_character", "revision": 0, "data": {
            "name": "Герой", "attributes": {"strength": 3}, "sheet": {"values": {"hp_max": 20, "hp_current": 18}},
        }}
        request_id = str(uuid4())
        receipt = self.store.execute(self.room, command, request_id)
        actor_id = receipt["entity_id"]
        self.command("add_item", {"name": "Меч", "category": "weapon", "bulk": "1", "state": "held"}, character_id=actor_id)
        before = self.store.read(self.room)
        self.assertEqual(migrate(self.database, self.target), (1, None))
        self.assertEqual(self.store.read(self.room), before)
        count, backup = migrate(self.database, self.target, apply=True)
        self.assertEqual(count, 1)
        self.assertTrue(backup.is_file())
        after = self.store.read(self.room)
        self.assertEqual(after["revision"], before["revision"] + 1)
        actor = after["characters"][0]
        self.assertEqual(actor["id"], actor_id)
        self.assertEqual(actor["items"], before["characters"][0]["items"])
        self.assertEqual(actor["attributes"], before["characters"][0]["attributes"])
        self.assertEqual(actor["sheet_statistics"], before["characters"][0]["sheet_statistics"])
        self.assertEqual(actor["sheet"]["values"]["hp_current"], 18)
        self.assertEqual(actor["sheet"]["values"]["edicts"], "")
        self.assertEqual(actor["sheet"]["values"]["anathema"], "")
        self.assertEqual(after["character_rules"]["profiles"]["pc"]["fields"]["speed"]["default"], 30)
        self.assertEqual(self.store.execute(self.room, command, request_id), receipt)
        with self.assertRaises(MessageConflict):
            self.store.execute(self.room, {"action": "update_character", "revision": before["revision"],
                               "id": actor_id, "data": {"name": "Устаревшая форма"}}, str(uuid4()))
        self.assertEqual(migrate(self.database, self.target), (0, None))
        self.assertEqual(self.store.read(self.room), after)

    def test_beliefs_are_optional_for_pc_and_npc_and_persist_after_restart(self) -> None:
        migrate(self.database, self.target, apply=True, room_id=self.room)  # No snapshot until first read.
        self.store.read(self.room)
        migrate(self.database, self.target, apply=True, room_id=self.room)
        for kind in ("pc", "npc"):
            actor_id = self.command("create_character", {"name": kind, "kind": kind})["entity_id"]
            actor = next(actor for actor in self.store.read(self.room)["characters"] if actor["id"] == actor_id)
            self.assertEqual(actor["sheet"]["values"]["edicts"], "")
            self.command("update_character", {"sheet": {"values": {
                "edicts": "Держать слово. Помогать спутникам.", "anathema": "Предавать доверившихся.",
            }}}, id=actor_id)
        before = self.store.read(self.room)
        restarted = RosterStore(ChatStore(self.database), InventoryRules.load(), CharacterRules(self.old))
        self.assertEqual(restarted.read(self.room), before)
        actor = before["characters"][0]
        with self.assertRaises(ValueError):
            self.command("update_character", {"sheet": {"values": {"edicts": "Я" * 1001}}}, id=actor["id"])
        self.assertEqual(self.store.read(self.room), before)

    def test_conflicting_fields_abort_all_changes_and_other_systems_are_untouched(self) -> None:
        self.store.read(self.room)
        conflicting = deepcopy(self.old)
        conflicting["profiles"]["pc"]["fields"]["edicts"] = {
            "label": "Свои принципы", "group": "identity", "type": "text", "max_length": 100, "default": "",
        }
        other_store = RosterStore(self.chat, InventoryRules.load(), CharacterRules(conflicting))
        other = self.chat.create_room()
        other_before = other_store.read(other)
        before = self.store.read(self.room)
        with self.assertRaises(ValueError):
            migrate(self.database, self.target, apply=True)
        self.assertEqual(self.store.read(self.room), before)
        self.assertEqual(other_store.read(other), other_before)
        unrelated = deepcopy(self.old)
        unrelated["id"] = "other-game"
        self.assertEqual(upgrade_snapshot(unrelated, self.target), unrelated)


if __name__ == "__main__":
    unittest.main()
