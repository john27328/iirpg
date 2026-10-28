"""Configurable sheets; PC calculations follow Player Core pp. 25–27."""

from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from uuid import uuid4

from iirpg.character_sheet import CharacterRules
from iirpg.roster import InventoryRules
from iirpg.roster_storage import RosterStore
from iirpg.storage import ChatStore, MessageConflict


class CharacterSheetTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.database = Path(self.directory.name) / "game.sqlite3"
        self.chat = ChatStore(self.database)
        self.rules = CharacterRules.load()
        self.inventory = InventoryRules.load()
        self.store = RosterStore(self.chat, self.inventory, self.rules)
        self.room = self.chat.create_room()

    def execute(self, action: str, data: dict | None = None, **extra: str) -> str:
        return self.store.execute(self.room, {
            "action": action, "revision": self.state()["revision"], "data": data or {}, **extra,
        }, str(uuid4()))["entity_id"]

    def state(self) -> dict:
        return self.store.read(self.room)

    def actor(self, **data: object) -> str:
        return self.execute("create_character", {"name": "Герой", **data})

    def character(self, identifier: str) -> dict:
        return next(actor for actor in self.state()["characters"] if actor["id"] == identifier)

    def test_pc_and_npc_default_profiles_use_the_same_attributes_and_inventory(self) -> None:
        pc = self.actor(attributes={"strength": 3, "wisdom": 2})
        npc = self.actor(kind="npc", name="Страж")
        self.assertEqual(self.character(pc)["sheet"]["profile"], "pc")
        self.assertEqual(self.character(npc)["sheet"]["profile"], "npc")
        self.assertEqual(len(self.character(pc)["attributes"]), 6)
        self.assertEqual(self.character(pc)["summary"]["encumbered_at"], 8)
        self.execute("add_item", {"name": "Кинжал", "category": "weapon", "bulk": "L"}, character_id=npc)
        self.assertEqual(len(self.character(npc)["items"]), 1)

    def test_pc_proficiency_and_ability_calculations(self) -> None:
        actor = self.actor(attributes={"strength": 4, "dexterity": 3, "constitution": 2, "wisdom": 1},
                           sheet={"values": {"level": 5, "athletics_rank": "expert", "athletics_other": 1,
                                  "fortitude_rank": "trained", "reflex_rank": "master", "perception_rank": "legendary"}})
        stats = self.character(actor)["sheet_statistics"]
        self.assertEqual(stats["athletics"], 14)  # 4 Str + 5 level + 4 expert + 1 other
        self.assertEqual(stats["fortitude"], 9)
        self.assertEqual(stats["reflex"], 14)
        self.assertEqual(stats["perception"], 14)
        self.assertEqual(stats["will"], 1)  # untrained does not add level
        self.assertEqual(stats["stealth"], 3)

    def test_armor_dexterity_cap_and_class_dc(self) -> None:
        actor = self.actor(attributes={"dexterity": 4, "charisma": 3}, sheet={"values": {
            "level": 3, "armor_rank": "trained", "armor_item_bonus": 3,
            "armor_dex_cap": 1, "armor_other": 1,
            "key_attribute": "charisma", "class_dc_rank": "expert", "class_dc_other": -1,
        }})
        stats = self.character(actor)["sheet_statistics"]
        self.assertEqual(stats["armor_class"], 20)  # 10 + 1 capped Dex + 5 proficiency + 3 + 1
        self.assertEqual(stats["class_dc"], 19)
        self.execute("update_character", {"attributes": {"dexterity": -2}}, id=actor)
        self.assertEqual(self.character(actor)["sheet_statistics"]["armor_class"], 17)
        self.assertEqual(self.character(actor)["attributes"]["charisma"], 3)

    def test_npc_stat_block_values_do_not_add_level_or_attribute_again(self) -> None:
        actor = self.actor(kind="npc", attributes={"dexterity": 5, "wisdom": 3}, sheet={"values": {
            "level": 8, "armor_class": 26, "reflex": 19, "perception": 18, "athletics": 21,
        }})
        self.assertEqual(self.character(actor)["sheet_statistics"]["reflex"], 19)
        self.assertEqual(self.character(actor)["sheet_statistics"]["armor_class"], 26)
        self.execute("update_character", {"attributes": {"dexterity": 6}, "sheet": {"values": {"level": 9}}}, id=actor)
        stats = self.character(actor)["sheet_statistics"]
        self.assertEqual((stats["reflex"], stats["perception"], stats["athletics"]), (19, 18, 21))
        self.assertNotIn("class_dc", stats)

    def test_npc_can_use_pc_style_profile_and_pc_cannot_use_npc_profile(self) -> None:
        npc = self.actor(kind="npc", sheet={"profile": "pc", "values": {"level": 4, "athletics_rank": "trained"}},
                         attributes={"strength": 3})
        self.assertEqual(self.character(npc)["sheet_statistics"]["athletics"], 9)
        pc = self.actor()
        with self.assertRaises(ValueError):
            self.execute("update_character", {"sheet": {"profile": "npc"}}, id=pc)
        self.execute("update_character", {"sheet": {"profile": "npc", "values": {"athletics": 18}}}, id=npc)
        self.assertEqual(self.character(npc)["sheet_statistics"]["athletics"], 18)
        self.assertNotIn("athletics_rank", self.character(npc)["sheet"]["values"])

    def test_partial_sheet_updates_preserve_other_fields_and_shared_strength(self) -> None:
        actor = self.actor(sheet={"values": {"hp_max": 30, "hp_current": 25, "background": "Проводник"}})
        self.execute("update_character", {"sheet": {"values": {"hp_current": 12, "hp_temporary": 5}}}, id=actor)
        self.execute("update_character", {"attributes": {"strength": 4}}, id=actor)
        character = self.character(actor)
        self.assertEqual(character["sheet"]["values"]["hp_max"], 30)
        self.assertEqual(character["sheet"]["values"]["background"], "Проводник")
        self.assertEqual(character["sheet"]["values"]["hp_current"], 12)
        self.assertEqual(character["summary"]["maximum"], 14)
        self.assertEqual(character["sheet_statistics"]["athletics"], 4)
        self.execute("update_character", {"name": "Новое имя"}, id=actor)
        self.assertEqual(self.character(actor)["sheet"], character["sheet"])

    def test_invalid_values_and_hp_constraints_rollback_without_losing_inventory(self) -> None:
        actor = self.actor(sheet={"values": {"hp_max": 20, "hp_current": 15}})
        self.execute("add_item", {"name": "Меч", "category": "weapon", "bulk": "1"}, character_id=actor)
        before = self.state()
        cases = [
            {"sheet": None}, {"sheet": []}, {"sheet": {"unknown": 1}},
            {"sheet": {"profile": "missing"}}, {"sheet": {"values": []}},
            {"sheet": {"values": {"level": True}}}, {"sheet": {"values": {"level": -1}}},
            {"sheet": {"values": {"hp_current": 21}}}, {"sheet": {"values": {"hp_max": 10}}},
            {"sheet": {"values": {"hero_points": 4}}}, {"sheet": {"values": {"level": 2.5}}},
            {"sheet": {"values": {"athletics_rank": "god"}}}, {"sheet": {"values": {"unknown": 2}}},
            {"attributes": {"wisdom": "2"}}, {"sheet": {"values": {}}, "kind": []},
        ]
        for data in cases:
            with self.subTest(data=data), self.assertRaises(ValueError):
                self.execute("update_character", data, id=actor)
            self.assertEqual(self.state(), before)

    def test_bulk_validation_also_applies_to_sheet_attribute_edits(self) -> None:
        actor = self.actor(attributes={"strength": 4})
        self.execute("add_item", {"name": "Груз", "category": "gear", "bulk": "13"}, character_id=actor)
        before = self.state()
        with self.assertRaises(ValueError):
            self.execute("update_character", {"attributes": {"strength": 0}}, id=actor)
        self.assertEqual(self.state(), before)

    def test_snapshots_data_and_retries_survive_restart_and_config_changes(self) -> None:
        actor = self.actor(sheet={"values": {"level": 3, "hp_max": 25, "hp_current": 20}})
        command = {"action": "update_character", "id": actor, "revision": self.state()["revision"],
                   "data": {"sheet": {"values": {"hp_current": 17}}}}
        request_id = str(uuid4())
        receipt = self.store.execute(self.room, command, request_id)
        before = self.state()
        config = deepcopy(self.rules.config)
        config["profiles"]["pc"]["fields"]["hp_max"]["default"] = 99
        restarted = RosterStore(ChatStore(self.database), self.inventory, CharacterRules(config))
        self.assertEqual(restarted.read(self.room), before)
        self.assertEqual(restarted.execute(self.room, command, request_id), receipt)
        new_room = self.chat.create_room()
        self.assertEqual(restarted.read(new_room)["character_rules"], config)
        with self.assertRaises(MessageConflict):
            restarted.execute(self.room, command, str(uuid4()))

    def test_migration_of_old_rosters_preserves_ids_revision_inventory_and_strength(self) -> None:
        path = Path(self.directory.name) / "legacy.sqlite3"
        chat = ChatStore(path)
        room = chat.create_room()
        actor, item, player = str(uuid4()), str(uuid4()), str(uuid4())
        with chat._connection() as connection:
            connection.executescript("""
                CREATE TABLE room_rosters (room_id TEXT PRIMARY KEY REFERENCES rooms(id), revision INTEGER NOT NULL DEFAULT 0, rules TEXT NOT NULL);
                CREATE TABLE players (id TEXT NOT NULL, room_id TEXT NOT NULL, name TEXT NOT NULL, PRIMARY KEY(room_id, id));
                CREATE TABLE characters (id TEXT NOT NULL, room_id TEXT NOT NULL, player_id TEXT, body TEXT NOT NULL, PRIMARY KEY(room_id, id));
                CREATE TABLE inventory_items (id TEXT NOT NULL, room_id TEXT NOT NULL, character_id TEXT NOT NULL, body TEXT NOT NULL, PRIMARY KEY(room_id, id));
            """)
            connection.execute("INSERT INTO room_rosters VALUES (?, 7, ?)", (room, json.dumps(self.inventory.config)))
            connection.execute("INSERT INTO players VALUES (?, ?, ?)", (player, room, "Игрок"))
            body = self.inventory.character({"name": "Старый герой", "player_id": player, "attributes": {"strength": 3}},
                                            [{"id": player, "name": "Игрок"}])
            connection.execute("INSERT INTO characters VALUES (?, ?, ?, ?)", (actor, room, player, json.dumps(body)))
            body = self.inventory.item({"name": "Меч", "category": "weapon", "bulk": "1", "state": "held"})
            connection.execute("INSERT INTO inventory_items VALUES (?, ?, ?, ?)", (item, room, actor, json.dumps(body)))
        upgraded = RosterStore(chat, self.inventory, self.rules)
        state = upgraded.read(room)
        character = state["characters"][0]
        self.assertEqual(state["revision"], 7)
        self.assertEqual(character["id"], actor)
        self.assertEqual(character["player_id"], player)
        self.assertEqual(character["attributes"]["strength"], 3)
        self.assertEqual(character["attributes"]["wisdom"], 0)
        self.assertEqual(character["items"][0]["id"], item)
        self.assertEqual(character["sheet"]["profile"], "pc")
        self.assertEqual(character["summary"]["hands_used"], 1)
        self.assertEqual(upgraded.read(room), state)

    def test_fields_profiles_and_formulas_can_be_replaced_without_pf2e_code(self) -> None:
        config = deepcopy(self.rules.config)
        config["id"] = "example-system"
        config["attributes"] = {"resolve": {"label": "Воля", "min": 0, "max": 10, "default": 2}}
        profile = {"label": "Общий лист", "description": "", "kinds": ["pc", "npc"], "fields": {
            "tier": {"label": "Ранг", "group": "identity", "type": "integer", "min": 0, "max": 10, "default": 1},
        }, "statistics": {
            "guard": {"label": "Защита", "group": "defense", "expression": {"add": [5, {"multiply": [2, {"ref": "attributes.resolve"}]}, {"ref": "sheet.tier"}]}},
        }}
        config["profiles"] = {"universal": profile}
        config["default_profiles"] = {"pc": "universal", "npc": "universal"}
        store = RosterStore(self.chat, self.inventory, CharacterRules(config))
        room = self.chat.create_room()
        command = {"action": "create_character", "revision": 0, "data": {"name": "Другой", "attributes": {"resolve": 4}, "sheet": {"values": {"tier": 3}}}}
        store.execute(room, command, str(uuid4()))
        actor = store.read(room)["characters"][0]
        self.assertEqual(actor["sheet_statistics"], {"guard": 16})
        snapshot = store.read(room)["character_rules"]
        self.assertEqual(list(snapshot["groups"]), list(config["groups"]))
        self.assertEqual(list(snapshot["attributes"]), list(config["attributes"]))
        self.assertNotIn("dexterity", actor["attributes"])
        self.assertIn("strength", actor["attributes"])  # existing inventory contract remains independent

    def test_malformed_configuration_is_rejected_before_use(self) -> None:
        cases = [
            lambda c: c.update(schema_version=True),
            lambda c: c["profiles"]["pc"]["fields"]["level"].update(default=True),
            lambda c: c["profiles"]["pc"]["statistics"]["armor_class"].update(expression={"eval": "1+1"}),
            lambda c: c["profiles"]["pc"]["statistics"]["armor_class"].update(expression={"ref": "sheet.missing"}),
            lambda c: c["profiles"]["pc"]["statistics"]["armor_class"].update(expression={"ref": "sheet.class_name"}),
            lambda c: c["default_profiles"].update(pc="npc"),
        ]
        for change in cases:
            config = deepcopy(self.rules.config)
            change(config)
            with self.subTest(change=change), self.assertRaises(ValueError):
                CharacterRules(config)


if __name__ == "__main__":
    unittest.main()
