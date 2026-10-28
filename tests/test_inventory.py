"""Inventory behavior from Player Core pp. 267–271 and 287 (local equipment.md)."""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from uuid import uuid4

from iirpg.roster import InventoryRules
from iirpg.roster_storage import RosterStore
from iirpg.storage import ChatStore, MessageConflict


class InventoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "chat.sqlite3"
        self.chat = ChatStore(self.path)
        self.rules = InventoryRules.load()
        self.store = RosterStore(self.chat, self.rules)
        self.room = self.chat.create_room()

    def command(self, action: str, data: dict | None = None, **extra: object) -> str:
        command = {"action": action, "revision": self.store.read(self.room)["revision"],
                   "data": data or {}, **extra}
        return self.store.execute(self.room, command, str(uuid4()))["entity_id"]

    def actor(self, **data: object) -> str:
        return self.command("create_character", {"name": "Герой", **data})

    def item(self, actor_id: str, **data: object) -> str:
        return self.command("add_item", {"name": "Предмет", "category": "gear", "bulk": "—", **data},
                            character_id=actor_id)

    def character(self, actor_id: str) -> dict:
        return next(entry for entry in self.store.read(self.room)["characters"] if entry["id"] == actor_id)

    def test_pc_and_npc_share_inventory_and_player_ownership(self) -> None:
        player = self.command("create_player", {"name": "Игрок"})
        pc = self.actor(player_id=player)
        npc = self.actor(name="Купец", kind="npc")
        self.item(pc, name="Кинжал", category="weapon", bulk="L")
        self.item(npc, name="Меч", category="weapon", bulk="1")
        self.assertEqual(self.character(pc)["player_id"], player)
        self.assertEqual(self.character(npc)["summary"]["bulk"], 1)
        with self.assertRaises(ValueError):
            self.actor(kind="npc", player_id=player)
        with self.assertRaises(ValueError):
            self.command("delete_player", id=player)
        self.command("update_character", {"player_id": None}, id=pc)
        self.command("delete_player", id=player)
        self.assertEqual(self.store.read(self.room)["players"], [])

    def test_light_bulk_and_coins_round_down_separately(self) -> None:
        pc = self.actor(coins=1999)
        stack = self.item(pc, bulk="L", quantity=9)
        self.assertEqual(self.character(pc)["summary"]["bulk"], 1)
        self.command("update_item", {"quantity": 11}, id=stack, character_id=pc)
        summary = self.character(pc)["summary"]
        self.assertEqual((summary["bulk"], summary["light_remainder"]), (2, 1))
        self.command("update_character", {"coins": 2000}, id=pc)
        self.assertEqual(self.character(pc)["summary"]["bulk"], 3)

    def test_encumbered_and_maximum_use_strength_and_reject_atomically(self) -> None:
        pc = self.actor(attributes={"strength": 2})
        item = self.item(pc, bulk="7")
        self.assertFalse(self.character(pc)["summary"]["encumbered"])
        self.command("update_item", {"bulk": "8"}, id=item, character_id=pc)
        self.assertTrue(self.character(pc)["summary"]["encumbered"])
        self.command("update_item", {"bulk": "12"}, id=item, character_id=pc)
        before = self.store.read(self.room)
        for action, data, extra in [
            ("update_item", {"bulk": "13"}, {"id": item, "character_id": pc}),
            ("update_character", {"attributes": {"strength": 1}}, {"id": pc}),
            ("add_item", {"name": "Лишний", "category": "gear", "bulk": "1"}, {"character_id": pc}),
        ]:
            with self.subTest(action=action), self.assertRaises(ValueError):
                self.command(action, data, **extra)
            self.assertEqual(self.store.read(self.room), before)

    def test_holding_differs_from_wielding_and_hands_cannot_be_overbooked(self) -> None:
        pc = self.actor()
        weapon = self.item(pc, name="Двуручное оружие", category="weapon", required_hands=2, state="held", hands=1)
        offhand = self.item(pc, name="Факел", state="held")
        self.assertEqual(self.character(pc)["summary"]["hands_used"], 2)
        self.assertNotIn(weapon, self.character(pc)["summary"]["ready_item_ids"])
        with self.assertRaises(ValueError):
            self.command("update_item", {"hands": 2}, id=weapon, character_id=pc)
        self.command("update_item", {"state": "worn"}, id=offhand, character_id=pc)
        self.command("update_item", {"hands": 2}, id=weapon, character_id=pc)
        self.assertIn(weapon, self.character(pc)["summary"]["ready_item_ids"])
        with self.assertRaises(ValueError):
            self.item(pc, state="held", quantity=2)
        self.command("update_item", {"state": "stowed"}, id=weapon, character_id=pc)
        self.assertEqual(self.character(pc)["summary"]["hands_used"], 0)

    def test_one_plus_usage_needs_a_free_hand(self) -> None:
        pc = self.actor()
        bow = self.item(pc, required_hands=1, free_hands=1, state="held")
        self.assertIn(bow, self.character(pc)["summary"]["ready_item_ids"])
        self.item(pc, state="held")
        self.assertNotIn(bow, self.character(pc)["summary"]["ready_item_ids"])

    def test_armor_bulk_changes_when_worn_and_only_one_suit_is_worn(self) -> None:
        pc = self.actor(attributes={"strength": 5})
        armor = self.item(pc, category="armor", bulk="4")
        self.assertEqual(self.character(pc)["summary"]["bulk"], 5)
        self.command("update_item", {"state": "worn"}, id=armor, character_id=pc)
        self.assertEqual(self.character(pc)["summary"]["bulk"], 4)
        with self.assertRaises(ValueError):
            self.item(pc, category="armor", state="worn")
        light = self.item(pc, category="armor", bulk="L")
        self.assertEqual(self.character(pc)["summary"]["bulk"], 5)
        self.command("remove_item", id=armor, character_id=pc)
        self.command("update_item", {"state": "worn"}, id=light, character_id=pc)
        self.assertEqual(self.character(pc)["summary"]["bulk"], 0)

    def test_worn_toolkits_have_two_bulk_limit(self) -> None:
        pc = self.actor()
        self.item(pc, category="toolkit", bulk="1", quantity=2, state="worn")
        with self.assertRaises(ValueError):
            self.item(pc, category="toolkit", bulk="L", state="worn")
        self.item(pc, category="toolkit", bulk="1", state="stowed")

    def test_other_room_cannot_reference_player_actor_or_item(self) -> None:
        player = self.command("create_player", {"name": "Другой игрок"})
        actor = self.actor(player_id=player)
        item = self.item(actor)
        original_room = self.room
        self.room = self.chat.create_room()
        own_actor = self.actor()
        for action, data, extra in [
            ("create_character", {"name": "Вор", "player_id": player}, {}),
            ("add_item", {"name": "Предмет", "category": "gear"}, {"character_id": actor}),
            ("update_item", {"name": "Украдено"}, {"character_id": own_actor, "id": item}),
        ]:
            with self.subTest(action=action), self.assertRaises(KeyError):
                self.command(action, data, **extra)
        self.assertEqual(len(self.store.read(original_room)["characters"][0]["items"]), 1)

    def test_concurrent_retry_and_stale_revision_do_not_duplicate_or_overwrite(self) -> None:
        command = {"action": "create_character", "revision": 0, "data": {"name": "Один"}}
        request_id = str(uuid4())
        with ThreadPoolExecutor(max_workers=4) as pool:
            replies = list(pool.map(lambda _: self.store.execute(self.room, command, request_id), range(4)))
        self.assertTrue(all(reply == replies[0] for reply in replies))
        self.assertEqual(len(self.store.read(self.room)["characters"]), 1)
        with self.assertRaises(MessageConflict):
            self.store.execute(self.room, {**command, "data": {"name": "Другой"}}, request_id)
        with self.assertRaises(MessageConflict):
            self.store.execute(self.room, command, str(uuid4()))
        self.assertEqual(self.store.read(self.room)["revision"], 1)

    def test_restart_keeps_data_requests_and_pinned_rules(self) -> None:
        pc = self.actor(kind="npc")
        self.item(pc, category="armor", bulk="4", state="worn")
        before = self.store.read(self.room)
        config = deepcopy(self.rules.config)
        config["name"] = "Другие правила"
        config["hands"] = 3
        self.store = RosterStore(ChatStore(self.path), InventoryRules(config))
        self.assertEqual(self.store.read(self.room), before)
        new_room = self.chat.create_room()
        self.assertEqual(self.store.read(new_room)["rules"]["hands"], 3)
        operation = {"action": "create_player", "revision": before["revision"], "data": {"name": "Игрок"}}
        request_id = str(uuid4())
        receipt = self.store.execute(self.room, operation, request_id)
        restarted = RosterStore(ChatStore(self.path), self.rules)
        self.assertEqual(restarted.execute(self.room, operation, request_id), receipt)

    def test_config_controls_states_categories_attributes_and_limits(self) -> None:
        config = deepcopy(self.rules.config)
        config["states"]["bag"] = config["states"].pop("stowed")
        config["default_state"] = "bag"
        config["attributes"]["might"] = config["attributes"].pop("strength")
        config["bulk"]["attribute"] = "might"
        config["bulk"]["maximum_base"] = 20
        config["categories"]["armor"]["worn_count_limit"] = 2
        config["hands"] = 3
        self.store = RosterStore(self.chat, InventoryRules(config))
        pc = self.actor(attributes={"might": 1})
        item = self.item(pc, bulk="15")
        self.assertEqual(self.character(pc)["items"][0]["state"], "bag")
        self.assertEqual(self.character(pc)["summary"]["maximum"], 21)
        self.command("remove_item", id=item, character_id=pc)
        self.item(pc, category="armor", state="worn", quantity=2)
        for _ in range(3):
            self.item(pc, state="held")
        self.assertEqual(self.character(pc)["summary"]["hands_used"], 3)

    def test_invalid_data_is_rejected_without_mutations(self) -> None:
        pc = self.actor()
        before = self.store.read(self.room)
        cases = [{"quantity": True}, {"quantity": 0}, {"quantity": 1.5}, {"bulk": "-1"},
                 {"bulk": 1}, {"bulk": "0.1"}, {"state": "missing"}, {"category": []},
                 {"hands": 1}, {"required_hands": 0}, {"notes": []}, {"name": "  "},
                 {"source_url": "javascript:alert(1)"}, {"made_up": 1}]
        for data in cases:
            with self.subTest(data=data), self.assertRaises(ValueError):
                self.item(pc, **data)
            self.assertEqual(self.store.read(self.room), before)
        for data in [{"attributes": {"strength": True}}, {"attributes": {"other": 0}},
                     {"coins": -1}, {"kind": "monster"}, {"size": "large"}]:
            with self.subTest(data=data), self.assertRaises(ValueError):
                self.command("update_character", data, id=pc)
        with self.assertRaises(ValueError):
            self.store.execute(self.room, {}, "not-a-uuid")

    def test_existing_chat_data_is_preserved_and_character_removal_cascades_items(self) -> None:
        # Initializing the new schema must leave the existing queue unchanged.
        legacy_chat = ChatStore(Path(self.directory.name) / "legacy.sqlite3")
        legacy_room = legacy_chat.create_room()
        original = legacy_chat.append_event(legacy_room, str(uuid4()), {
            "type": "chat.message", "text": "History", "choices": [],
        })
        upgraded = RosterStore(legacy_chat, self.rules)
        self.assertEqual(upgraded.read(legacy_room)["characters"], [])
        self.assertEqual(legacy_chat.page(legacy_room)["messages"], [original])
        pc = self.actor()
        self.item(pc)
        self.command("delete_character", id=pc)
        self.assertEqual(self.store.read(self.room)["characters"], [])
        with self.chat._connection() as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM inventory_items").fetchone()[0], 0)

    def test_config_rejects_unsupported_strategy_and_invalid_catalog(self) -> None:
        for change in [lambda c: c["bulk"].update(strategy="other"),
                       lambda c: c.update(hands=0),
                       lambda c: c["catalog"][0].update(category="missing")]:
            config = deepcopy(self.rules.config)
            change(config)
            with self.assertRaises(ValueError):
                InventoryRules(config)


if __name__ == "__main__":
    unittest.main()
