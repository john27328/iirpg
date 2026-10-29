import tempfile
from pathlib import Path
from uuid import uuid4
import unittest
from iirpg.scene import SceneStore
from iirpg.roster_storage import RosterStore
from iirpg.roster import InventoryRules
from iirpg.storage import ChatStore, MessageConflict

class SceneTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.store=ChatStore(Path(self.temp.name)/'game.db');self.room=self.store.create_room()
        self.roster=RosterStore(self.store,InventoryRules.load())
        self.actor=self.roster.execute(self.room,{'action':'create_character','revision':0,'data':{'name':'Герой','attributes':{'strength':4},'sheet':{'values':{'athletics_rank':'trained'}}}},str(uuid4()))['entity_id']
        self.rolls=[]
        self.scene=SceneStore(self.roster,lambda sides:self.rolls.append(sides) or 20)
    def command(self,action,**extra):
        return self.scene.execute(self.room,{'action':action,'revision':self.scene.read(self.room)['revision'],'actor_id':self.actor,**extra},str(uuid4()))
    def test_intent_adjudication_cover_movement_retry_and_restart(self):
        self.command('join');self.command('move',target_id='hall')
        state=self.command('propose',text='Опрокину стол и спрячусь')
        self.assertFalse(state['entities'][0]['provides_cover']);self.assertEqual(self.rolls,[])
        proposal=state['proposals'][0]['id'];key=str(uuid4())
        command={'action':'resolve','revision':state['revision'],'proposal_id':proposal,'target_id':'table','parameters':{'effect':'tip_over','dc':15}}
        state=self.scene.execute(self.room,command,key)
        self.assertTrue(state['entities'][0]['provides_cover']);self.assertEqual(state['proposals'][0]['status'],'resolved')
        self.assertEqual(self.scene.execute(self.room,command,key),state);self.assertEqual(self.rolls,[20])
        state=self.command('take_cover',target_id='table')
        self.assertEqual(state['entities'][-1]['cover']['bonus'],2)
        self.assertEqual(SceneStore(self.roster,lambda sides:1).read(self.room),state)
        state=self.command('move',target_id='entrance');self.assertIsNone(state['entities'][-1]['cover'])
        events=self.store.page(self.room)['messages'];self.assertTrue(all(e['type']=='scene.action' for e in events))
        self.assertIn('before',events[-1]['scene']);self.assertIn('after',events[-1]['scene'])
    def test_invalid_actions_are_atomic_and_room_scoped(self):
        self.command('join');before=self.scene.read(self.room);events=self.store.page(self.room)
        for action,extra in [('move',{'target_id':'counter'}),('take_cover',{'target_id':'table'}),('resolve',{'proposal_id':'missing'}),('nonsense',{})]:
            with self.assertRaises(ValueError):self.command(action,**extra)
            self.assertEqual(self.scene.read(self.room),before);self.assertEqual(self.store.page(self.room),events)
        with self.assertRaises(MessageConflict):self.scene.execute(self.room,{'action':'move','revision':0,'actor_id':self.actor,'target_id':'hall'},str(uuid4()))
        other=self.store.create_room()
        with self.assertRaises(ValueError):self.scene.execute(other,{'action':'join','revision':0,'actor_id':self.actor},str(uuid4()))
    def test_failure_and_rejection_are_recorded_without_changing_table(self):
        self.command('join');self.command('move',target_id='hall');self.scene.roll_die=lambda sides:1
        state=self.command('tip_over',target_id='table');self.assertFalse(state['entities'][0]['provides_cover'])
        state=self.command('propose',text='Намерение');proposal=state['proposals'][0]['id']
        state=self.command('resolve',proposal_id=proposal,parameters={'effect':'reject'})
        self.assertEqual(state['proposals'][0]['status'],'resolved');self.assertFalse(state['entities'][0]['provides_cover'])
