"""Evidence and its two different authors survive restart between actual rolls."""
from pathlib import Path
import json
import tempfile
import unittest
from urllib.error import HTTPError
from urllib.parse import parse_qs
from urllib.request import Request, urlopen
from uuid import uuid4

import test_campaign_persistence as fixture
from test_restart import running_server


class InvestigationRestartTests(unittest.TestCase):
    def test_parent_and_locked_continuation_survive_real_restart(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory=Path(temporary)
            server,room,client=fixture.CampaignPersistenceTests.create(self,directory)
            try:
                first=server.adventure.read(room)['character_id']
                with server.service.store._connection() as c:
                    server.games.participants.add(c,room,'Первый',character_id=first)
                    second=server.games.participants.add(c,room,'Второй')['character_id']
                players=server.games.participants.list(room)['players']
                tokens=[parse_qs(p['fragment'])['access'][0] for p in players]
                roster=server.roster.read(room)
                server.roster.execute(room,{'action':'update_character','revision':roster['revision'],'id':second,
                    'data':{'sheet':{'values':{'athletics_other':80}}}},str(uuid4()))
                def complete(payload,timeout):
                    if payload['response_format']['json_schema']['name']=='campaign_scene':
                        data=json.loads(payload['messages'][1]['content'])
                        value=fixture.description()
                        if data['seed']['name']=='Старый паром':
                            value['npcs'][0]['name']='Тимофей'
                            value['features'][0]={'name':'Запертый ящик','description':'Закрытый ящик с навесным замком.','kind':'obstacle'}
                            value['discoveries'][0].update(trigger='open',destination='quarry',
                                observation='В накладной: груз вернули в каменоломни.',
                                follow_up=data['open_leads'][0]['id'])
                        return {'content':json.dumps(value,ensure_ascii=False)}
                    return {'content':json.dumps({'intent':'act','reply':'','steps':client.steps})}
                server.ai.client.complete=complete
                def act(actor,steps):
                    client.steps=steps
                    return server.adventure.execute(room,{'revision':server.adventure.read(room)['revision'],
                        'character_id':actor,'text':'Изучаю найденный след.'},str(uuid4()))
                act(first,[{'rule':'move','target':'riverbank'}])
                act(first,[{'rule':'look','target':'riverbank:feature:0'}])
                act(second,[{'rule':'move','target':'old_ferry'}])
                before=act(second,[{'rule':'force_open','target':'old_ferry:feature:0'}])
                pending=before['pending_check']
                self.assertEqual(before['world']['leads']['riverbank:lead:0']['status'],'visited')
                self.assertNotIn('old_ferry:lead:0',before['world']['leads'])
            finally:server.server_close()
            root='/api/rooms/'+room
            def request(base,path,body=None,token='restart-action-plans-testing-token-123456789'):
                with urlopen(Request(base+root+path,data=None if body is None else json.dumps(body).encode(),
                        headers={'Content-Type':'application/json','Authorization':'Bearer '+token}),timeout=5) as response:
                    return json.load(response)
            dice={'sender':'first','offer_id':pending['offer_id'],'request_id':pending['roll_request_id']}
            with running_server(directory) as base:
                self.assertEqual(request(base,'/session'),before)
                with self.assertRaises(HTTPError) as error:request(base,'/tools/dice',dice,tokens[0])
                self.assertEqual(error.exception.code,403);error.exception.close()
                request(base,'/tools/dice',dice,tokens[1])
                after=request(base,'/session')
                parent=after['world']['leads']['riverbank:lead:0'];child=after['world']['leads']['old_ferry:lead:0']
                self.assertEqual(parent['status'],'followed')
                self.assertEqual(parent['discovered_by'],first)
                self.assertEqual(child['discovered_by'],second)
                self.assertEqual(child['follow_up'],parent['id'])
                request(base,'/tools/dice',dice,tokens[1])
                self.assertEqual(request(base,'/session'),after)
            with running_server(directory) as base:
                self.assertEqual(request(base,'/session'),after)
