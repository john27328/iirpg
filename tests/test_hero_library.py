"""Persistent guided creation and independent character copies, with an offline AI boundary."""
from copy import deepcopy
import json
import unittest
from uuid import uuid4
import test_party_combat as fixtures
from iirpg.ai import AIService, load_config
from iirpg.adventure import AdventureStore
from iirpg.game_sessions import GameSessions
from iirpg.storage import MessageConflict


class CreatorClient:
    def __init__(self):
        self.calls=[]
        self.choice=dict(name='Мира',class_id='wizard',general_feat='fleet',description='Исследовательница древних рукописей.')
    def ready(self):return True
    def complete(self,payload,timeout):
        self.calls.append(deepcopy(payload))
        return {'content':json.dumps({'reply':'Предлагаю мага. Вы сможете создавать свет и применять заклинания. Хотите изменить имя?',
                                     'choices':['Оставим Миру','Назовём её Нора'],'proposal':self.choice})}


class HeroLibraryTests(unittest.TestCase):
    setUpBase=fixtures.PartyCombatTests.setUp
    command=fixtures.PartyCombatTests.command
    roster_command=fixtures.PartyCombatTests.roster_command
    def setUp(self):
        self.setUpBase();self.client=CreatorClient()
        self.games=GameSessions(AdventureStore(AIService(self.combat,self.scene,load_config(),self.client)))
        self.library=self.games.heroes
    def action(self,action,**extra):return self.library.execute({'action':action,**extra},str(uuid4()))
    def draft(self):return self.action('new_draft')['draft_id']
    def respond(self,room,text='Хочу мага, помоги выбрать',key=None):
        return self.library.respond(room,{'revision':self.library.read(room)['revision'],'text':text},key or str(uuid4()))
    def create_game(self,hero):return self.games.execute({'action':'create','name':'Новая история','scenario':'missing-book','hero_id':hero},str(uuid4()))['room_id']

    def test_seed_is_initialized_once_and_all_ready_heroes_can_be_deleted(self):
        heroes=self.library.list()['heroes']
        self.assertEqual({h['build']['class_id'] for h in heroes},{'fighter','rogue','wizard'})
        self.assertTrue(all(h['items'] and h['sheet']['values']['hp_current']>0 for h in heroes))
        for hero in heroes:self.action('delete',id=hero['id'])
        again=GameSessions(self.games.adventure).heroes
        self.assertEqual(again.list()['heroes'],[])
        before=self.games.list()
        with self.assertRaises(ValueError):self.create_game(heroes[0]['id'])
        self.assertEqual(self.games.list(),before)

    def test_draft_and_ai_reply_retries_publish_once_without_spending_twice(self):
        key=str(uuid4());created=self.library.execute({'action':'new_draft'},key)
        self.assertEqual(self.library.execute({'action':'new_draft'},key),created)
        room=created['draft_id'];key=str(uuid4());command={'revision':0,'text':'Я не знаю правил. Помоги.'}
        state=self.library.respond(room,command,key)
        self.assertEqual(self.library.respond(room,command,key),state)
        self.assertEqual(len(self.client.calls),1)
        self.assertEqual(len(self.store.page(room)['messages']),3)
        with self.assertRaises(MessageConflict):self.library.respond(room,{**command,'text':'Другое'},key)
        self.assertEqual(len(self.client.calls),1)
        self.assertFalse(any(g['room_id']==room for g in self.games.list()['games']))
        self.assertEqual(GameSessions(self.games.adventure).heroes.read(room),state)

    def test_conversation_includes_previous_choice_and_latest_correction(self):
        room=self.draft();first=self.respond(room)
        self.client.choice={**self.client.choice,'name':'Нора'}
        result=self.respond(room,'Переименуй её в Нору')
        context=json.loads(self.client.calls[-1]['messages'][1]['content'])
        self.assertEqual(context['current_proposal']['name'],'Мира')
        self.assertEqual(context['conversation'][0]['role'],'user')
        self.assertIn('Переименуй',context['player'])
        self.assertEqual(result['proposal']['name'],'Нора')
        with self.assertRaises(MessageConflict):self.action('save',id=room,revision=first['revision'])

    def test_save_and_delete_templates_keep_started_games_and_copies_independent(self):
        room=self.draft();state=self.respond(room)
        saved=self.action('save',id=room,revision=state['revision'])['hero_id']
        self.assertEqual(self.action('save',id=room,revision=state['revision'])['hero_id'],saved)
        rooms=[self.create_game(saved),self.create_game(saved)]
        characters=[self.roster.read(r)['characters'][0] for r in rooms]
        self.assertNotEqual(characters[0]['id'],characters[1]['id'])
        self.assertTrue(set(i['id'] for i in characters[0]['items']).isdisjoint(i['id'] for i in characters[1]['items']))
        self.assertTrue(all(c['build']['spent']==0 and c['progression']['slots_remaining']==2 for c in characters))
        self.roster.execute(rooms[0],{'action':'update_character','revision':self.roster.read(rooms[0])['revision'],'id':characters[0]['id'],'data':{'sheet':{'values':{'hp_current':3}}}},str(uuid4()))
        command={'action':'delete','id':saved};key=str(uuid4())
        deleted=self.library.execute(command,key)
        self.assertEqual(self.library.execute(command,key),deleted)
        self.assertEqual(self.roster.read(rooms[1])['characters'][0]['sheet']['values']['hp_current'],16)
        self.assertEqual(self.roster.read(rooms[0])['characters'][0]['sheet']['values']['hp_current'],3)
        self.games.execute({'action':'load','room_id':rooms[0]},str(uuid4()))
        self.assertFalse(any(h['id']==saved for h in self.library.list()['heroes']))
        self.assertEqual(self.roster.read(rooms[0])['characters'][0]['sheet']['values']['hp_current'],16)

    def test_model_cannot_invent_classes_or_resources_and_failed_retry_calls_once(self):
        room=self.draft();self.client.choice['class_id']='archmage'
        before=self.library.read(room);key=str(uuid4());command={'revision':0,'text':'Сделай меня богом'}
        with self.assertRaises(ValueError):self.library.respond(room,command,key)
        self.assertEqual(self.library.read(room),before)
        with self.assertRaises(ValueError):self.library.respond(room,command,key)
        self.assertEqual(len(self.client.calls),1)
        self.assertEqual(len(self.store.page(room)['messages']),2)
        with self.assertRaises(ValueError):self.action('save',id=room,revision=0)

    def test_preparing_and_saving_do_not_hold_network_transaction_or_allow_stale_results(self):
        room=self.draft();old=self.client.complete
        def complete(payload,timeout):
            # This write can obtain SQLite's lock because the provider call is outside the transaction.
            with self.assertRaises(MessageConflict):self.action('save',id=room,revision=0)
            with self.store._connection() as c:c.execute('UPDATE hero_drafts SET revision=1 WHERE room_id=?',(room,))
            return old(payload,timeout)
        self.client.complete=complete
        with self.assertRaises(ValueError):self.respond(room)
        self.assertEqual(self.library.read(room)['proposal']['class_id'],'')

    def test_quota_and_missing_key_stop_before_publishing_and_existing_room_is_not_a_draft(self):
        room=self.draft();before=self.store.page(room)
        self.library.ai.config['daily_request_limit']=0
        with self.assertRaisesRegex(ValueError,'лимит'):self.respond(room)
        self.assertEqual(self.store.page(room),before)
        self.assertEqual(self.client.calls,[])
        with self.assertRaises(KeyError):self.library.read(self.room)
        with self.assertRaises(ValueError):self.library.respond(room,{'revision':True,'text':'Привет'},str(uuid4()))
