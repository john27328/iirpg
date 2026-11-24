"""Real process restart with frozen dice, plan continuation and cancelled offers; no AI API."""
import json
from pathlib import Path
import tempfile
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from uuid import uuid4
from iirpg.server import ChatHTTPServer
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
            server.roster.execute(room,{'action':'update_character','revision':roster['revision'],'id':actor,'data':{'sheet':{'values':{'athletics_other':80}}}},str(uuid4()))
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
