"""Model-facing commands without queue IDs, senders or tool envelopes."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile
import unittest
from uuid import uuid4

from iirpg.application import ChatService
from iirpg.commands import describe_commands
from iirpg.dice import DiceTool
from iirpg.storage import ChatStore, MessageConflict


class CommandTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.database = Path(self.directory.name) / "chat.sqlite3"
        self.draws = 0

        def roll_die(sides):
            self.draws += 1
            return min(13, sides)

        self.service = ChatService(ChatStore(self.database), DiceTool(roll_die=roll_die))
        self.room = self.service.store.create_room()

    def tearDown(self) -> None:
        self.directory.cleanup()

    def send(self, command, request_id=None):
        return self.service.send_command(self.room, command, request_id or str(uuid4()))

    def test_plain_text_uses_gm_and_keeps_choices_and_whitespace(self) -> None:
        command = {"text": "  Перед вами дверь.\n", "choices": ["Открыть", "Уйти"]}
        message = self.send(command)
        self.assertEqual(message["sender"], "second")
        self.assertEqual(message["text"], command["text"])
        self.assertEqual(message["choices"], command["choices"])
        self.assertEqual(message["visibility"], {"mode": "all", "chat_ids": []})
        self.assertEqual(self.draws, 0)
        self.assertNotIn("request_id", command)

    def test_offer_waits_for_player_and_uses_flat_dice_arguments(self) -> None:
        offer = self.send({"action": "offer_roll", "text": "Проверь скрытность.", "roll": "1d20+5", "target": 15})
        self.assertEqual(self.draws, 0)
        self.assertEqual(offer["type"], "tool.offer")
        self.assertEqual(offer["recipient"], "first")
        self.assertEqual(offer["arguments"]["success"], {"operator": ">=", "target": 15})
        result = self.service.send(self.room, {"sender": "first", "type": "tool.call", "tool": "dice.roll",
                                             "offer_id": offer["request_id"], "request_id": offer["roll_request_id"]})
        self.assertEqual(result["tool_result"]["result"], 18)
        self.assertEqual(result["requested_by"], "first")

    def test_transport_key_prevents_rerolls_concurrently_and_after_restart(self) -> None:
        command = {"action": "roll", "roll": "1d20+5", "target": 15}
        key = str(uuid4())
        with ThreadPoolExecutor(max_workers=6) as executor:
            results = list(executor.map(lambda _: self.send(command, key), range(12)))
        self.assertEqual(self.draws, 1)
        self.assertTrue(all(result == results[0] for result in results))
        restarted = ChatService(ChatStore(self.database), DiceTool(roll_die=lambda sides: self.fail("Retry rerolled")))
        self.assertEqual(restarted.send_command(self.room, command, key), results[0])
        with self.assertRaises(MessageConflict):
            restarted.send_command(self.room, {**command, "target": 20}, key)
        self.assertEqual(len(restarted.messages(self.room, "second")["messages"]), 2)

    def test_audience_applies_to_entire_offer_and_roll(self) -> None:
        offer = self.send({"action": "offer_roll", "text": "Для игрока.", "roll": "1d20", "target": 10,
                           "audience": "player", "metadata": {"scene": "gate"}})
        self.assertEqual(self.service.messages(self.room, "second")["messages"], [])
        self.assertEqual(self.service.messages(self.room, "first")["messages"], [offer])
        result = self.service.send(self.room, {"sender": "first", "type": "tool.call", "tool": "dice.roll",
                                             "offer_id": offer["request_id"], "request_id": offer["roll_request_id"]})
        self.assertEqual(result["metadata"], {"scene": "gate"})
        self.assertEqual(self.service.messages(self.room, "second")["messages"], [])
        gm = self.send({"text": "Заметка ведущего.", "audience": "gm"})
        self.assertEqual(self.service.messages(self.room, "second")["messages"], [gm])

    def test_percentile_and_pool_accept_optional_comparisons(self) -> None:
        percentile = self.send({"action": "roll", "system": "percentile", "roll": "1d100", "target": 20, "operator": "<="})
        self.assertIs(percentile["tool_result"]["success"], True)
        pool = self.send({"action": "roll", "system": "pool_d10", "roll": "3d10", "target": 3, "die_target": 8})
        self.assertEqual(pool["tool_result"]["result"], 3)
        self.assertEqual(pool["arguments"]["success"]["die"], {"operator": ">=", "target": 8})

    def test_invalid_commands_and_transport_fields_are_rejected_before_randomness(self) -> None:
        valid = {"action": "offer_roll", "text": "Проверка", "roll": "1d20", "target": 10}
        invalid = [[], None, {}, {"action": []}, {"action": "unknown"}, {**valid, "audience": "gm"},
                   {**valid, "audience": []}, {**valid, "target": "15"}, {**valid, "roll": "0d20"},
                   {**valid, "sender": "first"}, {**valid, "request_id": str(uuid4())},
                   {**valid, "choices": ["Roll"]}, {**valid, "targett": 15},
                   {"action": "roll", "target": 15}, {"action": "roll", "roll": "1d20"},
                   {**valid, "die_operator": ">="}]
        for command in invalid:
            with self.subTest(command=command), self.assertRaises(ValueError):
                self.send(command)
        self.assertEqual(self.draws, 0)
        self.assertEqual(self.service.messages(self.room, "first")["messages"], [])

    def test_same_command_with_new_transport_key_is_a_new_operation(self) -> None:
        command = {"action": "roll", "roll": "1d20", "target": 10}
        first = self.send(command)
        second = self.send(command)
        self.assertNotEqual(first["id"], second["id"])
        self.assertEqual(self.draws, 2)

    def test_published_examples_work_without_invented_ids(self) -> None:
        for command in describe_commands()["examples"]:
            with self.subTest(command=command):
                self.assertNotIn("request_id", command)
                self.assertNotIn("sender", command)
                self.send(command)


if __name__ == "__main__":
    unittest.main()
