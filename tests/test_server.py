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

from iirpg.server import ChatHTTPServer


class ServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.directory = tempfile.TemporaryDirectory()
        cls.server = ChatHTTPServer(("127.0.0.1", 0), Path(cls.directory.name) / "chat.sqlite3")
        cls.worker = Thread(target=cls.server.serve_forever, daemon=True)
        cls.worker.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.worker.join()
        cls.directory.cleanup()

    def request(self, path: str, payload: object = None) -> tuple[int, bytes]:
        data = None if payload is None else json.dumps(payload).encode()
        request = Request(self.base + path, data=data, headers={"Content-Type": "application/json"})
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
                status, created = request("/api/rooms", token, {})
                self.assertEqual(status, 201)
                path = f"/api/rooms/{created['room_id']}/messages"
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
