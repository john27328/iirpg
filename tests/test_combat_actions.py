"""Context menus are read-only and agree with execution, not optimistic UI guesses."""
import unittest
from uuid import uuid4

import test_party_combat
from iirpg.combat_tactics import fighter_id
from iirpg.storage import MessageConflict


class CombatActionsTests(unittest.TestCase):
    setUp = test_party_combat.PartyCombatTests.setUp
    command = test_party_combat.PartyCombatTests.command
    roster_command = test_party_combat.PartyCombatTests.roster_command
    start = test_party_combat.PartyCombatTests.start
    act = test_party_combat.PartyCombatTests.act

    def menu(self, state, position=None):
        return self.combat.actions(self.room, state['revision'], position)

    def names(self, menu, section):
        return [item['command']['action'] for item in menu[section]]

    def test_general_actions_do_not_autoselect_a_target_and_queries_are_read_only(self):
        state = self.start();scene = self.scene.read(self.room);history = self.store.page(self.room);rolls = list(self.rolls)
        menu = self.menu(state)
        self.assertEqual(self.names(menu, 'general'), ['shield', 'end_turn'])
        self.assertEqual(menu['selected'], [])
        self.assertEqual(menu['actor_id'], self.actor)
        for position in ([0, 0], [10, 6], state['fighters'][0]['position'], state['fighters'][1]['position']):
            self.assertEqual(self.menu(state, position)['selected'], [])
        self.assertEqual(self.names(self.menu(state, state['fighters'][2]['position']), 'selected'), ['demoralize'])
        self.assertEqual(self.combat.read(self.room), state)
        self.assertEqual(self.scene.read(self.room), scene)
        self.assertEqual(self.store.page(self.room), history)
        self.assertEqual(self.rolls, rolls)

    def test_reachable_move_then_shield_disappear_after_execution(self):
        state = self.start();position = [2, 5]
        menu = self.menu(state, position)
        self.assertEqual(self.names(menu, 'selected'), ['move'])
        self.assertEqual(menu['selected'][0]['distance_feet'], 5)
        command = menu['selected'][0]['command']
        state = self.act(state, command.pop('action'), **command)
        self.assertEqual(self.menu(state, position)['selected'], [])
        state = self.act(state, 'shield')
        self.assertEqual(self.names(self.menu(state), 'general'), ['end_turn'])

    def test_selected_enemy_has_only_legal_strike_and_no_friendly_attack(self):
        state = self.start();state = self.act(state, 'move', position=[3, 3]);state = self.act(state, 'move', position=[2, 2])
        target = state['fighters'][2]
        menu = self.menu(state, target['position'])
        self.assertEqual(self.names(menu, 'selected'), ['demoralize', 'strike'])
        self.assertEqual(menu['selected'][0]['command']['target_id'], target['entity_id'])
        self.assertEqual(self.menu(state, state['fighters'][1]['position'])['selected'], [])
        state = self.act(state, 'strike', target_id=target['entity_id'])
        self.assertEqual(state['actions'], 0)
        self.assertEqual(self.names(self.menu(state, target['position']), 'general'), ['end_turn'])
        self.assertEqual(self.menu(state, target['position'])['selected'], [])

    def test_object_menu_changes_from_tip_over_to_cover_without_rolling_on_read(self):
        state = self.start();state = self.act(state, 'move', position=[3, 3])
        rolls = list(self.rolls);menu = self.menu(state, [4, 3])
        self.assertEqual(self.names(menu, 'selected'), ['tip_over'])
        self.assertEqual(self.rolls, rolls)
        state = self.act(state, 'tip_over', target_id='table')
        self.assertEqual(self.names(self.menu(state, [4, 3]), 'selected'), ['take_cover'])
        state = self.act(state, 'take_cover', target_id='table')
        self.assertEqual(self.menu(state, [4, 3])['selected'], [])
        for _ in range(4):state = self.act(state, 'end_turn')
        self.assertEqual(state['turn'], 0)
        self.assertEqual(self.menu(state, [4, 3])['selected'], [])

    def test_stale_and_invalid_queries_and_npc_turn(self):
        state = self.start()
        for position in ([-1, 0], [40, 0], [0, 40], [1], [True, 1]):
            with self.assertRaises(ValueError):self.menu(state, position)
        with self.assertRaises(MessageConflict):self.combat.actions(self.room, 0)
        with self.assertRaises(KeyError):self.combat.actions(str(uuid4()), 0)
        state = self.act(state, 'end_turn');state = self.act(state, 'end_turn')
        menu = self.menu(state, [2, 5])
        self.assertEqual(menu['general'], []);self.assertEqual(menu['selected'], [])

    def test_unmapped_encounter_needs_explicit_target(self):
        room = self.store.create_room()
        actor = self.roster.execute(room, {'action': 'create_character', 'revision': 0, 'data': {
            'name': 'Без карты', 'sheet': {'values': {'hp_max': 20, 'hp_current': 20, 'perception_other': 100}}}}, str(uuid4()))['entity_id']
        state = self.combat.execute(room, {'action': 'start', 'revision': 0, 'character_id': actor}, str(uuid4()))
        self.assertEqual(self.combat.actions(room, state['revision'])['selected'], [])
        menu = self.combat.actions(room, state['revision'], target_id=fighter_id(state, 1))
        self.assertEqual(self.names(menu, 'selected'), ['demoralize', 'strike'])
