"""Agent-approved world seeds, compressed hooks and receipts survive a real process restart."""
import json
from pathlib import Path
import tempfile
import unittest
from urllib.request import Request, urlopen
from uuid import uuid4

from iirpg.server import ChatHTTPServer
from test_restart import running_server


class WorldGrowthRestartTests(unittest.TestCase):
    def test_verified_seed_and_receipt_survive_real_restart_without_new_model_calls(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory=Path(temporary)
            token='restart-action-plans-testing-token-123456789'
            (directory/'access-token').write_text(token)
            server=ChatHTTPServer(('127.0.0.1',0),directory/'chat.sqlite3')
            class Client:
                def ready(self):return True
                def complete(self,payload,timeout):
                    name=payload['response_format']['json_schema']['name']
                    if name=='session_verification':value={'ok':True,'issues':[]}
                    elif name=='action_plan':value={'intent':'question','reply':'Мара рассказывает местный слух.',
                                                    'steps':[],'voice':'npc','interlocutor':'mara','world_request':True}
                    elif name=='scene_narration':value={'reply':'Мара рассказывает о заброшенной лодочной у протоки.',
                        'world_additions':[{'name':'Заброшенная лодочная','premise':'Маленький сарай у рыночной протоки.',
                            'rumor':'Говорят, там оставили груз.','question':'Кто оставил груз?','speaker_id':'mara'}]}
                    else:raise AssertionError(name)
                    return {'content':json.dumps(value,ensure_ascii=False)}
            server.ai.client=Client()
            try:
                room=server.games.execute({'action':'create','campaign':'river-marches'},str(uuid4()))['room_id']
                before=server.adventure.read(room)
                command={'revision':before['revision'],'character_id':before['character_id'],
                         'text':'Мара, что необычного можно исследовать поблизости?'}
                key=str(uuid4());receipt=server.adventure.execute(room,command,key)
                count=server.ai.status()['requests_today']
                self.assertEqual(count,4)
                self.assertEqual(len(receipt['world']['story_hooks']),1)
            finally:server.server_close()
            with running_server(directory) as base:
                def request(body=None):
                    req=Request(base+'/api/rooms/'+room+'/session',
                                data=None if body is None else json.dumps(body).encode(),
                                headers={'Authorization':'Bearer '+token,'Content-Type':'application/json','Idempotency-Key':key})
                    with urlopen(req,timeout=5) as response:return json.load(response)
                self.assertEqual(request(),receipt)
                self.assertEqual(request(command),receipt)
                with urlopen(Request(base+'/api/ai',headers={'Authorization':'Bearer '+token}),timeout=5) as response:
                    self.assertEqual(json.load(response)['requests_today'],count)
