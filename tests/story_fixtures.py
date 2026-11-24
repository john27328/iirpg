"""Offline GM boundary and player-driven dice, shared by session tests."""
import json
from uuid import uuid4
from copy import deepcopy
import test_gm_intents
from iirpg.adventure import AdventureStore
from iirpg.ai import AIService, load_config
from iirpg.application import ChatService
from iirpg.dice import DiceTool


class Client:
    def __init__(self):
        self.calls = []
        self.proposal = {'intent': 'question', 'reply': 'Собеседник отвечает на вопрос.', 'steps': []}
    def ready(self): return True
    def complete(self, payload, timeout):
        self.calls.append(json.loads(payload['messages'][1]['content']))
        return {'content': json.dumps(self.proposal, ensure_ascii=False)}


class StoryFixture:
    command = test_gm_intents.GMIntentTests.command
    roster_command = test_gm_intents.GMIntentTests.roster_command
    def setUp(self):
        test_gm_intents.GMIntentTests.setUp(self)
        self.client = Client()
        self.session = AdventureStore(AIService(self.combat, self.scene, load_config(), self.client))
        self.session.ai.config['session_narration'] = False
    def say(self, text, *steps, intent='act', reply='Пробуем выполнить замысел.', key=None):
        self.client.proposal = {'intent': intent, 'reply': reply, 'steps': list(steps)}
        state = self.session.read(self.room)
        return self.session.execute(self.room, {'revision': state['revision'], 'character_id': self.actor, 'text': text}, key or str(uuid4()))
    def roll_pending(self, session, state, natural=20):
        p = state['pending_check']
        service = ChatService(self.store, DiceTool(roll_die=lambda sides: natural))
        command = {'sender': 'first', 'type': 'tool.call', 'tool': 'dice.roll', 'request_id': p['roll_request_id'], 'offer_id': p['offer_id']}
        service.send(self.room, command)
        after = session.read(self.room)
        service.send(self.room, command)
        self.assertEqual(session.read(self.room), after)
        return after
    def hero(self): return next(a for a in self.roster.read(self.room)['characters'] if a['id'] == self.actor)
    def edit(self, fn):
        self.session.read(self.room)
        with self.store._connection() as c:
            state = self.session._load(c, self.room); fn(state)
            self.session.save(c, self.room, state)
