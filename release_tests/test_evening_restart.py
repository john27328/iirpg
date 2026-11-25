"""A finite solo ending and its receipt survive actual server restarts, offline."""
import json
from pathlib import Path
import tempfile
import unittest
from urllib.request import Request, urlopen
from uuid import uuid4

from iirpg.server import ChatHTTPServer
from test_restart import running_server


class EveningRestartTests(unittest.TestCase):
    def test_ready_and_completed_contracts_survive_restart(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory=Path(temporary);token='solo-restart-testing-token-123456789'
            (directory/'access-token').write_text(token)
            server=ChatHTTPServer(('127.0.0.1',0),directory/'chat.sqlite3',token)
            class Client:
                steps=[]
                def ready(self):return True
                def complete(self,payload,timeout):
                    name=payload['response_format']['json_schema']['name']
                    if name=='evening_outline':
                        value={'title':'Фонарь','opening':'Фонарь погас.','hub':'Двор фонарщика',
                            'patron':{'name':'Марта','role':'Фонарщица','voice':'Тихая речь.','request':'Найдите масло.'},
                            'truth':'Масло убрали ради ремонта.','stages':[
                                {'name':'Сарай','description':'У реки.','evidence':'Масло в сарае.','question':'Почему убрали?'},
                                {'name':'Мастерская','description':'За двором.','evidence':'План ремонта.','question':'Что сообщить?'}],
                            'ending':'Марта ремонтирует фонарь.'}
                    elif name=='campaign_scene':
                        value={'description':'Место тихое.','npcs':[],
                            'features':[{'kind':'landmark','name':'Записка','description':'Бумага на стене.'}]}
                    else:value={'intent':'act','reply':'','steps':self.steps}
                    return {'content':json.dumps(value)}
            client=Client();server.ai.client=client;server.ai.config['session_narration']=False
            try:
                game=server.games.execute({'action':'create','campaign':'solo-evening','name':'Соло'},str(uuid4()))
                room=game['room_id'];state=server.adventure.read(room);hero=state['character_id']
                server.adventure.adventure_control(room,{'action':'start','revision':state['revision'],'character_id':hero},str(uuid4()))
                for stage in server.adventure.read(room)['world']['adventure']['stages']:
                    client.steps=[{'rule':'move','target':stage['id']},{'rule':'look','target':stage['id']+':feature:0'}]
                    server.adventure.execute(room,{'revision':server.adventure.read(room)['revision'],'character_id':hero,'text':'Осматриваю место.'},str(uuid4()))
                before=server.adventure.read(room)
                self.assertEqual(before['adventure']['status'],'ready')
            finally:server.server_close()
            root='/api/rooms/'+room;key=str(uuid4())
            command={'action':'finish','revision':before['revision'],'character_id':hero}
            def request(base,path,body=None):
                with urlopen(Request(base+root+path,data=None if body is None else json.dumps(body).encode(),
                    headers={'Content-Type':'application/json','Authorization':'Bearer '+token,'Idempotency-Key':key}),timeout=5) as response:
                    return json.load(response)
            with running_server(directory) as base:
                self.assertEqual(request(base,'/session'),before)
                finished=request(base,'/session/adventure',command)
                self.assertEqual(finished['adventure']['status'],'completed')
            with running_server(directory) as base:
                self.assertEqual(request(base,'/session'),finished)
                self.assertEqual(request(base,'/session/adventure',command),finished)
                self.assertFalse(request(base,'/session/status')['thinking'])
