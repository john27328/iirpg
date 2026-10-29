"""Group turn order, observation boundaries and audible coordination."""
from copy import deepcopy
import json
import unittest
from uuid import uuid4

import test_scene
from test_ai import FakeClient
from iirpg.ai import AIService, load_config
from iirpg.ai_context import npc_context
from iirpg.combat import CombatStore
from iirpg.combat_map import approach, move_fighter
from iirpg.combat_tactics import (can_hear, can_see, communicate, fighter_id,
                                 may_surrender, observe)


class GroupCombatTests(unittest.TestCase):
    command = test_scene.SceneTests.command

    def setUp(self):
        test_scene.SceneTests.setUp(self)
        self.roster.execute(self.room, {'action': 'update_character', 'id': self.actor,
            'revision': self.roster.read(self.room)['revision'], 'data': {
                'attributes': {'strength': 4}, 'sheet': {'values': {'hp_max': 100, 'hp_current': 100}}}}, str(uuid4()))
        self.command('load_map', parameters={'template_id': 'tavern'})
        self.combat = CombatStore(self.roster, lambda sides: 10 if sides == 20 else 1)
        self.scene_state = self.scene.read(self.room)
        self.profile = load_config()['npc']

    def start(self, group='patrol'):
        return self.combat.execute(self.room, {'action': 'start', 'group_id': group, 'character_id': self.actor,
                                              'revision': self.combat.read(self.room)['revision']}, str(uuid4()))

    def act(self, state, action, **extra):
        return self.combat.execute(self.room, {'revision': state['revision'], 'action': action, **extra}, str(uuid4()))

    def test_group_start_unique_cells_orders_retry_and_turn_cycle(self):
        key = str(uuid4())
        command = {'action': 'start', 'group_id': 'squad', 'character_id': self.actor, 'revision': 0}
        state = self.combat.execute(self.room, command, key)
        self.assertEqual(len(state['fighters']), 4)
        self.assertEqual(len({tuple(f['position']) for f in state['fighters']}), 4)
        self.assertEqual(state['initiative_order'], [1, 2, 3, 0])
        events = self.store.page(self.room)
        self.assertEqual(self.combat.execute(self.room, command, key), state)
        self.assertEqual(self.store.page(self.room), events)
        self.assertTrue(any(e.get('combat', {}).get('action') == 'communicate' for e in events['messages']))
        guard_id = fighter_id(state, 2)
        self.assertEqual(state['awareness'][guard_id]['order']['mode'], 'advance')
        for expected in (2, 3, 0, 1):
            state = self.act(state, 'end_turn')
            self.assertEqual(state['turn'], expected)
            self.assertEqual(state['actions'], 3)
            self.assertEqual(state['attacks'], 0)
        self.assertEqual(state['round'], 2)
        self.assertEqual(CombatStore(self.roster, lambda s: 1).read(self.room), state)

    def test_target_selection_defeat_does_not_finish_group_and_dead_are_skipped(self):
        state = self.start()
        with self.assertRaises(ValueError):
            self.act(state, 'strike', target_id=fighter_id(state, 2))
        with self.assertRaises(ValueError):
            self.act(state, 'move', position=state['fighters'][2]['position'])
        state = self.act(state, 'end_turn')
        state = self.act(state, 'end_turn')
        self.assertEqual(state['turn'], 0)
        position = approach(state, self.scene.read(self.room), 2)
        state = self.act(state, 'move', position=position)
        self.combat.roll_die = lambda sides: sides
        state = self.act(state, 'strike', target_id=fighter_id(state, 2))
        self.assertEqual(state['fighters'][2]['hp'], 0)
        self.assertEqual(state['status'], 'active')
        event = self.store.page(self.room)['messages'][-1]['combat']
        self.assertEqual(event['target_index'], 2)
        self.assertEqual(event['target'], state['fighters'][2]['name'])
        state = self.act(state, 'end_turn')
        self.assertEqual(state['turn'], 1)
        state = self.act(state, 'end_turn')
        self.assertEqual(state['turn'], 0)

    def test_ai_controls_current_group_member_and_context_fits_budget(self):
        state = self.start()
        state = self.act(state, 'end_turn')
        client = FakeClient()
        config = load_config()
        ai = AIService(self.combat, self.scene, config, client)
        preview = ai.preview(self.room)
        self.assertEqual(preview['context']['actor_id'], fighter_id(state, 2))
        self.assertEqual(preview['context']['self']['role'], 'guard')
        self.assertLessEqual(len(json.dumps(preview['context'], ensure_ascii=False).encode()), config['context_max_bytes'])
        response = ai.step(self.room, {'revision': state['revision']}, str(uuid4()))
        self.assertTrue(response['combat']['fighters'][2]['shield'])
        self.assertFalse(response['combat']['fighters'][1]['shield'])

    def test_configured_language_barrier_prevents_receiving_leader_order(self):
        self.combat.rules['groups']['patrol']['members'][1]['languages'] = ['unshared']
        state = self.start()
        self.assertEqual(state['fighters'][2]['languages'], ['unshared'])
        self.assertIsNone(state['awareness'][fighter_id(state, 2)]['order'])
        self.assertFalse(can_hear(state, self.scene.read(self.room), 2, 1))

    def blind_state(self):
        state = self.start()
        state['awareness'] = {}
        state['fighters'][1]['position'] = [4, 4]
        state['fighters'][2]['position'] = [7, 4]
        state['fighters'][0]['position'] = [8, 4]
        observe(state, self.scene_state)
        return state

    def test_walls_hearing_language_and_no_shared_omniscience(self):
        state = self.blind_state()
        self.assertFalse(can_see(state, self.scene_state, 1, 0))
        self.assertTrue(can_see(state, self.scene_state, 2, 0))
        self.assertNotIn(fighter_id(state, 0), state['awareness'][fighter_id(state, 1)]['contacts'])
        self.assertTrue(can_hear(state, self.scene_state, 1, 2))
        state['fighters'][2]['languages'] = ['other_language']
        self.assertFalse(can_hear(state, self.scene_state, 1, 2))
        state['fighters'][2]['languages'] = ['common']
        state['fighters'][2]['position'] = [10, 6]
        state['fighters'][1]['position'] = [1, 1]
        self.assertFalse(can_hear(state, self.scene_state, 1, 2))

    def test_reported_position_stays_old_and_hidden_changes_do_not_change_menu(self):
        state = self.blind_state()
        state['turn'] = 2
        report = communicate(state, self.scene_state)
        self.assertIn(fighter_id(state, 1), report['recipients'])
        state['turn'] = 1
        state['fighters'][0]['position'] = [10, 1]
        context, commands = npc_context(state, self.scene_state, self.profile)
        enemy = context['opponents'][0]
        self.assertEqual(enemy['position'], [8, 4])
        self.assertEqual(enemy['source'], 'heard')
        self.assertFalse(enemy['visible_now'])
        self.assertFalse(any(c['action'] == 'strike' for c in commands.values()))
        other = deepcopy(state)
        other['fighters'][0].update(position=[9, 1], hp=1, ac=99, attack=99)
        other_context, other_commands = npc_context(other, self.scene_state, self.profile)
        self.assertEqual(other_context, context)
        self.assertEqual(other_commands, commands)

    def test_order_delivery_expiry_and_morale_use_only_known_losses(self):
        state = self.blind_state()
        state['fighters'][1]['hp'] = 2
        report = communicate(state, self.scene_state)
        self.assertEqual(report['order']['mode'], 'withdraw')
        state['turn'] = 2
        context, commands = npc_context(state, self.scene_state, self.profile)
        self.assertEqual(context['current_intent'], 'withdraw_together')
        self.assertIn('surrender', commands)
        state['turns_elapsed'] = state['rules']['coordination']['order_ttl_turns'] + 1
        context, commands = npc_context(state, self.scene_state, self.profile)
        self.assertTrue(context['received_order']['stale'])
        self.assertNotIn('surrender', commands)
        state['turn'] = 1
        state['fighters'][1]['hp'] = 10
        state['awareness'][fighter_id(state, 1)]['order'] = None
        state['fighters'][2].update(position=[10, 6], hp=0)
        observe(state, self.scene_state)
        self.assertFalse(may_surrender(state, 1))
        state['fighters'][2]['position'] = [3, 4]
        observe(state, self.scene_state)
        self.assertTrue(may_surrender(state, 1))

    def test_checked_empty_last_position_is_not_treated_as_occupied(self):
        state = self.start()
        old = deepcopy(state['awareness'][fighter_id(state, 1)]['contacts'][fighter_id(state, 0)])
        state['fighters'][1]['position'] = [4, 4]
        old['position'] = [3, 4]
        state['awareness'][fighter_id(state, 1)]['contacts'][old['id']] = old
        state['fighters'][0]['position'] = [8, 4]
        observe(state, self.scene_state)
        self.assertTrue(state['awareness'][fighter_id(state, 1)]['contacts'][old['id']]['last_position_checked_empty'])
        state['fighters'][1]['hp'] = 1
        context, commands = npc_context(state, self.scene_state, self.profile)
        self.assertEqual(context['current_intent'], 'protect_self')
        self.assertIn('surrender', commands)

    def test_hidden_object_changes_do_not_leak_through_action_menu(self):
        state = self.blind_state()
        before, commands = npc_context(state, self.scene_state, self.profile)
        changed = deepcopy(self.scene_state)
        counter = next(e for e in changed['entities'] if e['id'] == 'counter')
        counter.update(provides_cover=False, blocks_movement=False)
        after, other_commands = npc_context(state, changed, self.profile)
        self.assertEqual(after, before)
        self.assertEqual(other_commands, commands)

    def test_group_battle_can_finish_with_surrender_without_changing_roster(self):
        state = self.start()
        original = self.roster.read(self.room)
        self.combat.roll_die = lambda sides: sides
        for _ in range(60):
            if state['status'] == 'finished':
                break
            if state['turn'] and may_surrender(state, state['turn']):
                state = self.act(state, 'surrender')
            elif state['turn']:
                state = self.act(state, 'end_turn')
            elif not state['actions']:
                state = self.act(state, 'end_turn')
            else:
                victim = next(i for i,f in enumerate(state['fighters']) if i and f['hp']>0 and not f.get('surrendered'))
                position = approach(state, self.scene.read(self.room), victim)
                if position:
                    state = self.act(state, 'move', position=position)
                else:
                    state = self.act(state, 'strike', target_id=fighter_id(state, victim))
                    if state['status'] == 'active':
                        state = self.act(state, 'end_turn')
        self.assertEqual(state['status'], 'finished')
        self.assertEqual(state['winner_team'], 'players')
        self.assertTrue(any(f.get('surrendered') for f in state['fighters']))
        self.assertEqual(self.roster.read(self.room), original)
        self.assertTrue(any(e.get('combat', {}).get('action') == 'surrender' for e in self.store.page(self.room)['messages']))
