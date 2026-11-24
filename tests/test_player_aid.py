"""Two actual player identities prepare and roll Aid; model boundary is stubbed.

Rules: Player Core Aid, https://2e.aonprd.com/Actions.aspx?ID=2292.
"""
from copy import deepcopy
import unittest

import test_multiplayer as multiplayer_fixture


class PlayerAidTests(unittest.TestCase):
    setUp = multiplayer_fixture.MultiplayerTests.setUp
    request = multiplayer_fixture.MultiplayerTests.request
    create = multiplayer_fixture.MultiplayerTests.create
    say = multiplayer_fixture.MultiplayerTests.say
    roll = multiplayer_fixture.MultiplayerTests.roll

    def prepare_help(self, skill='athletics'):
        self.server.ai.config['session_narration'] = False
        roster = self.server.roster.read(self.room)
        first = next(h for h in roster['characters'] if h['id'] == self.players[0]['character_id'])
        steps = [{'rule': 'stow', 'target': 'inventory:' + i['id']} for i in first['items'] if i.get('hands')]
        status, _ = self.say(0, *steps, {'rule': 'move', 'target': 'store'})
        self.assertEqual(status, 200)
        before = list(self.rolls)
        status, state = self.say(1, {'rule': 'aid', 'target': first['id'], 'task': 'chest', 'skill': skill},
                                 text='Придерживаю сундук, пока товарищ открывает.')
        self.assertEqual(status, 200)
        self.assertNotIn('pending_check', state)
        self.assertEqual(self.rolls, before)
        self.assertEqual(self.server.adventure.read(self.room)['world']['aid'][first['id']]['helper_id'], self.players[1]['character_id'])

    def attempt(self):
        before = list(self.rolls)
        status, _ = self.say(0, {'rule': 'force_open', 'target': 'chest'}, {'rule': 'take', 'target': 'book'}, text='Теперь открываю и забираю книгу.')
        self.assertEqual(status, 200)
        self.assertEqual(self.rolls, before)
        return self.server.adventure.read(self.room)

    def test_helper_roll_precedes_beneficiary_roll_and_loot_stays_with_beneficiary(self):
        self.prepare_help()
        state = self.attempt()
        helper = deepcopy(state['pending_check'])
        self.assertEqual(helper['kind'], 'player_aid')
        self.assertEqual(helper['character_id'], self.players[1]['character_id'])
        self.assertEqual(state['plan']['index'], 0)
        self.assertEqual(self.roll(0, helper)[0], 403)
        self.assertEqual(self.roll(1, helper)[0], 201)
        state = self.server.adventure.read(self.room)
        main = deepcopy(state['pending_check'])
        self.assertEqual(main['character_id'], self.players[0]['character_id'])
        self.assertEqual(main['offer_id'], helper['assisted_check']['offer_id'])
        self.assertEqual(main['modifier'], helper['assisted_check']['modifier'] + 2)
        self.assertEqual(main['aid']['helper_id'], helper['character_id'])
        self.assertEqual(sum(p['value'] for p in main['modifier_breakdown']), main['modifier'])
        self.assertEqual(state['plan']['index'], 0)
        self.assertFalse(state['world']['entities']['chest']['open'])
        self.assertEqual(self.roll(1, main)[0], 403)
        self.assertEqual(self.roll(0, main)[0], 201)
        after = self.server.adventure.read(self.room)
        self.assertTrue(after['world']['entities']['chest']['open'])
        self.assertEqual(after['world']['entities']['book']['owner'], self.players[0]['character_id'])
        self.assertEqual(after['plan']['status'], 'completed')
        roster = self.server.roster.read(self.room)['characters']
        self.assertEqual([sum(i['name'] == 'Дорожная книга' for i in h['items']) for h in roster], [1, 0])
        rolled = list(self.rolls)
        self.assertEqual(self.roll(1, helper)[0], 201)
        self.assertEqual(self.roll(0, main)[0], 201)
        self.assertEqual(self.rolls, rolled)
        self.assertEqual(self.server.adventure.read(self.room), after)

    def test_four_degrees_change_only_one_frozen_check(self):
        for index, (natural, bonus) in enumerate([(1, -1), (7, 0), (12, 1), (20, 2)]):
            with self.subTest(natural=natural):
                if index:self.create(2)
                self.prepare_help()
                state = self.attempt()
                helper = state['pending_check']
                self.server.service.dice.roll_die = lambda sides: min(sides, natural)
                self.assertEqual(self.roll(1, helper)[0], 201)
                state = self.server.adventure.read(self.room)
                self.assertEqual(state['pending_check']['aid']['bonus'], bonus)
                self.assertEqual(state['pending_check']['modifier'], helper['assisted_check']['modifier'] + bonus)
                self.assertNotIn(self.players[0]['character_id'], state['world']['aid'])
                self.assertFalse(state['world']['entities']['chest']['open'])

    def test_helper_can_decline_without_cancelling_ally_attempt(self):
        self.prepare_help()
        helper = self.attempt()['pending_check']
        status, _ = self.request(self.root + '/session/cancel', {'offer_id': helper['offer_id']}, token=self.tokens[1])
        self.assertEqual(status, 200)
        state = self.server.adventure.read(self.room)
        self.assertEqual(state['pending_check']['character_id'], self.players[0]['character_id'])
        self.assertEqual(state['pending_check']['modifier'], helper['assisted_check']['modifier'])
        self.assertNotIn('aid', state['pending_check'])
        self.assertEqual(state['plan']['status'], 'awaiting_roll')
        self.assertEqual(self.rolls, [])
        self.assertEqual(self.roll(1, helper)[0], 409)
        self.assertEqual(self.roll(0, state['pending_check'])[0], 201)
        self.assertEqual(self.server.adventure.read(self.room)['world']['entities']['book']['owner'], self.players[0]['character_id'])

    def test_helper_can_cancel_via_text_and_other_player_cannot(self):
        self.prepare_help()
        helper = self.attempt()['pending_check']
        status, _ = self.say(0, text='Отменяю чужую проверку помощи.', intent='cancel')
        self.assertEqual(status, 200)
        self.assertEqual(self.server.adventure.read(self.room)['pending_check'], helper)
        status, _ = self.say(1, text='Я передумала помогать.', intent='cancel')
        self.assertEqual(status, 200)
        state = self.server.adventure.read(self.room)
        self.assertEqual(state['pending_check']['character_id'], self.players[0]['character_id'])
        self.assertEqual(state['pending_check']['modifier'], helper['assisted_check']['modifier'])
        self.assertEqual(self.rolls, [])

    def test_different_task_and_expired_preparation_do_not_roll_helper(self):
        self.prepare_help()
        with self.server.service.store._connection() as c:
            state = self.server.adventure._load(c, self.room)
            state['world']['entities']['other_chest'] = {'kind': 'container', 'name': 'Другой ящик', 'place': 'store',
                                                       'open': False, 'locked': True, 'force_dc': 15}
            self.server.adventure.save(c, self.room, state)
        self.say(0, {'rule': 'force_open', 'target': 'other_chest'})
        state = self.server.adventure.read(self.room)
        self.assertEqual(state['pending_check']['character_id'], self.players[0]['character_id'])
        self.assertIn(self.players[0]['character_id'], state['world']['aid'])
        self.request(self.root + '/session/cancel', {'offer_id': state['pending_check']['offer_id']}, token=self.tokens[0])
        self.say(1, {'rule': 'aid', 'target': self.players[0]['character_id'], 'task': 'chest', 'skill': 'athletics'})
        with self.server.service.store._connection() as c:
            state = self.server.adventure._load(c, self.room)
            state['world']['time'] += 61
            self.server.adventure.save(c, self.room, state)
        state = self.attempt()
        self.assertEqual(state['pending_check']['character_id'], self.players[0]['character_id'])
        self.assertEqual(self.rolls, [])

    def test_helper_can_use_a_different_skill_without_changing_ally_stat(self):
        self.prepare_help('performance')
        helper = self.attempt()['pending_check']
        self.assertEqual(helper['skill'], 'performance')
        self.assertEqual(helper['assisted_check']['skill'], 'athletics')
        self.assertNotEqual(helper['label'], helper['assisted_check']['label'])
        self.roll(1, helper)
        main = self.server.adventure.read(self.room)['pending_check']
        self.assertEqual(main['skill'], 'athletics')
        self.assertEqual(main['aid']['skill'], 'performance')
        self.assertEqual(main['modifier'], helper['assisted_check']['modifier'] + main['aid']['bonus'])

    def test_armor_class_cannot_be_used_as_helper_roll(self):
        self.server.ai.config['session_narration'] = False
        self.say(0, {'rule': 'move', 'target': 'store'})
        self.say(1, {'rule': 'aid', 'target': self.players[0]['character_id'], 'task': 'chest', 'skill': 'armor_class'})
        self.assertEqual(self.server.adventure.read(self.room)['plan']['status'], 'blocked')
        self.assertFalse(self.server.adventure.read(self.room)['world'].get('aid'))

    def test_downed_helper_is_not_asked_to_roll(self):
        self.prepare_help()
        roster = self.server.roster.read(self.room)
        status, _ = self.request(self.root + '/roster', {'action': 'update_character', 'revision': roster['revision'],
            'id': self.players[1]['character_id'], 'data': {'sheet': {'values': {'hp_current': 0}}}})
        self.assertEqual(status, 200)
        state = self.attempt()
        self.assertEqual(state['pending_check']['character_id'], self.players[0]['character_id'])
        self.assertEqual(self.rolls, [])

    def test_self_help_is_rejected_without_bonus(self):
        self.server.ai.config['session_narration'] = False
        self.say(0, {'rule': 'move', 'target': 'store'})
        status, _ = self.say(1, {'rule': 'aid', 'target': self.players[1]['character_id'], 'task': 'chest', 'skill': 'athletics'})
        self.assertEqual(status, 200)
        state = self.server.adventure.read(self.room)
        self.assertEqual(state['plan']['status'], 'blocked')
        self.assertFalse(state['world'].get('aid'))

    def test_context_distinguishes_pending_helper_from_main_actor(self):
        self.prepare_help()
        self.attempt()
        self.say(0, text='Кто сейчас бросает и что будет потом?', intent='question')
        supplied = self.calls[-1]
        self.assertEqual(supplied['hero_id'], self.players[0]['character_id'])
        self.assertEqual(supplied['pending_check']['character_id'], self.players[1]['character_id'])
        self.assertEqual(supplied['pending_check']['assisted_attempt']['character_id'], self.players[0]['character_id'])

    def test_player_help_and_handoff_do_not_generate_extra_player_speech(self):
        self.prepare_help()
        self.server.ai.config['session_narration'] = True
        before = len(self.calls)
        self.say(1, {'rule': 'aid', 'target': self.players[0]['character_id'], 'task': 'chest', 'skill': 'athletics'})
        self.assertEqual(len(self.calls), before + 1)
        helper = self.attempt()['pending_check']
        self.roll(1, helper)
        before = len(self.calls)
        self.server.adventure.react(self.room, helper['offer_id'])
        self.assertEqual(len(self.calls), before)
        main = self.server.adventure.read(self.room)['pending_check']
        self.roll(0, main)
        self.server.adventure.react(self.room, main['offer_id'])
        self.assertEqual(len(self.calls), before)
        self.say(0, {'rule': 'give', 'target': 'book', 'recipient': self.players[1]['character_id']})
        self.assertEqual(len(self.calls), before + 1)
        self.assertEqual(self.server.adventure.read(self.room)['world']['entities']['book']['owner'], self.players[1]['character_id'])
        self.server.ai.config['session_narration'] = False
        self.say(1, {'rule': 'move', 'target': 'hall'}, {'rule': 'give', 'target': 'book', 'recipient': 'hostess'})
        quest = self.server.adventure.read(self.room)['world']['quests']['return_book']
        self.assertTrue(quest['completed'])
        self.assertEqual(quest['completed_by'], self.players[1]['character_id'])
        self.say(0, text='Кто вернул книгу?', intent='question')
        self.assertEqual(self.calls[-1]['quests']['return_book']['completed_by'], self.players[1]['character_id'])

    def test_declining_main_attempt_discards_help_and_replay_cannot_restore_it(self):
        self.prepare_help()
        helper = self.attempt()['pending_check']
        self.roll(1, helper)
        main = self.server.adventure.read(self.room)['pending_check']
        self.request(self.root + '/session/cancel', {'offer_id': main['offer_id']}, token=self.tokens[0])
        state = self.attempt()
        self.assertEqual(state['pending_check']['character_id'], self.players[0]['character_id'])
        self.assertEqual(state['pending_check']['modifier'], helper['assisted_check']['modifier'])
        self.assertNotIn('aid', state['pending_check'])
        before = deepcopy(state)
        self.assertEqual(self.roll(1, helper)[0], 201)
        self.assertEqual(self.server.adventure.read(self.room), before)


if __name__ == '__main__':
    unittest.main()
