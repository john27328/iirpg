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
