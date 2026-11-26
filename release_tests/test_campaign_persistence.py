"""Campaign restart and a simulated long history; no external AI calls."""
from copy import deepcopy
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from urllib.request import Request, urlopen
from uuid import uuid4

from iirpg import campaign_storage
from iirpg.server import ChatHTTPServer
from iirpg.session_context import context, schema
from test_restart import running_server


def description(name='Тихон'):
    return {'description':'На высоком берегу растут ивы. Видны старый паром и новый мост.',
        'npcs':[{'name':name,'role':'Перевозчик','voice':'Негромкий, внимательный.','goal':'Найти груз.',
                 'boundary':'Не обвиняет без доказательств.','knowledge':['Паром продолжает работать.']}],
        'features':[{'name':'Путевая записка','description':'Здесь ждали груз.','kind':'item'}],
        'discoveries':[{'source':0,'trigger':'look','observation':'Груз ожидают у старого парома.',
                       'destination':'old_ferry','question':'Кто примет груз?','follow_up':''}]}


class CampaignPersistenceTests(unittest.TestCase):
    def create(self, directory):
        (directory/'access-token').write_text('restart-action-plans-testing-token-123456789')
        server=ChatHTTPServer(('127.0.0.1',0),directory/'chat.sqlite3')
        server.ai.config['session_agent']=False  # Agent review is tested separately.
        class Client:
            steps=[]
            def ready(self):return True
            def complete(self,payload,timeout):
                name=payload['response_format']['json_schema']['name']
                result=description() if name=='campaign_scene' else {'intent':'act','reply':'','steps':self.steps}
                return {'content':json.dumps(result,ensure_ascii=False)}
        client=Client();server.ai.client=client;server.ai.config['session_narration']=False
        room=server.games.execute({'action':'create','campaign':'river-marches'},str(uuid4()))['room_id']
        return server,room,client

    def test_scene_inventory_journal_and_receipt_survive_real_restart(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory=Path(temporary);server,room,client=self.create(directory)
            try:
                actor=server.adventure.read(room)['character_id']
                client.steps=[{'rule':'move','target':'riverbank'}]
                command={'revision':server.adventure.read(room)['revision'],'character_id':actor,'text':'Иду к берегу.'};key=str(uuid4())
                receipt=server.adventure.execute(room,command,key)
                hero=server.roster.read(room)['characters'][0]
                client.steps=[{'rule':'stow','target':'inventory:'+i['id']} for i in hero['items'] if i.get('hands')]+[{'rule':'take','target':'riverbank:feature:0'}]
                server.adventure.execute(room,{'revision':receipt['revision'],'character_id':actor,'text':'Убираю оружие и беру записку.'},str(uuid4()))
                with server.service.store._connection() as c:
                    state=server.adventure._load(c,room)
                    state['history'].extend({'player':f'Запомни пароль: янтарная дорога {i}','gm':'Событие сохранено.',
                        'character_id':actor,'listeners':['mara'],'action':'question','chat_index':1000+i} for i in range(200))
                    state['revision']=1200;server.adventure.save(c,room,state)
                before=server.adventure.read(room);inventory=server.roster.read(room)
            finally:server.server_close()
            def request(base,path,body=None,request_key=None):
                with urlopen(Request(base+'/api/rooms/'+room+path,data=None if body is None else json.dumps(body).encode(),
                        headers={'Authorization':'Bearer restart-action-plans-testing-token-123456789','Content-Type':'application/json',
                                 'Idempotency-Key':request_key or str(uuid4())}),timeout=5) as response:return json.load(response)
            with running_server(directory) as base:
                self.assertEqual(request(base,'/session'),before)
                self.assertEqual(request(base,'/roster'),inventory)
                self.assertEqual(request(base,'/session',command,key),receipt)
                self.assertEqual(request(base,'/session'),before)
            with running_server(directory) as base:
                self.assertEqual(request(base,'/session'),before)
                self.assertEqual(request(base,'/roster'),inventory)
            with sqlite3.connect(directory/'chat.sqlite3') as c:
                c.row_factory=sqlite3.Row
                old=campaign_storage.recall(c,before,'янтарная дорога',actor,'mara')
                self.assertTrue(any('янтарная дорога' in r['player'] for r in old))
                raw=json.loads(c.execute('SELECT body FROM adventures WHERE room_id=?',(room,)).fetchone()[0])
                self.assertNotIn('riverbank',raw['world']['places'])
                self.assertEqual(raw['world']['entities']['riverbank:feature:0']['owner'],actor)

    def test_long_history_and_hundreds_of_places_keep_context_local(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory=Path(temporary);server,room,client=self.create(directory)
            try:
                actor_id=server.adventure.read(room)['character_id']
                with server.service.store._connection() as c:
                    state=server.adventure._load(c,room)
                    for i in range(500):
                        key=f'long_place_{i}'
                        place={'name':f'Дальний участок {i}','description':'Тропа у реки. '*50,'parent':'old_ferry',
                               'exits':[{'to':'old_ferry'}],'unexplored':False}
                        addition={'places':{key:place},'entities':{key+':stone':{'kind':'landmark','name':f'Камень {i}','place':key,'description':'Мох на береговом камне. '*20}}}
                        state['campaign']['scene_refs'][key]=campaign_storage.put(c,'scene',addition)
                        state['world']['places'].update(addition['places']);state['world']['entities'].update(addition['entities'])
                    raw_world_size=len(json.dumps(state['world'],ensure_ascii=False).encode())
                    for offset in range(0,10000,100):
                        state['history'].extend({'player':f'Событие долгой дороги {i}.','gm':'У реки по-прежнему слышен ветер, а путники продолжают разговор.',
                            'character_id':actor_id,'listeners':['mara'],'action':'question','chat_index':i+1000}
                            for i in range(offset,offset+100))
                        state['revision']=offset+1100
                        server.adventure.save(c,room,state)
                    actor,_=server.adventure.actor(c,room,actor_id)
                    hydrated=server.adventure._load(c,room)
                    encoded=c.execute('SELECT body FROM adventures WHERE room_id=?',(room,)).fetchone()[0]
                    before_count=c.execute('SELECT COUNT(*) FROM campaign_blobs WHERE kind=\'scene\'').fetchone()[0]
                    frozen=campaign_storage.freeze_result(c,server.adventure.view(hydrated))
                    self.assertEqual(campaign_storage.freeze_result(c,server.adventure.view(hydrated)),frozen)
                    self.assertEqual(campaign_storage.thaw_result(c,frozen),server.adventure.view(hydrated))
                    self.assertEqual(c.execute('SELECT COUNT(*) FROM campaign_blobs WHERE kind=\'scene\'').fetchone()[0],before_count)
                    stored_bytes=c.execute('SELECT SUM(length(body)) FROM campaign_blobs').fetchone()[0]
                prompt_bytes=len(json.dumps(context(hydrated,actor,'Мара, что слышно о дороге?'),ensure_ascii=False).encode())
                self.assertLess(len(hydrated['history']),97)
                self.assertGreaterEqual(hydrated['campaign']['archived_entries'],9900)
                self.assertLess(len(encoded.encode()),raw_world_size//4)
                self.assertLess(prompt_bytes,48000)
                self.assertNotIn('long_place_',json.dumps(schema(hydrated)))
                print(json.dumps({'campaign_scale':{'places':len(hydrated['world']['places']),'events':10000,
                    'world_json_bytes':raw_world_size,'active_state_bytes':len(encoded.encode()),
                    'compressed_artifact_bytes':stored_bytes,'prompt_bytes':prompt_bytes}},ensure_ascii=False),flush=True)
            finally:server.server_close()
