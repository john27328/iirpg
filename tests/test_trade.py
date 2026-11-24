"""Trade is a prototype house rule; no external AI or live price sources."""
from copy import deepcopy
import json
from unittest.mock import patch
import unittest
from uuid import uuid4

import test_multiplayer as multiplayer_fixture
from iirpg.action_rules import apply, attitude, prepare
from iirpg.session_context import context
from iirpg.session_world import persist_effects
from iirpg.trade import trade_view


class TradeTests(unittest.TestCase):
    setUp = multiplayer_fixture.MultiplayerTests.setUp
    request = multiplayer_fixture.MultiplayerTests.request
    say = multiplayer_fixture.MultiplayerTests.say
    roll = multiplayer_fixture.MultiplayerTests.roll

    def create(self, count):
        status, result = self.request('/api/games', {'action': 'create', 'scenario': 'crossroads', 'name': 'Переправа',
            'party': [{'name': f'Игрок {i+1}', 'hero_id': self.heroes[i]['id']} for i in range(count)]})
        self.assertEqual(status, 200, result)
        from urllib.parse import parse_qs
        self.room = result['room_id']; self.root = '/api/rooms/' + self.room
        self.players = result['players']; self.tokens = [parse_qs(p['fragment'])['access'][0] for p in self.players]
        self.server.ai.config['session_narration'] = False
        return result

    def hero(self, i=0):
        return next(h for h in self.server.roster.read(self.room)['characters'] if h['id'] == self.players[i]['character_id'])

    def edit(self, fn):
        with self.server.service.store._connection() as c:
            state = self.server.adventure._load(c, self.room); fn(state)
            self.server.adventure.save(c, self.room, state)

    def test_new_scenario_grants_each_hero_advance_without_changing_templates(self):
        self.assertEqual([self.hero(i)['coins'] for i in range(2)], [300, 300])
        entities = self.server.adventure.read(self.room)['world']['entities']
        self.assertEqual({k for k, e in entities.items() if e['kind'] == 'npc'}, {'mara', 'bor', 'selena', 'iven', 'vera'})
        self.assertTrue(all(h['coins'] == 0 for h in self.server.games.heroes.list()['heroes']))

    def test_purchase_is_atomic_personal_and_replay_does_not_pay_twice(self):
        state = self.server.adventure.read(self.room)
        command = {'revision': state['revision'], 'character_id': self.players[0]['character_id'], 'text': 'Покупаю верёвку.'}
        self.proposal = {'intent': 'act', 'reply': '', 'steps': [{'rule': 'buy', 'target': 'travel_rope'}]}
        key = str(uuid4())
        self.assertEqual(self.request(self.root + '/session', command, token=self.tokens[0], key=key)[0], 200)
        after = self.server.adventure.read(self.room)
        self.assertEqual(after['world']['entities']['travel_rope']['owner'], self.players[0]['character_id'])
        self.assertEqual(after['world']['entities']['mara']['trade']['cash_cp'], 1000)
        self.assertEqual([self.hero(i)['coins'] for i in range(2)], [200, 300])
        self.assertEqual(sum(i['name'] == 'Дорожная верёвка' for i in self.hero()['items']), 1)
        self.assertEqual(self.request(self.root + '/session', command, token=self.tokens[0], key=key)[0], 200)
        self.assertEqual(self.server.adventure.read(self.room), after)
        self.assertEqual(self.hero()['coins'], 200)
        self.assertEqual(self.rolls, [])

    def test_sale_and_repurchase_move_same_inventory_without_copies(self):
        self.say(0, {'rule': 'buy', 'target': 'travel_rope'})
        self.say(1, {'rule': 'sell', 'target': 'travel_rope', 'recipient': 'mara'})
        self.assertEqual(self.hero(1)['coins'], 300)
        self.say(0, {'rule': 'sell', 'target': 'travel_rope', 'recipient': 'mara'})
        self.assertEqual(self.hero()['coins'], 250)
        self.assertFalse(any(i['name'] == 'Дорожная верёвка' for i in self.hero()['items']))
        self.say(1, {'rule': 'buy', 'target': 'travel_rope'})
        self.assertEqual(self.hero(1)['coins'], 200)
        self.assertEqual(sum(i['name'] == 'Дорожная верёвка' for i in self.hero(1)['items']), 1)
        self.assertEqual(self.server.adventure.read(self.room)['world']['entities']['mara']['trade']['cash_cp'], 1050)

    def test_bargain_quote_is_personal_without_purchase_or_attitude_change(self):
        self.say(0, {'rule': 'bargain', 'target': 'travel_rope'})
        pending = self.server.adventure.read(self.room)['pending_check']
        self.assertEqual(self.roll(1, pending)[0], 403)
        self.assertEqual(self.roll(0, pending)[0], 201)
        state = self.server.adventure.read(self.room)
        self.assertEqual(state['world']['entities']['travel_rope']['owner'], 'mara')
        self.assertEqual([self.hero(i)['coins'] for i in range(2)], [300, 300])
        for i, expected in enumerate([80, 100]):
            with self.server.service.store._connection() as c:
                actor, _ = self.server.adventure.actor(c, self.room, self.players[i]['character_id'])
            row = next(r for r in trade_view(state['world'], actor)['merchants']['mara']['offers'] if r['item_id'] == 'travel_rope')
            self.assertEqual(row['price_cp'], expected)
            self.assertEqual(attitude(state['world']['entities']['mara'], actor['id']), 'indifferent')
        self.say(0, {'rule': 'buy', 'target': 'travel_rope'})
        self.assertEqual(self.hero()['coins'], 220)

    def test_quotes_expire_and_cannot_be_rerolled_same_day(self):
        self.say(0, {'rule': 'bargain', 'target': 'travel_rope'})
        self.roll(0, self.server.adventure.read(self.room)['pending_check'])
        self.edit(lambda s: s['world'].update(time=s['world']['time'] + 601))
        self.say(0, {'rule': 'bargain', 'target': 'travel_rope'})
        self.assertEqual(self.server.adventure.read(self.room)['plan']['status'], 'blocked')
        self.say(0, {'rule': 'buy', 'target': 'travel_rope'})
        self.assertEqual(self.hero()['coins'], 200)

    def test_checkpoint_restores_money_stock_and_inventory_without_another_advance(self):
        self.say(0, {'rule': 'buy', 'target': 'travel_rope'})
        saved = self.server.adventure.read(self.room)
        coins = [self.hero(i)['coins'] for i in range(2)]
        self.server.games.execute({'action': 'save', 'room_id': self.room}, str(uuid4()))
        self.say(0, {'rule': 'sell', 'target': 'travel_rope', 'recipient': 'mara'})
        self.server.games.execute({'action': 'load', 'room_id': self.room}, str(uuid4()))
        state = self.server.adventure.read(self.room)
        self.assertEqual(state['world'], saved['world'])
        self.assertEqual([self.hero(i)['coins'] for i in range(2)], coins)
        self.assertEqual(sum(i['name'] == 'Дорожная верёвка' for i in self.hero()['items']), 1)

    def test_bargain_four_degrees_never_generate_money_or_change_trust(self):
        for degree, price in enumerate([100, 100, 90, 80]):
            with self.subTest(degree=degree):
                world = deepcopy(self.server.adventure.read(self.room)['world'])
                with self.server.service.store._connection() as c:
                    actor, roster = self.server.adventure.actor(c, self.room, self.players[0]['character_id'])
                action = prepare(world, actor, {'rule': 'bargain', 'target': 'travel_rope'}, roster['rules'])
                result = apply(world, action, degree)
                self.assertEqual(result['quote']['price_cp'], price)
                self.assertEqual(result['effects'], [])
                self.assertEqual(world['entities']['mara']['trade']['cash_cp'], 900)
                self.assertEqual(attitude(world['entities']['mara'], actor['id']), 'indifferent')

    def test_short_funds_hostile_vendor_and_unknown_prices_do_not_transfer(self):
        roster = self.server.roster.read(self.room)
        self.request(self.root + '/roster', {'action': 'update_character', 'revision': roster['revision'],
            'id': self.players[0]['character_id'], 'data': {'coins': 0}})
        self.say(0, {'rule': 'buy', 'target': 'travel_rope'})
        self.assertEqual(self.server.adventure.read(self.room)['world']['entities']['travel_rope']['owner'], 'mara')
        self.edit(lambda s: s['world']['entities']['mara'].update(attitudes={self.players[1]['character_id']: 'hostile'}))
        self.say(1, {'rule': 'buy', 'target': 'copper_cup'})
        self.assertEqual(self.server.adventure.read(self.room)['plan']['status'], 'blocked')
        item = self.hero(1)['items'][0]
        self.say(1, {'rule': 'sell', 'target': 'inventory:' + item['id'], 'recipient': 'vera'})
        self.assertEqual(self.server.adventure.read(self.room)['plan']['status'], 'blocked')
        self.assertEqual(self.hero(1)['coins'], 300)

    def test_vendor_cannot_buy_without_cash_and_failed_persistence_rolls_back(self):
        def fail_after_effects(*args):
            persist_effects(*args)
            raise ValueError('Failure after payment and transfer')
        before = self.server.adventure.read(self.room)['world']
        with patch('iirpg.adventure.persist_effects', side_effect=fail_after_effects):
            self.assertEqual(self.say(0, {'rule': 'buy', 'target': 'travel_rope'})[0], 400)
        self.assertEqual(self.server.adventure.read(self.room)['world'], before)
        self.assertEqual(self.hero()['coins'], 300)
        self.say(0, {'rule': 'buy', 'target': 'travel_rope'})
        self.edit(lambda s: s['world']['entities']['mara']['trade'].update(cash_cp=0))
        self.say(0, {'rule': 'sell', 'target': 'travel_rope', 'recipient': 'mara'})
        self.assertEqual(self.server.adventure.read(self.room)['world']['entities']['travel_rope']['owner'], self.players[0]['character_id'])
        self.assertEqual(self.hero()['coins'], 200)

    def test_speaker_selection_switches_between_local_npcs_and_rejects_remote_one(self):
        self.say(0, {'rule': 'bargain', 'target': 'travel_rope'})
        pending = self.server.adventure.read(self.room)['pending_check']
        self.request(self.root + '/session/cancel', {'offer_id': pending['offer_id']}, token=self.tokens[0])
        for target, expected in [('vera', 200), ('selena', 400)]:
            state = self.server.adventure.read(self.room)
            self.proposal = {'intent': 'question', 'reply': 'Ответ.', 'steps': [], 'voice': 'npc', 'interlocutor': target}
            status, _ = self.request(self.root + '/session', {'revision': state['revision'], 'character_id': self.players[0]['character_id'], 'text': 'У меня вопрос.'}, token=self.tokens[0])
            self.assertEqual(status, expected)
            self.assertEqual(self.server.adventure.read(self.room)['conversation_with'], 'vera')

    def test_character_specific_attitudes_and_roles_are_supplied_to_gm(self):
        self.say(0, {'rule': 'make_impression', 'target': 'mara'})
        self.roll(0, self.server.adventure.read(self.room)['pending_check'])
        state = self.server.adventure.read(self.room)
        for i, expected in enumerate(['helpful', 'indifferent']):
            with self.server.service.store._connection() as c:
                actor, _ = self.server.adventure.actor(c, self.room, self.players[i]['character_id'])
            data = context(state, actor, 'Как относишься ко мне?')
            self.assertEqual(data['gm_knowledge']['mara']['attitude_to_hero'], expected)
            self.assertIn('personality', data['gm_knowledge']['mara'])
            self.assertEqual(data['trade']['hero_coins_cp'], 300)

    def test_move_then_question_uses_specialist_without_social_check_or_consent(self):
        self.server.ai.config['session_narration'] = True
        def complete(payload, timeout):
            data = json.loads(payload['messages'][1]['content'])
            self.calls.append(data)
            if data['phase'] == 'DIALOGUE':
                self.assertEqual(data['conversation_partner']['id'], 'bor')
                self.assertEqual(data['gm_knowledge']['bor']['attitude_to_hero'], 'unfriendly')
                self.assertIn('Кузнец', data['gm_knowledge']['bor']['role'])
                return {'content': json.dumps({'reply': 'Рабочую кромку смотри, а не блеск.'})}
            return {'content': json.dumps(self.proposal)}
        self.server.ai.client.complete = complete
        status, _ = self.say(1, {'rule': 'move', 'target': 'bor'}, {'rule': 'talk', 'target': 'bor'}, text='Иду к Бору и спрашиваю про инструмент.')
        self.assertEqual(status, 200)
        state = self.server.adventure.read(self.room)
        self.assertEqual(state['history'][-1]['gm'], 'Рабочую кромку смотри, а не блеск.')
        self.assertNotIn('pending_check', state)
        self.assertFalse(state['world']['entities']['bor'].get('agreements'))
        self.assertEqual(self.rolls, [])

    def test_merchant_stock_cannot_be_taken_without_paying(self):
        self.say(0, {'rule': 'take', 'target': 'travel_rope'})
        state = self.server.adventure.read(self.room)
        self.assertEqual(state['plan']['status'], 'blocked')
        self.assertEqual(state['world']['entities']['travel_rope']['owner'], 'mara')
        self.assertEqual(self.hero()['coins'], 300)

    def test_bargain_reaction_sees_real_stock_and_current_personal_price(self):
        self.say(0, {'rule': 'bargain', 'target': 'travel_rope'})
        pending = self.server.adventure.read(self.room)['pending_check']
        self.roll(0, pending)
        def complete(payload, timeout):
            data = json.loads(payload['messages'][1]['content'])
            self.assertEqual(data['phase'], 'AFTER_ROLL')
            offers = data['trade']['merchants']['mara']['offers']
            self.assertEqual({o['item_id'] for o in offers}, {'travel_rope', 'oilskin', 'copper_cup'})
            self.assertEqual(next(o for o in offers if o['item_id'] == 'travel_rope')['price_cp'], 80)
            return {'content': json.dumps({'intent': 'question', 'reply': 'Восемьдесят медных.', 'steps': [], 'voice': 'npc'})}
        self.server.ai.client.complete = complete
        self.server.adventure.react(self.room, pending['offer_id'])
