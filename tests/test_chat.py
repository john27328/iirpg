"""Core delivery, persistence, isolation, and retry behavior."""

from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from iirpg.application import ChatService
from iirpg.domain import Chat, Visibility
from iirpg.storage import ChatStore, MessageConflict


class ChatTests(unittest.TestCase):
    def test_chat_publishes_without_a_peer(self) -> None:
        first, second = Chat("first", "First"), Chat("second", "Second")
        self.assertEqual(first.send(" Hello ").sender, "first")
        self.assertEqual(second.send("Ответ").sender, "second")
        self.assertEqual(first.send(" Hello \n").text, " Hello \n")
        self.assertNotIn("receiver", first.send("Hello").to_dict())

    def test_visibility_selects_subscribers(self) -> None:
        first, second = Chat("first", "First"), Chat("second", "Second")
        public = first.send("Hello").to_dict()
        restricted = first.send("Note", Visibility("selected", ("first",))).to_dict()
        self.assertTrue(first.can_see(public))
        self.assertTrue(second.can_see(public))
        self.assertTrue(first.can_see(restricted))
        self.assertFalse(second.can_see(restricted))

    def test_invalid_messages_are_rejected(self) -> None:
        first = Chat("first", "First")
        for text in [None, [], 10, "", " \n ", "a" * 2001]:
            with self.subTest(text_type=type(text).__name__):
                with self.assertRaises(ValueError):
                    first.send(text)
        self.assertEqual(len(first.send("я" * 2000).text), 2000)


class StorageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.database = Path(self.directory.name) / "chat.sqlite3"
        self.service = ChatService(ChatStore(self.database))
        self.room = self.service.store.create_room()

    def tearDown(self) -> None:
        self.directory.cleanup()

    def payload(self, text: str = "Привет 👋", sender: str = "first") -> dict:
        return {"sender": sender, "text": text, "request_id": str(uuid4())}

    def test_history_survives_new_service_instance(self) -> None:
        sent = self.service.send(self.room, self.payload())
        restarted = ChatService(ChatStore(self.database))
        self.assertEqual(restarted.messages(self.room, "first")["messages"], [sent])

    def test_rooms_are_isolated_and_cursor_returns_only_new_messages(self) -> None:
        other_room = self.service.store.create_room()
        first = self.service.send(self.room, self.payload())
        second = self.service.send(self.room, self.payload("Ответ", "second"))
        self.assertEqual(self.service.messages(other_room, "first")["messages"], [])
        self.assertEqual(self.service.messages(self.room, "first", first["id"])["messages"], [second])
        self.assertEqual(second["sender"], "second")
        self.assertEqual(self.service.messages(self.room, "second")["messages"], [first, second])

    def test_retry_returns_original_message_without_duplicate(self) -> None:
        payload = self.payload()
        first = self.service.send(self.room, payload)
        self.assertEqual(self.service.send(self.room, payload), first)
        self.assertEqual(len(self.service.messages(self.room, "first")["messages"]), 1)

    def test_concurrent_retries_are_idempotent(self) -> None:
        payload = self.payload()
        with ThreadPoolExecutor(max_workers=6) as executor:
            responses = list(executor.map(lambda _: self.service.send(self.room, payload), range(12)))
        self.assertEqual(len({item["id"] for item in responses}), 1)
        self.assertEqual(len(self.service.messages(self.room, "first")["messages"]), 1)

    def test_request_id_cannot_be_reused_with_changed_content(self) -> None:
        payload = self.payload()
        self.service.send(self.room, payload)
        for changes in [{"text": "Changed"}, {"metadata": {"tag": "changed"}},
                        {"visibility": {"mode": "selected", "chat_ids": ["first"]}}]:
            with self.subTest(changes=changes), self.assertRaises(MessageConflict):
                self.service.send(self.room, {**payload, **changes})

    def test_unknown_room_and_sender_and_invalid_identifier_are_rejected(self) -> None:
        with self.assertRaises(KeyError):
            self.service.send(str(uuid4()), self.payload())
        for changes in [{"sender": []}, {"sender": "third"}, {"request_id": "bad"}]:
            with self.subTest(changes=changes):
                with self.assertRaises(ValueError):
                    self.service.send(self.room, {**self.payload(), **changes})

    def test_visibility_and_metadata_survive_storage_and_are_filtered_per_chat(self) -> None:
        shared = self.service.send(self.room, self.payload("Shared"))
        payload = {**self.payload("Only first"),
                   "visibility": {"mode": "selected", "chat_ids": ["first"]},
                   "metadata": {"scene_id": "tavern", "tags": ["note", "draft"]}}
        restricted = self.service.send(self.room, payload)
        restarted = ChatService(ChatStore(self.database))
        self.assertEqual(restarted.messages(self.room, "first")["messages"], [shared, restricted])
        self.assertEqual(restarted.messages(self.room, "second")["messages"], [shared])
        self.assertEqual(restarted.messages(self.room, "first")["messages"][1]["metadata"], payload["metadata"])
        self.assertEqual(restarted.messages(self.room, "first")["messages"], [shared, restricted])
        with sqlite3.connect(self.database) as connection:
            raw = connection.execute("SELECT envelope FROM queue_events WHERE id = ?", (restricted["id"],)).fetchone()[0]
        self.assertEqual(json.loads(raw)["visibility"], payload["visibility"])

    def test_hidden_page_advances_cursor_and_does_not_block_later_visible_message(self) -> None:
        for index in range(2):
            self.service.send(self.room, {**self.payload(f"Hidden {index}"),
                              "visibility": {"mode": "selected", "chat_ids": ["first"]}})
        visible = self.service.send(self.room, self.payload("Visible after hidden page"))
        with patch("iirpg.storage.PAGE_SIZE", 2):
            first_page = self.service.messages(self.room, "second")
            self.assertEqual(first_page["messages"], [])
            self.assertTrue(first_page["has_more"])
            self.assertGreater(first_page["next_cursor"], 0)
            next_page = self.service.messages(self.room, "second", first_page["next_cursor"])
            self.assertEqual(next_page["messages"], [visible])
            self.assertFalse(next_page["has_more"])
            empty = self.service.messages(self.room, "second", next_page["next_cursor"])
            self.assertEqual(empty["messages"], [])
            self.assertEqual(empty["next_cursor"], next_page["next_cursor"])

    def test_invalid_visibility_and_metadata_are_rejected(self) -> None:
        changes = [
            {"visibility": None}, {"visibility": {"mode": "unknown"}},
            {"visibility": {"mode": "all", "chat_ids": ["first"]}},
            {"visibility": {"mode": "selected", "chat_ids": []}},
            {"visibility": {"mode": "selected", "chat_ids": ["third"]}},
            {"visibility": {"mode": "selected", "chat_ids": "first"}},
            {"metadata": []}, {"metadata": None}, {"metadata": {"huge": "x" * 4097}},
            {"metadata": {"number": float("nan")}}, {"type": "unknown.event"},
        ]
        for change in changes:
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.service.send(self.room, {**self.payload(), **change})
        for viewer in ["", "third", None]:
            with self.subTest(viewer=viewer), self.assertRaises(ValueError):
                self.service.messages(self.room, viewer)

    def test_choices_survive_restart_and_do_not_restrict_free_text(self) -> None:
        payload = {**self.payload("Куда пойдёте?", "second"), "choices": ["В лес", "В город"]}
        question = self.service.send(self.room, payload)
        restarted = ChatService(ChatStore(self.database))
        self.assertEqual(restarted.messages(self.room, "first")["messages"][0]["choices"], payload["choices"])
        self.assertEqual(restarted.send(self.room, payload), question)
        with self.assertRaises(MessageConflict):
            restarted.send(self.room, {**payload, "choices": ["Домой"]})
        reply = restarted.send(self.room, self.payload("Останусь на месте и разобью лагерь"))
        self.assertEqual(reply["choices"], [])
        self.assertEqual(restarted.messages(self.room, "second")["messages"], [question, reply])

    def test_invalid_choices_are_rejected_without_appending(self) -> None:
        for choices in [None, "Ответ", {}, [1], [None], [{}], [""], [" \n"], ["x" * 201], ["Да"] * 11]:
            with self.subTest(choices=choices), self.assertRaises(ValueError):
                self.service.send(self.room, {**self.payload(), "choices": choices})
        self.assertEqual(self.service.messages(self.room, "first")["messages"], [])
        boundary = ["x" * 200 for _ in range(10)]
        message = self.service.send(self.room, {**self.payload(), "choices": boundary})
        self.assertEqual(message["choices"], boundary)

    def test_legacy_history_migrates_once_and_preserves_ids(self) -> None:
        database = Path(self.directory.name) / "legacy.sqlite3"
        request_id = str(uuid4())
        with sqlite3.connect(database) as connection:
            connection.executescript("""
                CREATE TABLE rooms (id TEXT PRIMARY KEY, created_at TEXT DEFAULT CURRENT_TIMESTAMP);
                CREATE TABLE messages (id INTEGER PRIMARY KEY, room_id TEXT, request_id TEXT,
                                       sender TEXT, receiver TEXT, text TEXT, created_at TEXT);
            """)
            connection.execute("INSERT INTO rooms (id) VALUES (?)", (self.room,))
            connection.execute("INSERT INTO messages VALUES (7, ?, ?, 'first', 'second', 'Legacy', '2026-10-27T00:00:00+00:00')", (self.room, request_id))
        for _ in range(2):
            service = ChatService(ChatStore(database))
            history = service.messages(self.room, "second")["messages"]
            self.assertEqual(len(history), 1)
            self.assertEqual(history[0]["id"], 7)
            self.assertEqual(history[0]["text"], "Legacy")
            self.assertEqual(history[0]["request_id"], request_id)
            self.assertEqual(history[0]["visibility"]["mode"], "all")
            self.assertEqual(history[0]["choices"], [])
        sent = service.send(self.room, self.payload("New"))
        self.assertGreater(sent["id"], 7)


if __name__ == "__main__":
    unittest.main()
