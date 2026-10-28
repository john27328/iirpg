"""HTTP-level tests with a real ephemeral server and a temporary database."""

import json
from pathlib import Path
import secrets
import tempfile
from threading import Thread
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from uuid import uuid4

from iirpg.dice import DiceTool
from iirpg.server import ChatHTTPServer


class ServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.directory = tempfile.TemporaryDirectory()
        cls.server = ChatHTTPServer(("127.0.0.1", 0), Path(cls.directory.name) / "chat.sqlite3",
                                    dice=DiceTool(roll_die=lambda sides: min(13, sides)))
        cls.worker = Thread(target=cls.server.serve_forever, daemon=True)
        cls.worker.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.worker.join()
        cls.directory.cleanup()

    def request(self, path: str, payload: object = None, headers: dict | None = None) -> tuple[int, bytes]:
        data = None if payload is None else json.dumps(payload).encode()
        return self.raw_request(path, data, headers)

    def raw_request(self, path: str, data: bytes | None = None, headers: dict | None = None) -> tuple[int, bytes]:
        request = Request(self.base + path, data=data, headers={"Content-Type": "application/json", **(headers or {})})
        try:
            response = urlopen(request, timeout=5)
        except HTTPError as error:
            response = error
        with response:
            return response.status, response.read()

    def test_page_and_static_assets_are_served(self) -> None:
        for path, marker in [("/", "Одна очередь."), ("/app.js", "class Chat"), ("/styles.css", ".chats")]:
            status, body = self.request(path)
            self.assertEqual(status, 200)
            self.assertIn(marker, body.decode())
        self.assertEqual(self.request("/health")[0], 200)

    def test_full_bidirectional_delivery_and_retry(self) -> None:
        status, body = self.request("/api/rooms", {})
        self.assertEqual(status, 201)
        room = json.loads(body)["room_id"]
        path = f"/api/rooms/{room}/messages"
        payload = {"sender": "first", "text": "Первое сообщение", "request_id": str(uuid4())}
        status, body = self.request(path, payload)
        self.assertEqual(status, 201)
        message = json.loads(body)["message"]
        self.assertEqual(message["visibility"]["mode"], "all")
        self.assertNotIn("receiver", message)
        self.assertEqual(json.loads(self.request(path, payload)[1])["message"], message)
        status, body = self.request(path, {"sender": "second", "text": "Ответ", "request_id": str(uuid4())})
        self.assertEqual(status, 201)
        self.assertEqual(json.loads(body)["message"]["sender"], "second")
        messages = json.loads(self.request(path + "?chat=first")[1])["messages"]
        self.assertEqual([item["text"] for item in messages], ["Первое сообщение", "Ответ"])
        self.assertEqual(len(json.loads(self.request(f"{path}?chat=first&after={message['id']}")[1])["messages"]), 1)
        self.assertEqual(len(json.loads(self.request(path + "?chat=second")[1])["messages"]), 2)
        self.assertEqual(self.request(path, {**payload, "text": "Changed"})[0], 409)

    def test_invalid_requests_and_unknown_paths(self) -> None:
        room = json.loads(self.request("/api/rooms", {})[1])["room_id"]
        path = f"/api/rooms/{room}/messages"
        self.assertEqual(self.request(path, {"sender": "first", "text": ""})[0], 400)
        self.assertEqual(self.request(path, ["not an object"])[0], 400)
        self.assertEqual(self.request(path + "?after=-1")[0], 400)
        self.assertEqual(self.request(path + "?after=999999999999999999999")[0], 400)
        self.assertEqual(self.request(path)[0], 400)
        self.assertEqual(self.request(path + "?chat=third")[0], 400)
        self.assertEqual(self.request(f"/api/rooms/{uuid4()}/messages?chat=first")[0], 404)
        self.assertEqual(self.request("/../../AGENTS.md")[0], 404)
        self.assertEqual(self.request("/missing")[0], 404)

    def test_restricted_message_never_appears_in_other_subscriber_response(self) -> None:
        room = json.loads(self.request("/api/rooms", {})[1])["room_id"]
        path = f"/api/rooms/{room}/messages"
        payload = {"sender": "first", "text": "Restricted first-only content",
                   "request_id": str(uuid4()),
                   "visibility": {"mode": "selected", "chat_ids": ["first"]},
                   "metadata": {"scene_id": "secret-room"}}
        self.assertEqual(self.request(path, payload)[0], 201)
        first = json.loads(self.request(path + "?chat=first")[1])
        status, second_raw = self.request(path + "?chat=second")
        self.assertEqual(status, 200)
        self.assertEqual(len(first["messages"]), 1)
        self.assertEqual(json.loads(second_raw)["messages"], [])
        self.assertNotIn(b"Restricted first-only content", second_raw)
        self.assertNotIn(b"secret-room", second_raw)
        self.assertGreater(json.loads(second_raw)["next_cursor"], 0)

    def test_raw_json_preserves_text_metadata_and_visibility(self) -> None:
        room = json.loads(self.request("/api/rooms", {})[1])["room_id"]
        path = f"/api/rooms/{room}/messages"
        payload = {
            "sender": "second", "type": "chat.message", "request_id": str(uuid4()),
            "text": "  Строка с пробелами\n\n  Вторая строка\n ",
            "choices": ["Вариант 1", "Вариант 2"],
            "visibility": {"mode": "selected", "chat_ids": ["first"]},
            "metadata": {"source": "json-stub", "values": [False, 0, None, {"label": "Тест"}]},
        }
        raw = json.dumps(payload, ensure_ascii=False, indent=4).encode()
        status, body = self.raw_request(path, raw)
        self.assertEqual(status, 201)
        message = json.loads(body)["message"]
        for key, value in payload.items():
            self.assertEqual(message[key], value)
        first = json.loads(self.request(path + "?chat=first")[1])["messages"]
        self.assertEqual(first, [message])
        self.assertEqual(json.loads(self.request(path + "?chat=second")[1])["messages"], [])
        self.assertEqual(json.loads(self.raw_request(path, raw)[1])["message"]["id"], message["id"])

    def test_malformed_json_does_not_append_a_queue_entry(self) -> None:
        room = json.loads(self.request("/api/rooms", {})[1])["room_id"]
        path = f"/api/rooms/{room}/messages"
        for raw in [b'{"sender":', b'not json', b'null', b'[]']:
            with self.subTest(raw=raw):
                status, body = self.raw_request(path, raw)
                self.assertEqual(status, 400)
                self.assertIn("error", json.loads(body))
        self.assertEqual(json.loads(self.request(path + "?chat=first")[1])["messages"], [])

    def test_choices_allow_a_custom_reply(self) -> None:
        room = json.loads(self.request("/api/rooms", {})[1])["room_id"]
        path = f"/api/rooms/{room}/messages"
        payload = {"sender": "second", "request_id": str(uuid4()),
                   "text": "Что будете делать?", "choices": ["Открыть дверь", "Уйти"]}
        status, body = self.request(path, payload)
        self.assertEqual(status, 201)
        question = json.loads(body)["message"]
        self.assertEqual(question["choices"], payload["choices"])
        self.assertEqual(json.loads(self.request(path + "?chat=first")[1])["messages"][0]["choices"], payload["choices"])
        status, body = self.request(path, {"sender": "first", "request_id": str(uuid4()),
                                          "text": "Постучать и спросить, кто внутри"})
        self.assertEqual(status, 201)
        reply = json.loads(body)["message"]
        self.assertEqual(reply["text"], "Постучать и спросить, кто внутри")
        self.assertEqual(reply["choices"], [])
        self.assertEqual(json.loads(self.request(path + "?chat=second")[1])["messages"], [question, reply])
        self.assertEqual(self.request(path, {**payload, "request_id": str(uuid4()), "choices": "bad"})[0], 400)

    def test_raw_ai_tool_call_records_request_and_reply(self) -> None:
        room = json.loads(self.request("/api/rooms", {})[1])["room_id"]
        path = f"/api/rooms/{room}/messages"
        payload = {"sender": "second", "type": "tool.call", "tool": "dice.roll",
                   "request_id": str(uuid4()), "arguments": {
                       "system": "d20", "roll": "1d20+5", "success": {"operator": ">=", "target": 15}}}
        raw = json.dumps(payload, indent=2).encode()
        status, body = self.raw_request(path, raw)
        self.assertEqual(status, 201)
        reply = json.loads(body)
        messages = json.loads(self.request(path + "?chat=second")[1])["messages"]
        call, result = messages
        self.assertEqual(call["sender"], "second")
        self.assertEqual(call["type"], "tool.call")
        self.assertEqual(result, reply["message"])
        self.assertEqual(result["tool_call_id"], payload["request_id"])
        self.assertEqual(call["metadata"], {})
        self.assertEqual(call["visibility"], {"mode": "all", "chat_ids": []})
        self.assertEqual(json.loads(self.raw_request(path, raw)[1]), reply)
        self.assertEqual(json.loads(self.request(path + "?chat=first")[1])["messages"], messages)

    def test_ai_can_offer_dice_then_player_rolls_once(self) -> None:
        room = json.loads(self.request("/api/rooms", {})[1])["room_id"]
        path = f"/api/rooms/{room}"
        payload = {"sender": "second", "type": "tool.offer", "tool": "dice.roll",
                   "recipient": "first", "text": "Проверьте скрытность.", "request_id": str(uuid4()),
                   "arguments": {"system": "d20", "roll": "1d20+5", "success": {"operator": ">=", "target": 15}}}
        status, body = self.request(path + "/messages", payload)
        self.assertEqual(status, 201)
        response = json.loads(body)
        self.assertNotIn("result", response)
        offer = response["message"]
        self.assertEqual(json.loads(self.request(path + "/messages?chat=first")[1])["messages"], [offer])
        accept = {"sender": "first", "request_id": offer["roll_request_id"], "offer_id": offer["request_id"]}
        status, body = self.request(path + "/tools/dice", accept)
        self.assertEqual(status, 201)
        result = json.loads(body)
        self.assertEqual(result["result"], 18)
        self.assertEqual(result["message"]["requested_by"], "first")
        self.assertEqual(result["message"]["offered_by"], "second")
        self.assertEqual(json.loads(self.request(path + "/messages", {**accept, "type": "tool.call", "tool": "dice.roll"})[1]), result)
        history = json.loads(self.request(path + "/messages?chat=second")[1])["messages"]
        self.assertEqual([event["type"] for event in history], ["tool.offer", "tool.call", "tool.result"])
        self.assertEqual(self.request(path + "/tools/dice", {**accept, "sender": "second"})[0], 400)

    def test_gm_command_body_has_no_queue_identifiers(self) -> None:
        self.assertEqual(self.request("/api/gm/commands")[0], 200)
        room = json.loads(self.request("/api/rooms", {})[1])["room_id"]
        path = f"/api/rooms/{room}/gm/commands"
        headers = {"Idempotency-Key": str(uuid4())}
        command = {"action": "offer_roll", "text": "Проверь скрытность.", "roll": "1d20+5", "target": 15}
        self.assertEqual(self.request(path, command)[0], 400)
        self.assertEqual(self.request(path, command, {"Idempotency-Key": "invalid"})[0], 400)
        status, body = self.request(path, command, headers)
        self.assertEqual(status, 201)
        offer = json.loads(body)["message"]
        self.assertEqual(offer["sender"], "second")
        self.assertEqual(offer["recipient"], "first")
        self.assertEqual(offer["request_id"], headers["Idempotency-Key"])
        self.assertEqual(json.loads(self.request(path, command, headers)[1])["message"], offer)
        self.assertEqual(self.request(path, {**command, "target": 20}, headers)[0], 409)
        self.assertEqual(self.request(path, {**command, "request_id": str(uuid4())}, headers)[0], 400)
        self.assertEqual(json.loads(self.request(f"/api/rooms/{room}/messages?chat=first")[1])["messages"], [offer])

    def test_dice_endpoint_and_queue_call_return_the_same_result(self) -> None:
        status, body = self.request("/api/tools/dice")
        self.assertEqual(status, 200)
        catalog = json.loads(body)
        self.assertEqual(catalog["name"], "dice.roll")
        self.assertIn("pool_d10", catalog["systems"])
        self.assertEqual(catalog["input_schema"]["required"], ["system", "success"])
        room = json.loads(self.request("/api/rooms", {})[1])["room_id"]
        path = f"/api/rooms/{room}"
        payload = {"sender": "first", "request_id": str(uuid4()),
                   "arguments": {"system": "d20", "roll": "1d20+5", "success": {"operator": ">=", "target": 18}}}
        status, body = self.request(path + "/tools/dice", payload)
        result = json.loads(body)
        self.assertEqual(status, 201)
        self.assertEqual(result["result"], 18)
        self.assertIs(result["success"], True)
        status, repeated = self.request(path + "/messages", {**payload, "type": "tool.call", "tool": "dice.roll"})
        self.assertEqual(status, 201)
        self.assertEqual(json.loads(repeated), result)
        messages = json.loads(self.request(path + "/messages?chat=first")[1])["messages"]
        self.assertEqual([message["type"] for message in messages], ["tool.call", "tool.result"])
        self.assertEqual(messages[0]["request_id"], payload["request_id"])
        self.assertEqual(messages[1], result["message"])
        self.assertEqual(messages[1]["tool_call_id"], messages[0]["request_id"])
        self.assertIn("18", messages[1]["text"])
        invalid = {**payload, "request_id": str(uuid4()), "arguments": {"system": "unknown"}}
        self.assertEqual(self.request(path + "/tools/dice", invalid)[0], 400)


class AccessTests(unittest.TestCase):
    def test_api_requires_key_and_keeps_history_unavailable_without_it(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            token = secrets.token_urlsafe(32)
            server = ChatHTTPServer(("127.0.0.1", 0), Path(directory) / "chat.sqlite3", token)
            worker = Thread(target=server.serve_forever, daemon=True)
            worker.start()
            base = f"http://127.0.0.1:{server.server_port}"

            def request(path: str, credential: str = "", payload: dict | None = None) -> tuple[int, dict]:
                headers = {"Content-Type": "application/json"}
                if credential:
                    headers["Authorization"] = f"Bearer {credential}"
                data = None if payload is None else json.dumps(payload).encode()
                try:
                    response = urlopen(Request(base + path, data=data, headers=headers), timeout=5)
                except HTTPError as error:
                    response = error
                with response:
                    return response.status, json.loads(response.read())

            try:
                self.assertEqual(request("/api/rooms", payload={})[0], 401)
                self.assertEqual(request("/api/rooms", "wrong", {})[0], 401)
                self.assertEqual(request("/api/tools/dice")[0], 401)
                self.assertEqual(request("/api/gm/commands")[0], 401)
                self.assertEqual(request("/api/tools/dice", token)[0], 200)
                status, created = request("/api/rooms", token, {})
                self.assertEqual(status, 201)
                path = f"/api/rooms/{created['room_id']}/messages"
                self.assertEqual(request(f"/api/rooms/{created['room_id']}/gm/commands", payload={"text": "Hello"})[0], 401)
                self.assertEqual(request(f"/api/rooms/{created['room_id']}/tools/dice", payload={})[0], 401)
                self.assertEqual(request(path, token, {
                    "sender": "first", "text": "Protected history", "request_id": str(uuid4()),
                })[0], 201)
                self.assertEqual(request(path + "?chat=first")[0], 401)
                self.assertEqual(request(path + "?chat=first", "wrong")[0], 401)
                status, history = request(path + "?chat=first", token)
                self.assertEqual(status, 200)
                self.assertEqual(history["messages"][0]["text"], "Protected history")
                self.assertEqual(request("/health")[0], 200)
            finally:
                server.shutdown()
                server.server_close()
                worker.join()


if __name__ == "__main__":
    unittest.main()
