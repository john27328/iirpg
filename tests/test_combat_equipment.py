"""Actual held equipment must determine story combat, independent of AI wording."""
import json
from pathlib import Path
import tempfile
import unittest
from uuid import uuid4

from iirpg.server import ChatHTTPServer


class CombatEquipmentTests(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup)
        self.server=ChatHTTPServer(('127.0.0.1',0),Path(temporary.name)/'game.sqlite3');self.addCleanup(self.server.server_close)
        self.server.ai.config['session_agent']=False
        class Client:
            def ready(self):return True
            def complete(self,*args):return {'content':json.dumps({'intent':'act','reply':'','steps':[{'rule':'combat','target':'warden'}]})}
        self.server.ai.client=Client()
        game=self.server.games.execute({'action':'create','scenario':'watch-post','name':'Снаряжение'},str(uuid4()))
        self.room=game['room_id']
    def hero(self):return next(a for a in self.server.roster.read(self.room)['characters'] if a['kind']=='pc')
    def item(self,item_name,**data):
        roster=self.server.roster.read(self.room);hero=self.hero();item=next(i for i in hero['items'] if i['name']==item_name)
        self.server.roster.execute(self.room,{'action':'update_item','revision':roster['revision'],'character_id':hero['id'],'id':item['id'],'data':data},str(uuid4()))
    def start(self):
        state=self.server.adventure.read(self.room)
        self.server.adventure.execute(self.room,{'revision':state['revision'],'character_id':state['character_id'],'text':'Начинаю поединок со сторожем.'},str(uuid4()))
        return next(f for f in self.server.combat.read(self.room)['fighters'] if f['team']=='players')

    def test_stowed_shield_is_not_available_in_story_combat(self):
        self.item('Деревянный щит',state='stowed')
        self.assertFalse(self.start()['has_shield'])

    def test_stowed_sword_does_not_turn_into_a_ghost_weapon(self):
        self.item('Длинный меч',state='stowed');self.item('Деревянный щит',state='stowed')
        fighter=self.start()
        self.assertFalse(fighter['has_shield']);self.assertEqual(fighter['damage_die'],4)
        self.assertEqual(fighter['weapon_name'],'Безоружная атака');self.assertTrue(fighter['agile'])

    def test_held_starter_equipment_retains_its_actual_profile(self):
        fighter=self.start()
        self.assertTrue(fighter['has_shield']);self.assertEqual(fighter['damage_die'],8)

    def test_broken_equipment_is_not_a_ready_weapon_or_shield(self):
        self.item('Длинный меч',broken=True);self.item('Деревянный щит',broken=True)
        fighter=self.start()
        self.assertFalse(fighter['has_shield']);self.assertIsNone(fighter['weapon_item_id'])
        self.assertEqual(fighter['damage_die'],4)

    def test_fighter_holding_dagger_gets_dagger_damage_and_agile(self):
        self.item('Длинный меч',state='stowed')
        roster=self.server.roster.read(self.room)
        self.server.roster.execute(self.room,{'action':'add_item','revision':roster['revision'],'character_id':self.hero()['id'],
            'data':{'name':'Кинжал','category':'weapon','bulk':'L','state':'held'}},str(uuid4()))
        fighter=self.start()
        self.assertEqual(fighter['weapon_name'],'Кинжал');self.assertEqual(fighter['damage_die'],4)
        self.assertTrue(fighter['agile']);self.assertEqual(fighter['attack'],9)

    def test_unknown_weapon_does_not_silently_use_class_damage(self):
        self.item('Длинный меч',name='Неподдерживаемое оружие')
        state=self.server.adventure.read(self.room)
        with self.assertRaisesRegex(ValueError, 'пока не поддержан'):
            self.server.adventure.execute(self.room,{'revision':state['revision'],'character_id':state['character_id'],'text':'Начинаю поединок.'},str(uuid4()))
        after=self.server.adventure.read(self.room)
        self.assertFalse(after.get('active_combat'))
        self.assertNotEqual(self.server.combat.read(self.room).get('status'),'active')
