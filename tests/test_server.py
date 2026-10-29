"""HTTP-level tests with a real ephemeral server and a temporary database."""

import json
from pathlib import Path
import secrets
import tempfile
from threading import Thread
import unittest
from unittest.mock import patch
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
        for path, marker in [("/", "Ваша история."), ("/debug", "Внутренние сущности"),
                             ("/debug/", "ИНСПЕКТОР КОМНАТЫ"), ("/debug.js", "class DebugPanel"),
                             ("/app.js", "class Chat"), ("/styles.css", ".chats")]:
            status, body = self.request(path)
            self.assertEqual(status, 200)
            self.assertIn(marker, body.decode())
        for path, marker in [('/battle', 'Очередь ходов'), ('/battle/', 'Ваше действие'),
                             ('/battle.js', 'renderQueue'), ('/battle.css', '.battle-layout'),
                             ('/room-session.js', 'createRoomSession')]:
            status, body = self.request(path)
            self.assertEqual(status, 200)
            self.assertIn(marker, body.decode())
        self.assertEqual(self.request("/health")[0], 200)

    def test_ai_context_step_debug_and_retry_with_stubbed_provider(self) -> None:
        from test_ai import FakeClient
        client = FakeClient()
        self.assertIn('patrol', json.loads(self.request('/api/combat/groups')[1])['groups'])
        room = self.server.service.store.create_room()
        actor = self.server.roster.execute(room, {'action': 'create_character', 'revision': 0,
                    'data': {'name': 'Боец', 'sheet': {'values': {'hp_max': 20, 'hp_current': 20}}}}, str(uuid4()))['entity_id']
        self.server.scene.execute(room, {'action': 'load_map', 'revision': 0,
                                        'parameters': {'template_id': 'tavern'}}, str(uuid4()))
        state = self.server.combat.execute(room, {'action': 'start', 'revision': 0, 'group_id': 'patrol',
                                                 'character_id': actor}, str(uuid4()))
        while state['turn'] != 2:
            state = self.server.combat.execute(room, {'action': 'end_turn', 'revision': state['revision']}, str(uuid4()))
        base = f'/api/rooms/{room}'
        with patch.object(self.server.ai, 'client', client):
            status, body = self.request('/api/ai')
            self.assertEqual(status, 200)
            self.assertTrue(json.loads(body)['free_only'])
            self.assertEqual(self.request(base + '/ai/npc/context')[0], 200)
            command = {'revision': state['revision']}
            headers = {'Idempotency-Key': str(uuid4())}
            status, body = self.request(base + '/ai/npc/step', command, headers)
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(body)['trace']['status'], 'completed')
            self.assertEqual(self.request(base + '/ai/npc/step', command, headers)[1], body)
            self.assertEqual(len(client.calls), 1)
            debug = json.loads(self.request(base + '/debug')[1])['entities']['ai']
            self.assertEqual(debug['requests'][0]['response']['model'], 'test/free')
            self.assertEqual(self.request(base + '/ai/npc/step', {**command, 'model': 'paid/model'}, headers)[0], 400)

    def test_gm_analysis_http_proposes_before_rolling(self) -> None:
        from test_gm_intents import GMClient
        client = GMClient();client.choice = 'demoralize_nonlinguistic_1'
        room = self.server.service.store.create_room();root = f'/api/rooms/{room}'
        actor = self.server.roster.execute(room, {'action':'create_character', 'revision':0,
            'data':{'name':'Герой', 'sheet':{'values':{'hp_max':20,'hp_current':20,'perception_other':100}}}}, str(uuid4()))['entity_id']
        state = self.server.combat.execute(room, {'action':'start','revision':0,'character_id':actor}, str(uuid4()))
        state = self.server.combat.execute(room, {'action':'intent','revision':state['revision'],'text':'Грозно кричу и бью мечом по щиту'}, str(uuid4()))
        body = {'revision':state['revision'], 'intent_id':state['intents'][-1]['id']};headers = {'Idempotency-Key':str(uuid4())}
        with patch.object(self.server.ai, 'client', client):
            status, raw = self.request(root+'/ai/gm/intent', body, headers)
            self.assertEqual(status, 200)
            result = json.loads(raw)['combat'];self.assertEqual(result['actions'], state['actions'])
            self.assertEqual(result['intents'][-1]['analysis']['effect'], 'demoralize')
            self.assertEqual(self.request(root+'/ai/gm/intent', body, headers)[1], raw)
            self.assertEqual(len(client.calls), 1)
            self.assertEqual(self.request(root+'/ai/gm/intent', {'revision':True}, headers)[0], 400)

    def test_game_and_debug_pages_have_separate_components(self) -> None:
        game = self.request("/")[1].decode()
        debug = self.request("/debug")[1].decode()
        self.assertIn('data-view="game"', game)
        self.assertIn('id="chat-template"', game)
        self.assertIn('id="inventory"', game)
        self.assertIn('href="/battle"', game)
        self.assertNotIn('id="combat"', game)
        self.assertNotIn('id="json-stub-template"', game)
        self.assertNotIn('id="debug-inspector"', game)
        self.assertIn('data-view="debug"', debug)
        self.assertIn('id="json-stub-template"', debug)
        self.assertIn('id="debug-inspector"', debug)
        self.assertIn('id="debug-log"', debug)
        self.assertNotIn('id="chat-template"', debug)
        self.assertNotIn('id="inventory"', debug)

    def test_battle_party_text_and_actor_binding_over_http(self) -> None:
        presets = json.loads(self.request('/api/combat/presets')[1])['presets']
        self.assertEqual({p['id'] for p in presets}, {'tavern_duel', 'forest_patrol', 'tavern_squad'})
        room = self.server.service.store.create_room()
        ids = []
        for name, perception in [('Первый', 30), ('Второй', 20)]:
            ids.append(self.server.roster.execute(room, {'action': 'create_character',
                'revision': self.server.roster.read(room)['revision'], 'data': {'name': name,
                'sheet': {'values': {'hp_max': 20, 'hp_current': 20, 'perception_other': perception}}}}, str(uuid4()))['entity_id'])
        path = f'/api/rooms/{room}/combat'
        status, body = self.request(path, {'action': 'start', 'revision': 0, 'character_ids': ids, 'preset_id': 'forest_patrol'}, {'Idempotency-Key': str(uuid4())})
        self.assertEqual(status, 200)
        state = json.loads(body)
        self.assertEqual(state['turn'], 0)
        self.assertEqual(state['preset_id'], 'forest_patrol')
        self.assertEqual(json.loads(self.request(f'/api/rooms/{room}/scene')[1])['map']['id'], 'forest_camp')
        menu_path = path + '/actions'
        status, raw_menu = self.request(menu_path + f"?revision={state['revision']}")
        self.assertEqual(status, 200)
        menu = json.loads(raw_menu)
        self.assertEqual(menu['actor_id'], ids[0])
        self.assertEqual([c['command']['action'] for c in menu['general']], ['shield', 'end_turn'])
        self.assertEqual(menu['selected'], [])
        self.assertEqual(self.request(menu_path + f"?revision={state['revision']}&x=1")[0], 400)
        headers = {'Idempotency-Key': str(uuid4())}
        command = {'action': 'intent', 'revision': state['revision'], 'actor_id': ids[0], 'text': 'Щит'}
        status, body = self.request(path, command, headers)
        self.assertEqual(status, 200)
        self.assertEqual(self.request(path, command, headers)[1], body)
        state = json.loads(body)
        self.assertEqual(state['actions'], 2)
        self.assertEqual(self.request(menu_path + '?revision=0')[0], 409)
        current_menu = json.loads(self.request(menu_path + f"?revision={state['revision']}")[1])
        self.assertEqual([c['command']['action'] for c in current_menu['general']], ['end_turn'])
        self.assertEqual(state['journal'][-1]['source_text'], 'Щит')
        wrong = {'action': 'intent', 'revision': state['revision'], 'actor_id': ids[1], 'text': 'Щит'}
        self.assertEqual(self.request(path, wrong, {'Idempotency-Key': str(uuid4())})[0], 400)
        status, body = self.request(path, {**wrong, 'actor_id': ids[0], 'text': 'Обрушу потолок'}, {'Idempotency-Key': str(uuid4())})
        self.assertEqual(status, 200)
        state = json.loads(body)
        self.assertEqual(state['actions'], 2)
        self.assertEqual(state['intents'][0]['status'], 'pending')

    def test_debug_api_returns_full_log_and_validates_cursors(self) -> None:
        room = json.loads(self.request("/api/rooms", {})[1])["room_id"]
        path = f"/api/rooms/{room}"
        payload = {"sender": "second", "text": "Скрытое событие", "request_id": str(uuid4()),
                   "visibility": {"mode": "selected", "chat_ids": ["second"]}}
        message = json.loads(self.request(path + "/messages", payload)[1])["message"]
        self.assertEqual(json.loads(self.request(path + "/messages?chat=first")[1])["messages"], [])
        status, body = self.request(path + "/debug")
        self.assertEqual(status, 200)
        snapshot = json.loads(body)
        self.assertEqual(snapshot["entities"]["room"]["id"], room)
        self.assertEqual(snapshot["queue"]["messages"], [message])
        self.assertEqual(json.loads(self.request(path + f"/debug?after={message['id']}")[1])["queue"]["messages"], [])
        for cursor in ("-1", "abc", "999999999999999999999"):
            self.assertEqual(self.request(path + f"/debug?after={cursor}")[0], 400)
        self.assertEqual(self.request(f"/api/rooms/{uuid4()}/debug")[0], 404)

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

    def test_roster_http_flow_validation_and_idempotency(self) -> None:
        room = json.loads(self.request("/api/rooms", {})[1])["room_id"]
        path = f"/api/rooms/{room}/roster"
        state = json.loads(self.request(path)[1])
        self.assertEqual(state["revision"], 0)
        self.assertEqual(state["rules"]["id"], "pf2e-remaster")
        headers = {"Idempotency-Key": str(uuid4())}
        payload = {"action": "create_character", "revision": 0, "data": {"name": "Страж", "kind": "npc"}}
        self.assertEqual(self.request(path, payload)[0], 400)
        status, response = self.request(path, payload, headers)
        self.assertEqual(status, 200)
        actor = json.loads(response)["entity_id"]
        self.assertEqual(json.loads(self.request(path, payload, headers)[1]), json.loads(response))
        self.assertEqual(self.request(path, payload, {"Idempotency-Key": str(uuid4())})[0], 409)
        self.assertEqual(self.request(path, {**payload, "data": {"name": "Другой"}}, headers)[0], 409)
        item = {"action": "add_item", "revision": 1, "character_id": actor,
                "data": {"name": "Меч", "category": "weapon", "bulk": "1", "state": "held"}}
        self.assertEqual(self.request(path, item, {"Idempotency-Key": str(uuid4())})[0], 200)
        state = json.loads(self.request(path)[1])
        self.assertEqual(state["characters"][0]["items"][0]["name"], "Меч")
        self.assertEqual(state["characters"][0]["summary"]["hands_used"], 1)
        self.assertEqual(self.request(path, {**item, "revision": 2, "data": {**item["data"], "quantity": True}},
                                      {"Idempotency-Key": str(uuid4())})[0], 400)
        self.assertEqual(json.loads(self.request(path)[1]), state)
        self.assertEqual(self.request(f"/api/rooms/{uuid4()}/roster")[0], 404)

    def test_inventory_assets_and_license_are_served(self) -> None:
        for path, marker in [("/inventory.js", b"class InventoryPanel"),
                             ("/character-sheet.js", b"class CharacterSheetWindow"),
                             ("/character-ORC-NOTICE.md", b"Attribution Notice"),
                             ("/inventory-ORC-NOTICE.md", b"Attribution Notice"),
                             ("/inventory-LICENSE-ORC.pdf", b"%PDF")]:
            status, body = self.request(path)
            self.assertEqual(status, 200)
            self.assertIn(marker, body)

    def test_character_sheet_http_updates_and_returns_calculated_statistics(self) -> None:
        room = json.loads(self.request("/api/rooms", {})[1])["room_id"]
        path = f"/api/rooms/{room}/roster"
        payload = {"action": "create_character", "revision": 0,
                   "data": {"name": "Герой", "attributes": {"wisdom": 3}, "sheet": {"values": {
                       "level": 4, "perception_rank": "trained", "hp_max": 30, "hp_current": 25,
                   }}}}
        self.assertEqual(self.request(path, payload, {"Idempotency-Key": str(uuid4())})[0], 200)
        state = json.loads(self.request(path)[1])
        self.assertEqual(state["character_rules"]["default_profiles"]["npc"], "npc")
        actor = state["characters"][0]
        self.assertEqual(actor["sheet_statistics"]["perception"], 9)
        invalid = {"action": "update_character", "revision": 1, "id": actor["id"],
                   "data": {"sheet": {"values": {"hp_current": 31}}}}
        self.assertEqual(self.request(path, invalid, {"Idempotency-Key": str(uuid4())})[0], 400)
        self.assertEqual(json.loads(self.request(path)[1]), state)
        debug = json.loads(self.request(f"/api/rooms/{room}/debug")[1])
        self.assertEqual(debug["entities"]["character_rules"], state["character_rules"])
        self.assertEqual(debug["entities"]["characters"][0]["sheet_statistics"]["perception"], 9)
        # A filled sheet can exceed the old 16 KiB transport limit in UTF-8 or escaped JSON.
        description = {key: "Я" * 1000 for key in ("attacks", "abilities", "spells", "description", "defenses")}
        update = {"action": "update_character", "revision": 1, "id": actor["id"],
                  "data": {"sheet": {"values": description}}}
        self.assertEqual(self.request(path, update, {"Idempotency-Key": str(uuid4())})[0], 200)
        self.assertEqual(json.loads(self.request(path)[1])["characters"][0]["sheet"]["values"]["description"], "Я" * 1000)

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
                self.assertEqual(request('/api/ai')[0], 401)
                self.assertEqual(request("/api/tools/dice", token)[0], 200)
                status, created = request("/api/rooms", token, {})
                self.assertEqual(status, 201)
                path = f"/api/rooms/{created['room_id']}/messages"
                roster_path = f"/api/rooms/{created['room_id']}/roster"
                ai_path = f"/api/rooms/{created['room_id']}/ai/npc"
                self.assertEqual(request(ai_path + '/context')[0], 401)
                self.assertEqual(request(f"/api/rooms/{created['room_id']}/ai/gm/intent", payload={'revision':0,'intent_id':'x'})[0], 401)
                self.assertEqual(request(ai_path + '/step', payload={'revision': 0})[0], 401)
                self.assertEqual(request(f"/api/rooms/{created['room_id']}/combat/actions?revision=0")[0], 401)
                self.assertEqual(request(roster_path)[0], 401)
                self.assertEqual(request(roster_path, payload={})[0], 401)
                self.assertEqual(request(roster_path, token)[0], 200)
                debug_path = f"/api/rooms/{created['room_id']}/debug"
                self.assertEqual(request(debug_path)[0], 401)
                self.assertEqual(request(debug_path, "wrong")[0], 401)
                self.assertEqual(request(debug_path, token)[0], 200)
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
