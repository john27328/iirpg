"""Deterministic dice and queue integration tests; no external AI or network."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import json
from pathlib import Path
import tempfile
import sqlite3
import unittest
from unittest.mock import patch
from uuid import UUID, uuid4, uuid5

from iirpg.application import ChatService
from iirpg.dice import DiceTool, parse_roll
from iirpg.storage import ChatStore, MessageConflict


def sequence_roller(values):
    sequence = iter(values)
    return lambda sides: next(sequence)


class DiceTests(unittest.TestCase):
    def test_sum_supports_multiple_groups_and_modifier(self) -> None:
        tool = DiceTool(roll_die=sequence_roller([4, 2, 3]))
        result = tool.roll({"system": "sum_d6", "roll": "2d6 + 1d4 - 2",
                            "success": {"operator": ">=", "target": 7}, "label": "Проверка"})
        self.assertEqual([group["values"] for group in result["rolls"]], [[4, 2], [3]])
        self.assertEqual(result["modifier"], -2)
        self.assertEqual(result["result"], 7)
        self.assertIs(result["success"], True)
        self.assertIn("Проверка", result["summary"])

    def test_comparisons_include_roll_under_and_exact_boundaries(self) -> None:
        tool = DiceTool(roll_die=lambda sides: 50)
        for operator, expected in [(">=", True), (">", False), ("<=", True),
                                   ("<", False), ("==", True), ("!=", False)]:
            with self.subTest(operator=operator):
                result = tool.roll({"system": "percentile", "success": {"operator": operator, "target": 50}})
                self.assertIs(result["success"], expected)

    def test_pool_counts_successful_dice_instead_of_summing_faces(self) -> None:
        tool = DiceTool(roll_die=sequence_roller([10, 7, 6, 4, 1]))
        result = tool.roll({"system": "pool_d10", "roll": "5d10+1",
                            "success": {"operator": ">=", "target": 4}})
        self.assertEqual(result["rolls"][0]["score"], 3)
        self.assertEqual(result["result"], 4)
        self.assertIs(result["success"], True)
        custom = DiceTool(roll_die=sequence_roller([1, 2, 3])).roll({
            "system": "pool_d10", "roll": "3d10",
            "success": {"operator": ">=", "target": 2, "die": {"operator": "<=", "target": 2}},
        })
        self.assertEqual(custom["result"], 2)

    def test_invalid_input_is_rejected_before_randomness(self) -> None:
        def forbidden(sides):
            self.fail("Invalid input must not roll dice")

        tool = DiceTool(roll_die=forbidden)
        valid = {"system": "d20", "success": {"operator": ">=", "target": 10}}
        invalid = [None, [], {}, {**valid, "system": "unknown"}, {**valid, "success": None},
                   {**valid, "success": {"operator": ">=", "target": True}},
                   {**valid, "success": {"operator": "bad", "target": 5}},
                   {**valid, "success": {"operator": ">=", "target": 1.5}},
                   {**valid, "success": {"operator": ">=", "target": 5, "die": {"operator": ">=", "target": 2}}},
                   {**valid, "extra": 1}, {**valid, "label": ""}]
        for roll in ["__import__('os')", "0d20", "1d1", "101d6", "50d6+51d6", "1d10001",
                     "1d20++5", "1d20garbage", "100", "1d6+10001", "1d6*2"]:
            invalid.append({**valid, "roll": roll})
        for payload in invalid:
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                tool.roll(payload)

    def test_rules_file_can_define_a_new_format(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            rules = Path(directory) / "dice.json"
            rules.write_text(json.dumps({"version": 1, "systems": {"custom": {
                "label": "Свой формат", "aggregation": "count", "roll": "4d8",
                "success": {"operator": ">=", "target": 2, "die": {"operator": ">=", "target": 6}},
            }}}))
            tool = DiceTool(rules, sequence_roller([8, 2, 6, 3]))
            result = tool.roll({"system": "custom", "success": {"operator": ">=", "target": 2}})
            self.assertEqual(result["result"], 2)
            self.assertIs(result["success"], True)

    def test_maximum_roll_and_invalid_random_source(self) -> None:
        tool = DiceTool(roll_die=lambda sides: 1)
        result = tool.roll({"system": "sum_d6", "roll": "100d6", "success": {"operator": ">=", "target": 100}})
        self.assertEqual(result["result"], 100)
        with self.assertRaises(RuntimeError):
            DiceTool(roll_die=lambda sides: 0).roll({"system": "d20", "success": {"operator": ">=", "target": 1}})
        self.assertEqual(parse_roll("-d6+2")[2], 2)


class DiceQueueTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.database = Path(self.directory.name) / "chat.sqlite3"
        self.draws = 0

        def roll_die(sides):
            self.draws += 1
            return 13

        self.service = ChatService(ChatStore(self.database), DiceTool(roll_die=roll_die))
        self.room = self.service.store.create_room()

    def tearDown(self) -> None:
        self.directory.cleanup()

    def payload(self):
        return {"sender": "second", "type": "tool.call", "tool": "dice.roll", "request_id": str(uuid4()),
                "arguments": {"system": "d20", "roll": "1d20+5", "success": {"operator": ">=", "target": 15},
                              "label": "Скрытность"}, "visibility": {"mode": "selected", "chat_ids": ["first"]}}

    def test_tool_publishes_human_and_structured_result_with_visibility(self) -> None:
        payload = self.payload()
        event = self.service.send(self.room, payload)
        self.assertEqual(event["type"], "tool.result")
        self.assertEqual(event["sender"], "dice")
        self.assertEqual(event["requested_by"], "second")
        self.assertEqual(event["tool_result"]["result"], 18)
        self.assertIs(event["tool_result"]["success"], True)
        self.assertIn("18", event["text"])
        self.assertIn("успех", event["text"])
        call, result = self.service.messages(self.room, "first")["messages"]
        self.assertEqual(result, event)
        self.assertEqual(call["type"], "tool.call")
        self.assertEqual(call["sender"], "second")
        self.assertEqual(call["request_id"], payload["request_id"])
        self.assertEqual(result["tool_call_id"], call["request_id"])
        self.assertEqual(call["arguments"], event["arguments"])
        self.assertEqual(call["visibility"], result["visibility"])
        self.assertLess(call["id"], result["id"])
        self.assertIn("1d20+5", call["text"])
        self.assertIn("≥ 15", call["text"])
        self.assertEqual(self.service.messages(self.room, "second")["messages"], [])

    def test_concurrent_retries_and_restart_do_not_reroll(self) -> None:
        payload = self.payload()
        with ThreadPoolExecutor(max_workers=6) as executor:
            events = list(executor.map(lambda _: self.service.send(self.room, payload), range(12)))
        self.assertEqual(self.draws, 1)
        self.assertEqual(len({event["id"] for event in events}), 1)

        def forbidden(sides):
            self.fail("Retry after restart must not roll")

        restarted = ChatService(ChatStore(self.database), DiceTool(roll_die=forbidden))
        self.assertEqual(restarted.send(self.room, payload), events[0])
        self.assertEqual(len(restarted.messages(self.room, "first")["messages"]), 2)
        for changes in [{"arguments": {**payload["arguments"], "roll": "1d20+6"}},
                        {"sender": "first"}, {"metadata": {"changed": True}},
                        {"visibility": {"mode": "all"}}]:
            with self.subTest(changes=changes), self.assertRaises(MessageConflict):
                restarted.send(self.room, {**payload, **changes})

    def test_call_and_result_can_be_read_across_page_boundary(self) -> None:
        payload = self.payload()
        result = self.service.send(self.room, payload)
        with patch("iirpg.storage.PAGE_SIZE", 1):
            first_page = self.service.messages(self.room, "first")
            self.assertEqual(first_page["messages"][0]["type"], "tool.call")
            self.assertTrue(first_page["has_more"])
            second_page = self.service.messages(self.room, "first", first_page["next_cursor"])
            self.assertEqual(second_page["messages"], [result])
            self.assertFalse(second_page["has_more"])
            hidden_page = self.service.messages(self.room, "second")
            self.assertEqual(hidden_page["messages"], [])
            self.assertTrue(hidden_page["has_more"])
            self.assertEqual(self.service.messages(self.room, "second", hidden_page["next_cursor"])["messages"], [])

    def test_legacy_result_retry_preserves_history_without_reroll(self) -> None:
        payload = self.payload()
        result = self.service.send(self.room, payload)
        envelope = {key: value for key, value in result.items()
                    if key not in ("id", "room_id", "request_id", "tool_call_id")}
        with closing(sqlite3.connect(self.database)) as connection, connection:
            connection.execute("DELETE FROM queue_events WHERE request_id = ?", (payload["request_id"],))
            connection.execute("UPDATE queue_events SET request_id = ?, envelope = ? WHERE id = ?",
                               (payload["request_id"], json.dumps(envelope), result["id"]))
        legacy = self.service.messages(self.room, "first")["messages"][0]
        restarted = ChatService(ChatStore(self.database), DiceTool(roll_die=lambda sides: self.fail("Legacy retry rerolled")))
        self.assertEqual(restarted.send(self.room, payload), legacy)
        self.assertEqual(self.draws, 1)
        self.assertEqual(restarted.messages(self.room, "first")["messages"], [legacy])

    def test_result_identifier_collision_is_rejected_before_rolling(self) -> None:
        payload = self.payload()
        result_id = str(uuid5(UUID(payload["request_id"]), "tool.result"))
        message = self.service.send(self.room, {"sender": "first", "text": "Existing", "request_id": result_id})
        with self.assertRaises(MessageConflict):
            self.service.send(self.room, payload)
        self.assertEqual(self.draws, 0)
        self.assertEqual(self.service.messages(self.room, "first")["messages"], [message])

    def test_missing_room_and_request_collisions_do_not_roll(self) -> None:
        payload = self.payload()
        with self.assertRaises(KeyError):
            self.service.send(str(uuid4()), payload)
        self.assertEqual(self.draws, 0)
        chat = {"sender": "second", "request_id": payload["request_id"], "text": "Normal message"}
        self.service.send(self.room, chat)
        with self.assertRaises(MessageConflict):
            self.service.send(self.room, payload)
        self.assertEqual(self.draws, 0)
        payload = self.payload()
        self.service.send(self.room, payload)
        with self.assertRaises(MessageConflict):
            self.service.send(self.room, {**chat, "request_id": payload["request_id"]})

    def test_failed_random_source_leaves_no_partial_event(self) -> None:
        self.service.dice.roll_die = lambda sides: 0
        payload = self.payload()
        with self.assertRaises(RuntimeError):
            self.service.send(self.room, payload)
        self.assertEqual(self.service.messages(self.room, "first")["messages"], [])
        self.service.dice.roll_die = lambda sides: 13
        self.assertEqual(self.service.send(self.room, payload)["tool_result"]["result"], 18)


if __name__ == "__main__":
    unittest.main()
