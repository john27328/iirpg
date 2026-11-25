"""Real process restart with frozen dice, plan continuation and cancelled offers; no AI API."""
import json
from pathlib import Path
import tempfile
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from urllib.parse import parse_qs
from uuid import uuid4
from iirpg.server import ChatHTTPServer
from iirpg.character_resources import save_character
from iirpg.application import ChatService
from iirpg.dice import DiceTool
from test_restart import running_server


class PlanRestartTests(unittest.TestCase):
    def seed(self, directory):
        (directory/'access-token').write_text('restart-action-plans-testing-token-123456789')
        server=ChatHTTPServer(('127.0.0.1',0),directory/'chat.sqlite3')
        try:
            room=server.games.execute({'action':'create','name':'Перезапуск','scenario':'missing-book'},str(uuid4()))['room_id']
            state=server.adventure.read(room);actor=state['character_id']
            roster=server.roster.read(room);hero=next(a for a in roster['characters'] if a['id']==actor)
            hero['sheet']['values']['athletics_other']=80
            with server.service.store._connection() as c:save_character(c,room,hero)
            steps=[{'rule':'stow','target':'inventory:'+i['id']} for i in hero['items'] if i.get('hands')]
            proposal={'intent':'act','reply':'Попытка.','steps':steps+[{'rule':'move','target':'store'},{'rule':'force_open','target':'chest'},{'rule':'take','target':'book'}]}
            class Client:
                def ready(self):return True
                def complete(self,*args):return {'content':json.dumps(proposal)}
            server.ai.client=Client()
            state=server.adventure.execute(room,{'revision':state['revision'],'character_id':actor,'text':'Открываю и беру'},str(uuid4()))
            return room,state
        finally:server.server_close()

    def request(self,base,path,payload=None,key=None):
        headers={'Content-Type':'application/json','Authorization':'Bearer restart-action-plans-testing-token-123456789'}
        if key:headers['Idempotency-Key']=key
        with urlopen(Request(base+path,data=None if payload is None else json.dumps(payload).encode(),headers=headers),timeout=5) as response:
            return json.load(response)

    def test_open_promise_memory_and_narration_receipt_survive_restart(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory=Path(temporary)
            (directory/'access-token').write_text('restart-action-plans-testing-token-123456789')
            server=ChatHTTPServer(('127.0.0.1',0),directory/'chat.sqlite3')
            class Client:
                steps=[{'rule':'request','target':'hostess','method':'Не называть имени гостя.'}]
                def ready(self):return True
                def complete(self,payload,timeout):
                    if payload['response_format']['json_schema']['name']=='scene_narration':
                        return {'content':json.dumps({'reply':'Под навесом пахнет сеном.'})}
                    return {'content':json.dumps({'intent':'act','reply':'Попытка.','steps':self.steps})}
            client=Client();server.ai.client=client
            try:
                room=server.games.execute({'action':'create','scenario':'missing-book','name':'Открытая просьба'},str(uuid4()))['room_id']
                state=server.adventure.read(room);actor=state['character_id']
                state=server.adventure.execute(room,{'revision':state['revision'],'character_id':actor,'text':'Меня зовут Кир, я ищу сестру. Прошу не называть моего имени.'},str(uuid4()))
                pending=state['pending_check']
                ChatService(server.service.store,DiceTool(roll_die=lambda sides:20)).send(room,{
                    'sender':'first','type':'tool.call','tool':'dice.roll','request_id':pending['roll_request_id'],'offer_id':pending['offer_id']})
                state=server.adventure.read(room)
                client.steps=[{'rule':'move','target':'yard'}]
                key=str(uuid4());command={'revision':state['revision'],'character_id':actor,'text':'Выхожу во двор.'}
                after=server.adventure.execute(room,command,key)
                self.assertEqual(after['history'][-1]['action'],'narration')
                self.assertEqual(len(after['world']['entities']['hostess']['commitments']),1)
            finally:server.server_close()
            with running_server(directory) as base:
                root='/api/rooms/'+room+'/session'
                self.assertEqual(self.request(base,root),after)
                self.assertEqual(self.request(base,root,command,key),after)
                self.assertEqual(self.request(base,root),after)

    def test_player_roll_resumes_plan_and_inventory_survives_real_restart(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory=Path(temporary);room,before=self.seed(directory);root='/api/rooms/'+room
            with running_server(directory) as base:
                state=self.request(base,root+'/session');self.assertEqual(state,before)
                p=state['pending_check'];command={'sender':'first','request_id':p['roll_request_id'],'offer_id':p['offer_id']}
                self.request(base,root+'/tools/dice',command)
                after=self.request(base,root+'/session');self.assertTrue(after['book'])
                self.assertEqual(after['plan']['status'],'completed')
                self.request(base,root+'/tools/dice',command)
                self.assertEqual(self.request(base,root+'/session'),after)
                inventory=self.request(base,root+'/roster')
            with running_server(directory) as base:
                self.assertEqual(self.request(base,root+'/session'),after)
                self.assertEqual(self.request(base,root+'/roster'),inventory)

    def test_cancelled_offer_stays_revoked_after_real_restart(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory=Path(temporary);room,before=self.seed(directory);root='/api/rooms/'+room;p=before['pending_check']
            with running_server(directory) as base:
                after=self.request(base,root+'/session/cancel',{'offer_id':p['offer_id']})
                self.assertNotIn('pending_check',after)
            with running_server(directory) as base:
                self.assertEqual(self.request(base,root+'/session'),after)
                with self.assertRaises(HTTPError) as error:
                    self.request(base,root+'/tools/dice',{'sender':'first','request_id':p['roll_request_id'],'offer_id':p['offer_id']})
                self.assertEqual(error.exception.code,409);error.exception.close()
                history=self.request(base,root+'/messages?chat=first')
                self.assertFalse(any(m['type']=='tool.result' for m in history['messages']))

    def test_player_aid_survives_restart_between_helper_and_owner_rolls(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory=Path(temporary)
            (directory/'access-token').write_text('restart-action-plans-testing-token-123456789')
            server=ChatHTTPServer(('127.0.0.1',0),directory/'chat.sqlite3')
            class Client:
                steps=[]
                def ready(self):return True
                def complete(self,*args):return {'content':json.dumps({'intent':'act','reply':'Попытка.','steps':self.steps})}
            client=Client();server.ai.client=client;server.ai.config['session_narration']=False
            try:
                heroes=server.games.heroes.list()['heroes']
                game=server.games.execute({'action':'create','scenario':'missing-book','name':'Помощь вдвоём',
                    'party':[{'name':'Алина','hero_id':heroes[0]['id']},{'name':'Борис','hero_id':heroes[1]['id']}]},str(uuid4()))
                room=game['room_id'];players=game['players'];ids=[p['character_id'] for p in players]
                tokens=[parse_qs(p['fragment'])['access'][0] for p in players]
                for actor in ids:
                    roster=server.roster.read(room)
                    hero=next(a for a in roster['characters'] if a['id']==actor);hero['sheet']['values']['athletics_other']=80
                    with server.service.store._connection() as c:save_character(c,room,hero)
                def say(actor,steps):
                    client.steps=steps;state=server.adventure.read(room)
                    return server.adventure.execute(room,{'revision':state['revision'],'character_id':actor,'text':'Выполняю заявленное действие.'},str(uuid4()))
                first=next(h for h in server.roster.read(room)['characters'] if h['id']==ids[0])
                say(ids[0],[{'rule':'stow','target':'inventory:'+i['id']} for i in first['items'] if i.get('hands')]+[{'rule':'move','target':'store'}])
                say(ids[1],[{'rule':'aid','target':ids[0],'task':'chest','skill':'athletics'}])
                before=say(ids[0],[{'rule':'force_open','target':'chest'},{'rule':'take','target':'book'}])
                self.assertEqual(before['pending_check']['kind'],'player_aid')
            finally:server.server_close()
            root='/api/rooms/'+room
            def roll(base,player,pending):
                with urlopen(Request(base+root+'/tools/dice',data=json.dumps({'sender':'first',
                    'offer_id':pending['offer_id'],'request_id':pending['roll_request_id']}).encode(),
                    headers={'Content-Type':'application/json','Authorization':'Bearer '+tokens[player]}),timeout=5) as response:
                    return json.load(response)
            with running_server(directory) as base:
                state=self.request(base,root+'/session');self.assertEqual(state,before)
                helper=state['pending_check']
                with self.assertRaises(HTTPError) as error:roll(base,0,helper)
                self.assertEqual(error.exception.code,403);error.exception.close()
                roll(base,1,helper)
                middle=self.request(base,root+'/session')
                self.assertEqual(middle['pending_check']['character_id'],ids[0])
                self.assertEqual(middle['pending_check']['modifier'],helper['assisted_check']['modifier']+middle['pending_check']['aid']['bonus'])
                roll(base,1,helper)
                self.assertEqual(self.request(base,root+'/session'),middle)
            with running_server(directory) as base:
                self.assertEqual(self.request(base,root+'/session'),middle)
                roll(base,0,middle['pending_check'])
                after=self.request(base,root+'/session')
                self.assertEqual(after['world']['entities']['book']['owner'],ids[0])
                self.assertEqual(after['plan']['status'],'completed')
                roll(base,1,helper);roll(base,0,middle['pending_check'])
                self.assertEqual(self.request(base,root+'/session'),after)
