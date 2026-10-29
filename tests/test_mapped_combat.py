import unittest
from copy import deepcopy
from uuid import uuid4
import test_scene
from iirpg.combat import CombatStore
from iirpg.storage import MessageConflict

class MappedCombatTests(unittest.TestCase):
    def setUp(self):
        test_scene.SceneTests.setUp(self)
        self.roster.execute(self.room,{'action':'update_character','id':self.actor,'revision':self.roster.read(self.room)['revision'],'data':{'sheet':{'values':{'hp_max':100,'hp_current':100}}}},str(uuid4()))
    command=test_scene.SceneTests.command

    def start(self):
        self.command('load_map',parameters={'template_id':'tavern'})
        self.command('join')
        self.combat=CombatStore(self.roster,lambda sides:10 if sides==20 else 1)
        return self.combat.execute(self.room,{'action':'start','revision':0,'character_id':self.actor},str(uuid4()))

    def act(self,state,action,**extra):
        return self.combat.execute(self.room,{'action':action,'revision':state['revision'],**extra},str(uuid4()))

    def test_map_sync_reach_action_cost_enemy_movement_and_restart(self):
        state=self.start();self.assertTrue(state['mapped'])
        self.assertGreater(sum(abs(a-b) for a,b in zip(state['fighters'][0]['position'],state['fighters'][1]['position'])),1)
        self.assertEqual(state['turn'],1)
        before=deepcopy(state)
        with self.assertRaises(ValueError):self.act(state,'strike')
        self.assertEqual(self.combat.read(self.room),before)
        state=self.act(state,'enemy_turn')
        events=self.store.page(self.room)['messages']
        self.assertTrue(any(e.get('combat',{}).get('action')=='move' for e in events))
        for fighter in state['fighters']:
            entity=next(e for e in self.scene.read(self.room)['entities'] if e['id']==fighter['entity_id'])
            self.assertEqual(entity['position'],fighter['position'])
            self.assertEqual(entity['training_hp'],fighter['hp'])
        self.assertEqual(CombatStore(self.roster,lambda sides:1).read(self.room),state)
        with self.assertRaises(ValueError):self.command('move_cell',parameters={'position':[2,5]})
        with self.assertRaises(ValueError):self.command('load_map',parameters={'template_id':'forest_camp'})

    def test_move_rollback_and_retries_do_not_spend_twice(self):
        state=self.start();state=self.act(state,'end_turn')
        self.assertEqual(state['turn'],0)
        for target in ([0,0],state['fighters'][1]['position'],[10,6]):
            before_scene=self.scene.read(self.room)
            with self.assertRaises(ValueError):self.act(state,'move',position=target)
            self.assertEqual(self.combat.read(self.room),state)
            self.assertEqual(self.scene.read(self.room),before_scene)
        key=str(uuid4());command={'action':'move','revision':state['revision'],'position':[2,5]}
        result=self.combat.execute(self.room,command,key)
        self.assertEqual(result['actions'],2)
        count=len(self.store.page(self.room)['messages'])
        self.assertEqual(self.combat.execute(self.room,command,key),result)
        self.assertEqual(len(self.store.page(self.room)['messages']),count)
        with self.assertRaises(MessageConflict):self.combat.execute(self.room,command,str(uuid4()))

    def test_cover_and_shield_use_highest_bonus_and_movement_ends_cover(self):
        self.command('load_map',parameters={'template_id':'tavern'})
        self.command('join')
        self.command('move_cell',parameters={'position':[3,3]})
        self.command('tip_over',target_id='table')
        self.combat=CombatStore(self.roster,lambda sides:10 if sides==20 else 1)
        state=self.combat.execute(self.room,{'action':'start','revision':0,'character_id':self.actor},str(uuid4()))
        state=self.act(state,'end_turn')
        state=self.act(state,'take_cover',target_id='table')
        self.assertEqual(state['actions'],2)
        state=self.act(state,'shield')
        state=self.act(state,'end_turn')
        state=self.act(state,'enemy_turn')
        attacks=[e['combat'] for e in self.store.page(self.room)['messages'] if e.get('combat',{}).get('action')=='strike']
        self.assertTrue(attacks)
        self.assertTrue(all(a['roll']['dc']==state['fighters'][0]['ac']+2 for a in attacks))
        if state['status']=='active':
            state=self.act(state,'move',position=[3,4])
            self.assertIsNone(state['fighters'][0]['cover'])
