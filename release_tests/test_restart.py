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
    def test_party_turn_intent_journal_and_receipt_survive_restart(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            token = secrets.token_urlsafe(32)
            (directory / 'access-token').write_text(token)

            def request(base, path, payload=None, key=None):
                headers = {'Content-Type': 'application/json', 'Authorization': 'Bearer ' + token}
                if key: headers['Idempotency-Key'] = key
                with urlopen(Request(base + path, data=None if payload is None else json.dumps(payload).encode(), headers=headers), timeout=5) as response:
                    return json.load(response)

            with running_server(directory) as base:
                room = request(base, '/api/rooms', {})['room_id']
                root = f'/api/rooms/{room}'
                ids = []
                for index, perception in enumerate((100, 0)):
                    ids.append(request(base, root + '/roster', {'action': 'create_character', 'revision': index,
                        'data': {'name': f'Герой {index+1}', 'sheet': {'values': {
                            'hp_max': 20, 'hp_current': 20, 'perception_other': perception}}}}, str(uuid4()))['entity_id'])
                state = request(base, root + '/combat', {'action': 'start', 'revision': 0, 'character_ids': ids, 'preset_id': 'forest_patrol'}, str(uuid4()))
                state = request(base, root + '/combat', {'action': 'intent', 'revision': state['revision'], 'actor_id': ids[0], 'text': 'Щит'}, str(uuid4()))
                operation = str(uuid4())
                command = {'action': 'intent', 'revision': state['revision'], 'actor_id': ids[0], 'text': 'Попробую договориться о перемирии'}
                receipt = request(base, root + '/combat', command, operation)
                state = request(base, root + '/combat', {'action': 'end_turn', 'revision': receipt['revision'], 'actor_id': ids[0]}, str(uuid4()))
                while state['turn'] != 1:
                    state = request(base, root + '/combat', {'action': 'end_turn', 'revision': state['revision'],
                        'actor_id': state['fighters'][state['turn']]['entity_id']}, str(uuid4()))
                scene = request(base, root + '/scene')
                history = request(base, root + '/messages?chat=first')

            with running_server(directory) as base:
                self.assertEqual(request(base, root + '/combat'), state)
                self.assertEqual(request(base, root + '/scene'), scene)
                self.assertEqual(scene['map']['id'], 'forest_camp')
                self.assertEqual(state['preset_id'], 'forest_patrol')
                self.assertEqual(request(base, root + '/combat', command, operation), receipt)
                self.assertEqual(request(base, root + '/combat'), state)
                self.assertEqual(request(base, root + '/messages?chat=first'), history)
                self.assertEqual(state['turn'], 1)
                self.assertEqual(state['intents'][0]['status'], 'pending')
                self.assertTrue(any(e.get('source_text') == command['text'] for e in state['journal']))
                state = request(base, root + '/combat', {'action': 'intent', 'revision': state['revision'], 'actor_id': ids[1], 'text': 'Щит'}, str(uuid4()))
                self.assertTrue(state['fighters'][1]['shield'])
                self.assertEqual(state['journal'][-1]['actor'], 'Герой 2')
                self.assertEqual([c['sheet']['values']['hp_current'] for c in request(base, root + '/roster')['characters']], [20, 20])

    def test_inventory_and_retry_survive_process_restart(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            token = secrets.token_urlsafe(32)
            (directory / "access-token").write_text(token)

            def request(base, path, payload=None, key=None):
                data = None if payload is None else json.dumps(payload).encode()
                headers = {"Content-Type": "application/json", "Authorization": "Bearer " + token}
                if key:
                    headers["Idempotency-Key"] = key
                with urlopen(Request(base + path, data=data, headers=headers), timeout=5) as response:
                    return json.load(response)

            with running_server(directory) as base:
                room = request(base, "/api/rooms", {})["room_id"]
                path = f"/api/rooms/{room}/roster"
                player = request(base, path, {"action": "create_player", "revision": 0,
                                 "data": {"name": "Игрок"}}, str(uuid4()))["entity_id"]
                actor = request(base, path, {"action": "create_character", "revision": 1,
                                "data": {"name": "Герой", "player_id": player,
                                         "attributes": {"wisdom": 2},
                                         "sheet": {"values": {"level": 3, "hp_max": 25, "hp_current": 18,
                                                               "perception_rank": "trained"}}}}, str(uuid4()))["entity_id"]
                request_id = str(uuid4())
                item = {"action": "add_item", "revision": 2, "character_id": actor,
                        "data": {"name": "Кинжал", "category": "weapon", "bulk": "L", "state": "held"}}
                receipt = request(base, path, item, request_id)
                state = request(base, path)
                debug_path = f"/api/rooms/{room}/debug"
                debug = request(base, debug_path)

            with running_server(directory) as base:
                self.assertEqual(request(base, path), state)
                self.assertEqual(request(base, debug_path), debug)
                self.assertEqual(request(base, path, item, request_id), receipt)
                self.assertEqual(len(request(base, path)["characters"][0]["items"]), 1)
                self.assertEqual(state["characters"][0]["summary"]["hands_used"], 1)
                self.assertEqual(state["characters"][0]["sheet"]["values"]["hp_current"], 18)
                self.assertEqual(state["characters"][0]["sheet_statistics"]["perception"], 7)

    def test_gm_transport_retry_survives_process_restart(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            token = secrets.token_urlsafe(32)
            (directory / "access-token").write_text(token)
            operation = str(uuid4())
            command = {"action": "roll", "roll": "1d20+5", "target": 15}

            def request(base, path, payload=None, key=None):
                data = None if payload is None else json.dumps(payload).encode()
                headers = {"Content-Type": "application/json", "Authorization": "Bearer " + token}
                if key:
                    headers["Idempotency-Key"] = key
                with urlopen(Request(base + path, data=data, headers=headers), timeout=5) as response:
                    return json.load(response)

            with running_server(directory) as base:
                room = request(base, "/api/rooms", {})["room_id"]
                path = f"/api/rooms/{room}/gm/commands"
                result = request(base, path, command, operation)
                history = request(base, f"/api/rooms/{room}/messages?chat=second")["messages"]
                self.assertEqual(len(history), 2)

            with running_server(directory) as base:
                self.assertEqual(request(base, path, command, operation), result)
                self.assertEqual(request(base, f"/api/rooms/{room}/messages?chat=second")["messages"], history)

    def test_dice_offer_waits_for_player_across_process_restarts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            token = secrets.token_urlsafe(32)
            (directory / "access-token").write_text(token)

            def request(base, path, payload=None):
                data = None if payload is None else json.dumps(payload).encode()
                headers = {"Content-Type": "application/json", "Authorization": "Bearer " + token}
                with urlopen(Request(base + path, data=data, headers=headers), timeout=5) as response:
                    return json.load(response)

            with running_server(directory) as base:
                room = request(base, "/api/rooms", {})["room_id"]
                path = f"/api/rooms/{room}/messages"
                offer_payload = {"sender": "second", "type": "tool.offer", "tool": "dice.roll",
                                 "recipient": "first", "request_id": str(uuid4()), "text": "Проверьте скрытность.",
                                 "arguments": {"system": "d20", "roll": "1d20+5", "success": {"operator": ">=", "target": 15}}}
                offer = request(base, path, offer_payload)["message"]
                self.assertEqual(request(base, path + "?chat=first")["messages"], [offer])

            with running_server(directory) as base:
                self.assertEqual(request(base, path + "?chat=first")["messages"], [offer])
                self.assertEqual(request(base, path, offer_payload)["message"], offer)
                accept = {"sender": "first", "type": "tool.call", "tool": "dice.roll",
                          "request_id": offer["roll_request_id"], "offer_id": offer["request_id"]}
                result = request(base, path, accept)
                history = request(base, path + "?chat=first")["messages"]
                self.assertEqual([event["type"] for event in history], ["tool.offer", "tool.call", "tool.result"])
                self.assertEqual(result["message"]["requested_by"], "first")

            with running_server(directory) as base:
                self.assertEqual(request(base, path, accept), result)
                self.assertEqual(request(base, path + "?chat=first")["messages"], history)

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
                dice_payload = {"sender": "first", "request_id": str(uuid4()),
                                "arguments": {"system": "d20", "roll": "1d20+5", "success": {"operator": ">=", "target": 15}}}
                dice_path = f"/api/rooms/{room}/tools/dice"
                dice = request(base, dice_path, dice_payload)["message"]
                call = request(base, path + "?chat=first")["messages"][-2]
                self.assertEqual(call["type"], "tool.call")
                self.assertEqual(dice["tool_call_id"], call["request_id"])

            with running_server(directory) as base:
                first_view = request(base, path + "?chat=first")
                second_view = request(base, path + "?chat=second")
                self.assertEqual(first_view["messages"], [first, second, call, dice])
                self.assertEqual(second_view["messages"], [first, call, dice])
                self.assertEqual(request(base, dice_path, dice_payload)["message"], dice)
                self.assertEqual(request(base, path, shared)["message"], first)
                self.assertEqual(len(request(base, path + "?chat=first")["messages"]), 4)
                with self.assertRaises(HTTPError) as denied:
                    request(base, path + "?chat=first", authenticated=False)
                self.assertEqual(denied.exception.code, 401)
                denied.exception.close()


if __name__ == "__main__":
    unittest.main()
