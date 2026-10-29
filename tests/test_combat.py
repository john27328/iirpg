import tempfile
from pathlib import Path
import unittest
from uuid import uuid4
from iirpg.storage import ChatStore, MessageConflict
from iirpg.roster import InventoryRules
from iirpg.roster_storage import RosterStore
from iirpg.combat import CombatStore, degree

class CombatTests(unittest.TestCase):
    def test_degrees(self):
        self.assertEqual(degree(20, 20, 40), 1)
        self.assertEqual(degree(1, 30, 20), 2)
        self.assertEqual(degree(10, 30, 20), 3)
        self.assertEqual(degree(10, 10, 20), 0)

    def test_full_fight_retry_restart_and_original_health(self):
        with tempfile.TemporaryDirectory() as d:
            store=ChatStore(Path(d)/'game.db');room=store.create_room()
            roster=RosterStore(store,InventoryRules.load())
            actor=roster.execute(room,{'action':'create_character','revision':0,'data':{'name':'Герой','attributes':{'strength':4},'sheet':{'values':{'hp_max':20,'hp_current':20}}}},str(uuid4()))['entity_id']
            combat=CombatStore(roster,lambda sides:sides)
            cmd={'action':'start','revision':0,'character_id':actor};key=str(uuid4())
            state=combat.execute(room,cmd,key)
            self.assertEqual(combat.execute(room,cmd,key),state)
            state=combat.execute(room,{'action':'end_turn','revision':state['revision']},str(uuid4()))
            key=str(uuid4());cmd={'action':'strike','revision':state['revision']}
            state=combat.execute(room,cmd,key)
            self.assertEqual(state['status'],'finished')
            self.assertEqual(state['winner'],'Герой')
            self.assertEqual(CombatStore(roster,lambda s:1).execute(room,cmd,key),state)
            self.assertEqual(roster.read(room)['characters'][0]['sheet']['values']['hp_current'],20)
            self.assertTrue(store.page(room)['messages'])
            with self.assertRaises(MessageConflict):
                combat.execute(room,cmd,str(uuid4()))

    def test_enemy_attacks_and_turn_transition_have_individual_action_events(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ChatStore(Path(directory) / 'game.db')
            room = store.create_room()
            roster = RosterStore(store, InventoryRules.load())
            actor = roster.execute(room, {'action': 'create_character', 'revision': 0, 'data': {
                'name': 'Герой', 'sheet': {'values': {'hp_max': 100, 'hp_current': 100}},
            }}, str(uuid4()))['entity_id']
            combat = CombatStore(roster, lambda sides: 10 if sides == 20 else 1)
            state = combat.execute(room, {'action': 'start', 'revision': 0, 'character_id': actor}, str(uuid4()))
            self.assertEqual(state['turn'], 1)
            key = str(uuid4())
            command = {'action': 'enemy_turn', 'revision': state['revision']}
            result = combat.execute(room, command, key)
            events = store.page(room)['messages']
            actions = [event['combat'] for event in events[1:]]
            self.assertEqual([a['action'] for a in actions], ['strike', 'strike', 'strike', 'end_turn'])
            self.assertEqual([a['before']['actions'] for a in actions], [3, 2, 1, 0])
            self.assertEqual([a['after']['actions'] for a in actions], [2, 1, 0, 3])
            self.assertEqual([a['roll']['map'] for a in actions[:3]], [0, -5, -10])
            self.assertEqual([a['step'] for a in actions], [0, 1, 2, 3])
            self.assertTrue(all(a['request_id'] == key for a in actions))
            self.assertTrue(all(a['actor'] == 'Учебный спарринг-партнёр' for a in actions))
            self.assertTrue(all(e['type'] == 'combat.action' for e in events))
            self.assertEqual(actions[-1]['before']['turn'], 1)
            self.assertEqual(actions[-1]['after']['turn'], 0)
            self.assertEqual(combat.execute(room, command, key), result)
            self.assertEqual(store.page(room)['messages'], events)
            with self.assertRaises(ValueError):
                combat.execute(room, {'action': 'nonsense', 'revision': result['revision']}, str(uuid4()))
            self.assertEqual(store.page(room)['messages'], events)
            from iirpg.application import ChatService
            from iirpg.debug import inspect_room
            debug = inspect_room(ChatService(store), roster, room, combat=combat)
            self.assertEqual(debug['queue']['messages'], events)
            self.assertEqual(debug['entities']['encounter'], result)
