"""AI boundaries are stubbed; this suite never contacts model providers."""

from copy import deepcopy
import io
import json
import os
from pathlib import Path
from threading import Event, Thread
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError
from uuid import uuid4

import test_scene
from iirpg.ai import AIService, OpenRouterClient, load_config
from iirpg.application import ChatService
from iirpg.combat import CombatStore
from iirpg.storage import MessageConflict


class FakeClient:
    def __init__(self):
        self.calls = []
        self.choose = lambda context: 'shield'

    def ready(self):
        return True

    def complete(self, payload, timeout):
        self.calls.append(deepcopy(payload))
        context = json.loads(payload['messages'][1]['content'])
        return {'model': 'test/free', 'content': json.dumps({'choice': self.choose(context)}),
                'usage': {'prompt_tokens': 10, 'completion_tokens': 5}, 'finish_reason': 'stop'}


class AITests(unittest.TestCase):
    command = test_scene.SceneTests.command

    def setUp(self):
        test_scene.SceneTests.setUp(self)
        self.roster.execute(self.room, {'action': 'update_character', 'id': self.actor,
                            'revision': self.roster.read(self.room)['revision'],
                            'data': {'sheet': {'values': {'hp_max': 100, 'hp_current': 100}}}}, str(uuid4()))
        self.command('load_map', parameters={'template_id': 'tavern'})
        self.command('join')
        self.combat = CombatStore(self.roster, lambda sides: 10 if sides == 20 else 1)
        self.battle = self.combat.execute(self.room, {'action': 'start', 'revision': 0,
                                                    'character_id': self.actor}, str(uuid4()))
        self.assertEqual(self.battle['turn'], 1)
        self.client = FakeClient()
        self.config = load_config()
        self.ai = AIService(self.combat, self.scene, self.config, self.client)

    def step(self, key=None):
        return self.ai.step(self.room, {'revision': self.combat.read(self.room)['revision']}, key or str(uuid4()))

    def test_context_is_explicit_and_contains_only_available_actions(self):
        self.command('propose', text='СЕКРЕТНОЕ намерение ведущего')
        ChatService(self.store).send(self.room, {'request_id': str(uuid4()),
            'type': 'chat.message', 'text': 'СЕКРЕТНЫЙ ЧАТ', 'sender': 'second',
            'visibility': {'mode': 'selected', 'chat_ids': ['second']}})
        preview = self.ai.preview(self.room)
        context = preview['context']
        self.assertEqual(context['role'], 'npc')
        self.assertEqual(context['mode'], 'local_observations')
        self.assertNotIn('СЕКРЕТ', json.dumps(context, ensure_ascii=False))
        actions = context['available_actions']
        self.assertNotIn('strike', actions)
        self.assertTrue(any(k.startswith('move_') for k in actions))
        self.assertNotIn('enemy_turn', actions)
        self.assertNotIn('hp', context['opponents'][0])
        self.assertNotIn('ac', context['opponents'][0])
        self.assertIn('current_intent', context)
        self.assertTrue(any(outcome['can_strike_after_move'] for outcome in context['movement_outcomes'].values()))
        self.assertEqual(self.ai.traces(self.room), [])
        self.assertEqual(self.client.calls, [])

    def test_decision_executes_once_and_survives_restart(self):
        key = str(uuid4())
        command = {'revision': self.battle['revision']}
        response = self.ai.step(self.room, command, key)
        self.assertEqual(response['combat']['actions'], 2)
        self.assertTrue(response['combat']['fighters'][1]['shield'])
        self.assertEqual(response['trace']['status'], 'completed')
        self.assertEqual(response['trace']['response']['model'], 'test/free')
        before_events = self.store.page(self.room)
        restarted = AIService(self.combat, self.scene, self.config, self.client)
        self.assertEqual(restarted.step(self.room, command, key), response)
        self.assertEqual(self.store.page(self.room), before_events)
        self.assertEqual(len(self.client.calls), 1)
        with self.assertRaises(MessageConflict):
            restarted.step(self.room, {'revision': 500}, key)
        payload = self.client.calls[0]
        self.assertEqual(payload['provider']['max_price'], {'prompt': 0, 'completion': 0})
        self.assertTrue(payload['provider']['require_parameters'])
        self.assertEqual(payload['reasoning'], {'enabled': False})
        self.assertEqual(payload['response_format']['json_schema']['schema']['properties']['choice']['enum'],
                         list(response['trace']['context']['available_actions']))

    def test_invalid_decisions_and_provider_errors_do_not_change_combat_or_retry(self):
        for content in ('not JSON', '{"choice":"invented"}', '{"choice":"shield","hp":100}', '[]'):
            self.client.complete = lambda *args: {'model': 'stub', 'content': content}
            before = self.combat.read(self.room)
            key = str(uuid4())
            with self.assertRaisesRegex(ValueError, 'допустимое'):
                self.step(key)
            self.assertEqual(self.combat.read(self.room), before)
            with self.assertRaises(ValueError):
                self.step(key)
            self.assertEqual(self.ai.traces(self.room)[0]['status'], 'failed')
        def fail(*args):
            raise ValueError('Исчерпана квота OpenRouter.')
        self.client.complete = fail
        with self.assertRaisesRegex(ValueError, 'квота'):
            self.step()
        self.assertIn('квота', self.ai.traces(self.room)[0]['error'])

    def test_stale_response_is_not_applied(self):
        before = self.battle
        def decide(context):
            self.combat.execute(self.room, {'revision': before['revision'], 'action': 'end_turn'}, str(uuid4()))
            return 'shield'
        self.client.choose = decide
        key = str(uuid4())
        with self.assertRaises(MessageConflict):
            self.step(key)
        state = self.combat.read(self.room)
        self.assertEqual(state['turn'], 0)
        self.assertFalse(state['fighters'][1]['shield'])
        self.assertEqual(self.ai.traces(self.room)[0]['status'], 'stale')
        with self.assertRaises(ValueError):
            self.ai.step(self.room, {'revision': before['revision']}, key)
        self.assertEqual(len(self.client.calls), 1)

    def test_parallel_and_repeated_running_request_cannot_call_provider_twice(self):
        entered, release = Event(), Event()
        errors = []
        key = str(uuid4())
        def choose(context):
            entered.set()
            if not release.wait(3):
                raise ValueError('test timeout')
            return 'shield'
        self.client.choose = choose
        def worker():
            try:
                self.step(key)
            except Exception as error:
                errors.append(error)
        thread = Thread(target=worker)
        thread.start()
        try:
            self.assertTrue(entered.wait(3))
            with self.assertRaises(MessageConflict):
                self.step(key)
            with self.assertRaises(MessageConflict):
                self.step()
        finally:
            release.set()
            thread.join(3)
        self.assertEqual(errors, [])
        self.assertEqual(len(self.client.calls), 1)

    def test_saved_choice_resumes_after_interruption_without_model_or_duplicate_action(self):
        response = self.step()
        trace = response['trace']
        trace.update(status='chosen')
        del trace['result']
        self.ai._save(self.room, trace['request_id'], trace)
        before = self.combat.read(self.room)
        recovered = self.ai.step(self.room, {'revision': trace['revision']}, trace['request_id'])
        self.assertEqual(recovered['combat'], before)
        self.assertEqual(len(self.client.calls), 1)

    def test_limits_and_wrong_turn_are_checked_before_network(self):
        self.config['daily_request_limit'] = 1
        self.step()
        with self.assertRaisesRegex(ValueError, 'дневной лимит'):
            self.step()
        self.assertEqual(len(self.client.calls), 1)
        state = self.combat.read(self.room)
        self.combat.execute(self.room, {'revision': state['revision'], 'action': 'end_turn'}, str(uuid4()))
        with self.assertRaisesRegex(ValueError, 'ход противника'):
            self.step()
        other = self.store.create_room()
        with self.assertRaises(KeyError):
            self.ai.preview(str(uuid4()))
        self.assertEqual(self.ai.traces(other), [])

    def test_context_budget_and_missing_key_do_not_spend_quota(self):
        self.config['context_max_bytes'] = 10
        with self.assertRaisesRegex(ValueError, 'бюджет'):
            self.step()
        self.config['context_max_bytes'] = 14000
        self.client.ready = lambda: False
        with self.assertRaisesRegex(ValueError, 'ключ'):
            self.step()
        self.assertEqual(self.ai.status()['requests_today'], 0)

    def test_interrupted_request_never_resends_even_after_lease_expiry(self):
        initial = 1800000000.0
        self.ai.now = lambda: initial
        def interrupted(*args):
            raise KeyboardInterrupt()
        self.client.complete = interrupted
        key = str(uuid4())
        with self.assertRaises(KeyboardInterrupt):
            self.step(key)
        self.assertEqual(self.ai.traces(self.room)[0]['status'], 'running')
        self.ai.now = lambda: initial + 121
        self.client.complete = lambda *args: self.fail('Interrupted request was resent')
        with self.assertRaisesRegex(ValueError, 'неизвестен'):
            self.step(key)
        self.assertEqual(self.combat.read(self.room), self.battle)


class OpenRouterClientTests(unittest.TestCase):
    @patch.dict(os.environ, {}, clear=True)
    def test_key_file_can_be_added_and_rotated_without_restart(self):
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            key_file = Path(directory) / 'key'
            client = OpenRouterClient(key_file)
            self.assertFalse(client.ready())
            key_file.write_text('first-fake-key-123456\n')
            self.assertEqual(client.key(), 'first-fake-key-123456')
            key_file.write_text('second-fake-key-123456\n')
            self.assertEqual(client.key(), 'second-fake-key-123456')

    @patch.dict(os.environ, {'OPENROUTER_API_KEY': 'secret-test-key-never-real'})
    def test_wire_format_and_sanitized_errors(self):
        client = OpenRouterClient(Path('/unused'))
        body = json.dumps({'model': 'free-model', 'choices': [{'message': {'content': '{"choice":"shield"}'},
                                                              'finish_reason': 'stop'}], 'usage': {'total_tokens': 20}}).encode()
        with patch('iirpg.ai.urlopen', return_value=io.BytesIO(body)) as send:
            response = client.complete({'model': 'openrouter/free'}, 15)
        request = send.call_args.args[0]
        self.assertEqual(request.full_url, 'https://openrouter.ai/api/v1/chat/completions')
        self.assertEqual(request.get_header('Authorization'), 'Bearer secret-test-key-never-real')
        self.assertNotIn('secret-test-key', json.dumps(response))
        exhausted = {'model': 'free-model', 'choices': [{'message': {'content': None}, 'finish_reason': 'length'}],
                     'usage': {'completion_tokens': 256}}
        with patch('iirpg.ai.urlopen', return_value=io.BytesIO(json.dumps(exhausted).encode())):
            response = client.complete({}, 15)
        self.assertEqual(response['content'], '')
        self.assertEqual(response['finish_reason'], 'length')
        self.assertEqual(response['usage']['completion_tokens'], 256)
        for error in (HTTPError(request.full_url, 429, 'secret-test-key', {}, None), URLError('secret-test-key')):
            with patch('iirpg.ai.urlopen', side_effect=error):
                with self.assertRaises(ValueError) as caught:
                    client.complete({}, 15)
            self.assertNotIn('secret-test-key', str(caught.exception))

    def test_only_free_model_configuration_is_accepted(self):
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'ai.json'
            config = load_config()
            for model in ('paid/model', 'openrouter/auto', ''):
                path.write_text(json.dumps({**config, 'model': model}))
                with self.assertRaises(ValueError):
                    load_config(path)
