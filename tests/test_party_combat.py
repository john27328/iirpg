"""Party control, attributed text actions and reviewable free-form intents."""
from copy import deepcopy
import unittest
from uuid import uuid4

import test_scene
from test_ai import FakeClient
from iirpg.ai import AIService, load_config
from iirpg.combat import CombatStore
from iirpg.combat_tactics import fighter_id, team
from iirpg.storage import MessageConflict


class PartyCombatTests(unittest.TestCase):
    command = test_scene.SceneTests.command

    def setUp(self):
        test_scene.SceneTests.setUp(self)
        self.owner1 = self.roster_command('create_player', data={'name': 'Первый игрок'})['entity_id']
        self.owner2 = self.roster_command('create_player', data={'name': 'Второй игрок'})['entity_id']
        self.roster_command('update_character', id=self.actor, data={'player_id': self.owner1,
            'sheet': {'values': {'hp_max': 100, 'hp_current': 100, 'perception_other': 20}}})
        self.second = self.roster_command('create_character', data={'name': 'Вторая героиня', 'player_id': self.owner2,
            'attributes': {'strength': 3}, 'sheet': {'values': {'hp_max': 20, 'hp_current': 20, 'perception_other': 10}}})['entity_id']
        self.command('load_map', parameters={'template_id': 'tavern'})
        self.rolls = []
        self.combat = CombatStore(self.roster, lambda sides: self.rolls.append(sides) or (10 if sides == 20 else 1))

    def roster_command(self, action, **extra):
        return self.roster.execute(self.room, {'action': action, 'revision': self.roster.read(self.room)['revision'], **extra}, str(uuid4()))

    def start(self):
        return self.combat.execute(self.room, {'action': 'start', 'revision': 0, 'character_ids': [self.actor, self.second], 'group_id': 'patrol'}, str(uuid4()))

    def act(self, state, action, **extra):
        return self.combat.execute(self.room, {'action': action, 'revision': state['revision'],
            'actor_id': fighter_id(state, state['turn']), **extra}, str(uuid4()))

    def test_party_start_owner_turns_distinct_spawns_and_atomic_retry(self):
        key = str(uuid4());command = {'action': 'start', 'revision': 0, 'character_ids': [self.actor, self.second], 'group_id': 'patrol'}
        state = self.combat.execute(self.room, command, key)
        self.assertEqual(self.combat.execute(self.room, command, key), state)
        self.assertEqual(len(state['fighters']), 4)
        self.assertEqual([f['team'] for f in state['fighters']], ['players', 'players', 'enemies', 'enemies'])
        self.assertEqual(state['initiative_order'], [0, 1, 2, 3])
        self.assertEqual(state['fighters'][0]['player_name'], 'Первый игрок')
        self.assertEqual(state['fighters'][1]['player_name'], 'Второй игрок')
        self.assertEqual(len({tuple(f['position']) for f in state['fighters']}), 4)
        state = self.act(state, 'shield')
        state = self.act(state, 'end_turn')
        self.assertEqual(state['turn'], 1)
        state = self.act(state, 'shield')
        self.assertTrue(state['fighters'][1]['shield'])
        self.assertEqual(state['journal'][-1]['actor'], 'Вторая героиня')
        self.assertEqual(state['actions'], 2)
        self.assertEqual(CombatStore(self.roster, lambda s: 1).read(self.room), state)

    def test_party_validation_actor_guard_and_friendly_fire_are_atomic(self):
        for ids in ([], [self.actor, self.actor], ['missing'], [self.actor] * 5, 'not-list'):
            with self.assertRaises(ValueError):
                self.combat.execute(self.room, {'action': 'start', 'revision': 0, 'character_ids': ids}, str(uuid4()))
        with self.assertRaises(ValueError):
            self.combat.execute(self.room, {'action': 'start', 'revision': 0, 'character_ids': [self.actor], 'character_id': self.actor}, str(uuid4()))
        state = self.start();before_rolls = list(self.rolls)
        with self.assertRaises(ValueError):
            self.act(state, 'shield', actor_id=self.second)
        with self.assertRaises(ValueError):
            self.act(state, 'strike', target_id=self.second)
        self.assertEqual(self.rolls, before_rolls)
        self.assertEqual(self.combat.read(self.room), state)

    def test_enemy_ai_targets_party_and_skips_defeated_hero(self):
        state = self.start();state = self.act(state, 'end_turn');state = self.act(state, 'end_turn')
        self.assertEqual(team(state, state['turn']), 'enemies')
        ai = AIService(self.combat, self.scene, load_config(), FakeClient())
        context = ai.preview(self.room)['context']
        self.assertEqual({c['id'] for c in context['opponents']}, {self.actor, self.second})
        self.assertEqual(len(context['allies']), 1)
        trial = deepcopy(state);trial['fighters'][0]['hp'] = 0
        trial['turn'] = 3
        self.combat.end_turn(trial)
        self.assertEqual(trial['turn'], 1)
        self.assertEqual(trial['status'], 'active')

    def test_known_text_command_spends_once_and_records_source(self):
        state = self.start();key = str(uuid4())
        command = {'action': 'intent', 'revision': state['revision'], 'actor_id': self.actor, 'text': 'Поднимаю щит'}
        result = self.combat.execute(self.room, command, key)
        self.assertTrue(result['fighters'][0]['shield']);self.assertEqual(result['actions'], 2)
        self.assertEqual(result['journal'][-1]['source_text'], 'Поднимаю щит')
        self.assertEqual(result['journal'][-1]['action'], 'shield')
        self.assertEqual(result['intents'], [])
        self.assertEqual(self.combat.execute(self.room, command, key), result)
        self.assertEqual(self.combat.read(self.room)['actions'], 2)
        with self.assertRaises(MessageConflict):
            self.combat.execute(self.room, command, str(uuid4()))
        result = self.act(result, 'intent', text='Конец хода')
        self.assertEqual(result['turn'], 1)
        with self.assertRaises(ValueError):
            self.act(result, 'intent', text='Щит', actor_id=self.actor)

    def test_unknown_intent_waits_for_owner_turn_and_requires_explicit_resolution(self):
        state = self.start()
        state = self.act(state, 'intent', text='Отвлеку стражника историей о драконе')
        self.assertEqual(state['actions'], 3)
        self.assertEqual(self.rolls, [20, 20, 20, 20])
        intent = state['intents'][0]
        self.assertEqual(intent['actor_id'], self.actor)
        self.assertEqual(intent['status'], 'pending')
        # An unexecuted intention does not become a perceived physical action for NPCs.
        self.assertFalse(any(e['action'] == 'intent' for m in state['awareness'].values() for e in m['events']))
        state = self.act(state, 'end_turn')
        with self.assertRaises(ValueError):
            self.act(state, 'resolve_intent', intent_id=intent['id'], effect='shield')
        for _ in range(3):state = self.act(state, 'end_turn')
        self.assertEqual(state['turn'], 0)
        state = self.act(state, 'resolve_intent', intent_id=intent['id'], effect='shield')
        self.assertEqual(state['actions'], 2)
        self.assertEqual(state['intents'][0]['status'], 'resolved')
        with self.assertRaises(ValueError):
            self.act(state, 'resolve_intent', intent_id=intent['id'], effect='shield')

    def test_compound_description_is_not_partially_executed_and_can_be_rejected(self):
        state = self.start()
        state = self.act(state, 'intent', text='Поднять щит и атаковать стражника')
        self.assertFalse(state['fighters'][0]['shield']);self.assertEqual(state['actions'], 3)
        state = self.act(state, 'resolve_intent', intent_id=state['intents'][0]['id'], effect='reject_intent')
        self.assertEqual(state['intents'][0]['status'], 'rejected');self.assertEqual(state['actions'], 3)
        with self.assertRaises(ValueError):self.act(state, 'reject_intent')

    def test_text_move_tip_over_and_rollback(self):
        state = self.combat.execute(self.room, {'action': 'start', 'revision': 0, 'character_id': self.actor}, str(uuid4()))
        state = self.act(state, 'intent', text='Идти 3,3')
        self.assertEqual(state['fighters'][0]['position'], [3, 3]);self.assertEqual(state['actions'], 2)
        before = deepcopy(state)
        with self.assertRaises(ValueError):self.act(state, 'intent', text='Идти 0,0')
        self.assertEqual(self.combat.read(self.room), before)
        self.combat.roll_die = lambda sides: 20
        state = self.act(state, 'intent', text='Опрокинуть Тяжёлый стол')
        table = next(e for e in self.scene.read(self.room)['entities'] if e['id'] == 'table')
        self.assertTrue(table['provides_cover']);self.assertEqual(state['actions'], 1)
        state = self.act(state, 'intent', text='Укрытие', target_id='table')
        self.assertEqual(state['fighters'][0]['cover']['bonus'], 2);self.assertEqual(state['actions'], 0)

    def test_preset_starts_map_and_group_atomically_and_replays(self):
        before_roster = self.roster.read(self.room)
        command = {'action': 'start', 'revision': 0, 'character_ids': [self.actor, self.second], 'preset_id': 'forest_patrol'}
        key = str(uuid4())
        state = self.combat.execute(self.room, command, key)
        self.assertEqual(state['preset_id'], 'forest_patrol')
        self.assertEqual(state['group_id'], 'patrol')
        self.assertEqual(self.scene.read(self.room)['map']['id'], 'forest_camp')
        self.assertEqual(len(state['fighters']), 4)
        scene = self.scene.read(self.room)
        history = self.store.page(self.room)
        self.assertEqual(self.combat.execute(self.room, command, key), state)
        self.assertEqual(self.scene.read(self.room), scene)
        self.assertEqual(self.store.page(self.room), history)
        self.assertEqual(self.roster.read(self.room), before_roster)
        with self.assertRaises(ValueError):
            self.act(state, 'start', character_ids=[self.actor], preset_id='tavern_duel')
        self.assertEqual(self.scene.read(self.room), scene)

    def test_invalid_preset_or_hero_does_not_replace_scene(self):
        before = self.scene.read(self.room)
        for extra in ({'preset_id': 'missing'}, {'preset_id': 'forest_patrol', 'group_id': 'duel'},
                      {'preset_id': 'forest_patrol', 'character_ids': ['missing']}):
            command = {'action': 'start', 'revision': 0, 'character_ids': [self.actor], **extra}
            with self.assertRaises(ValueError):
                self.combat.execute(self.room, command, str(uuid4()))
            self.assertEqual(self.scene.read(self.room), before)
            self.assertEqual(self.combat.read(self.room)['status'], 'idle')

    def test_preset_bootstraps_scene_for_new_room_without_scene_row(self):
        from iirpg.scene import SceneStore
        room = self.store.create_room()
        actor = self.roster.execute(room, {'action': 'create_character', 'revision': 0,
            'data': {'name': 'Новичок', 'sheet': {'values': {'hp_max': 20, 'hp_current': 20}}}}, str(uuid4()))['entity_id']
        state = self.combat.execute(room, {'action': 'start', 'revision': 0, 'character_id': actor, 'preset_id': 'tavern_duel'}, str(uuid4()))
        self.assertTrue(state['mapped'])
        self.assertEqual(SceneStore(self.roster, lambda s: 1).read(room)['map']['id'], 'tavern')
