"""Finite stock, payment and a personal quote across real process restarts."""
import json
from pathlib import Path
import tempfile
import unittest
from urllib.request import Request, urlopen
from uuid import uuid4

from iirpg.server import ChatHTTPServer
from test_restart import running_server


class TradeRestartTests(unittest.TestCase):
    def test_payment_inventory_and_pending_quote_survive_process_restart(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            token = 'restart-action-plans-testing-token-123456789'
            (directory / 'access-token').write_text(token)
            server = ChatHTTPServer(('127.0.0.1', 0), directory / 'chat.sqlite3')
            class Client:
                steps = []
                def ready(self):return True
                def complete(self, *args):return {'content': json.dumps({'intent': 'act', 'reply': '', 'steps': self.steps})}
            client = Client(); server.ai.client = client; server.ai.config['session_narration'] = False
            try:
                room = server.games.execute({'action': 'create', 'scenario': 'crossroads'}, str(uuid4()))['room_id']
                actor = server.adventure.read(room)['character_id']
                def act(steps):
                    client.steps = steps
                    return server.adventure.execute(room, {'revision': server.adventure.read(room)['revision'],
                        'character_id': actor, 'text': 'Выполняю действие.'}, str(uuid4()))
                act([{'rule': 'buy', 'target': 'copper_cup'}])
                before = act([{'rule': 'bargain', 'target': 'travel_rope'}])
                inventory = server.roster.read(room)
                pending = before['pending_check']
            finally:
                server.server_close()
            root = '/api/rooms/' + room
            def request(base, path, body=None):
                with urlopen(Request(base + root + path, data=None if body is None else json.dumps(body).encode(),
                        headers={'Content-Type': 'application/json', 'Authorization': 'Bearer ' + token}), timeout=5) as response:
                    return json.load(response)
            dice = {'sender': 'first', 'offer_id': pending['offer_id'], 'request_id': pending['roll_request_id']}
            with running_server(directory) as base:
                self.assertEqual(request(base, '/session'), before)
                self.assertEqual(request(base, '/roster'), inventory)
                self.assertEqual(inventory['characters'][0]['coins'], 280)
                self.assertEqual(before['world']['entities']['mara']['trade']['cash_cp'], 920)
                request(base, '/tools/dice', dice)
                after = request(base, '/session')
                self.assertNotIn('pending_check', after)
                self.assertIn('travel_rope', after['world']['entities']['mara']['trade']['quotes'][actor])
                self.assertEqual(after['world']['entities']['copper_cup']['owner'], actor)
                self.assertEqual(request(base, '/roster'), inventory)
                request(base, '/tools/dice', dice)
                self.assertEqual(request(base, '/session'), after)
            with running_server(directory) as base:
                self.assertEqual(request(base, '/session'), after)
                self.assertEqual(request(base, '/roster'), inventory)
