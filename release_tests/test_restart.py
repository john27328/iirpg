"""Opt-in checks of the real server process; no external services required."""

from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys
import tempfile
import time
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]


@contextmanager
def running_server(directory: Path):
    log = directory / "server.log"
    with log.open("w") as output:
        process = subprocess.Popen(
            [sys.executable, "-m", "iirpg", "--port", "0",
             "--database", str(directory / "chat.sqlite3"),
             "--access-token-file", str(directory / "access-token")],
            cwd=ROOT,
            env={**os.environ, "PYTHONPATH": str(ROOT / "src")},
            stdout=output, stderr=subprocess.STDOUT,
        )
        try:
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                match = re.search(r"IIRPG Chat: (http://127\.0\.0\.1:\d+)", log.read_text())
                if match:
                    yield match.group(1)
                    break
                if process.poll() is not None:
                    raise AssertionError(f"Server exited during startup: {log.read_text()}")
                time.sleep(0.02)
            else:
                raise AssertionError(f"Server did not start within 5 seconds: {log.read_text()}")
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


class ReleaseTests(unittest.TestCase):
    def test_history_visibility_and_retry_survive_process_restart(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            token = secrets.token_urlsafe(32)
            (directory / "access-token").write_text(token)

            def request(base: str, path: str, payload: dict | None = None, authenticated: bool = True):
                headers = {"Content-Type": "application/json"}
                if authenticated:
                    headers["Authorization"] = "Bearer " + token
                data = None if payload is None else json.dumps(payload).encode()
                with urlopen(Request(base + path, data=data, headers=headers), timeout=5) as response:
                    return json.load(response)

            shared = {"sender": "first", "text": "Shared", "request_id": str(uuid4()),
                      "choices": ["Continue", "Look around"]}
            restricted = {"sender": "first", "text": "First only", "request_id": str(uuid4()),
                          "visibility": {"mode": "selected", "chat_ids": ["first"]}}
            with running_server(directory) as base:
                room = request(base, "/api/rooms", {})["room_id"]
                path = f"/api/rooms/{room}/messages"
                first = request(base, path, shared)["message"]
                second = request(base, path, restricted)["message"]

            with running_server(directory) as base:
                first_view = request(base, path + "?chat=first")
                second_view = request(base, path + "?chat=second")
                self.assertEqual(first_view["messages"], [first, second])
                self.assertEqual(second_view["messages"], [first])
                self.assertEqual(request(base, path, shared)["message"], first)
                self.assertEqual(len(request(base, path + "?chat=first")["messages"]), 2)
                with self.assertRaises(HTTPError) as denied:
                    request(base, path + "?chat=first", authenticated=False)
                self.assertEqual(denied.exception.code, 401)
                denied.exception.close()


if __name__ == "__main__":
    unittest.main()
