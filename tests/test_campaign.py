"""Campaign canon, lazy generation and branch-safe compressed memory; no AI APIs."""
from copy import deepcopy
import json
import unittest
from uuid import uuid4

import test_multiplayer as multiplayer_fixture
from iirpg import campaign, campaign_storage
from iirpg.adventure import AdventureStore
from iirpg.session_context import context, schema

SCENE = {'description':'С высокого берега видны старый паром и дорога к новому мосту.',
 'npcs':[{'name':'Нела','role':'Смотрительница берега','voice':'Говорит спокойно и точно.',
 'goal':'Понять, почему задерживается груз.','boundary':'Не обвиняет без доказательств.',
 'knowledge':['Старый паром продолжает работать.']}],
 'features':[{'name':'Записка перевозчика','description':'Груз ожидали вчера у старого парома.','kind':'item'},
 {'name':'Старый указатель','description':'Тропа спускается к старому парому.','kind':'landmark'}]}


class CampaignTests(unittest.TestCase):
    request = multiplayer_fixture.MultiplayerTests.request
    say = multiplayer_fixture.MultiplayerTests.say
    roll = multiplayer_fixture.MultiplayerTests.roll

    def setUp(self):
        multiplayer_fixture.MultiplayerTests.setUp(self)
        self.generated = 0; self.generated_value = deepcopy(SCENE); self.on_generation = None
        def complete(payload, timeout):
            data = json.loads(payload['messages'][1]['content']); self.calls.append(data)
            name = payload['response_format']['json_schema']['name']
            if name == 'campaign_scene':
                self.generated += 1
                if self.on_generation:self.on_generation()
                value = deepcopy(self.generated_value)
                if 'discoveries' not in value:
                    root = next((k for k,p in data['main_points'].items() if p['name']==data['seed']['name']),data['seed'].get('parent'))
                    prior = next((lead for lead in data['open_leads'] if lead['destination']==root and lead['status']!='followed'),None)
                    value['discoveries']=[{'source':0,'trigger':'look','observation':'На записке указан следующий пункт пути.',
                        'destination':next(k for k in data['main_points'] if k != root),
                        'question':'Кто видел груз?','follow_up':prior['id'] if prior else ''}]
                if data['seed']['name'] != 'Высокий берег реки' and value.get('npcs'):
                    value['npcs'][0]['name'] = 'Смотритель '+data['seed']['name'][:40]
                return {'content':json.dumps(value,ensure_ascii=False)}
            if name == 'scene_narration':return {'content':json.dumps({'reply':'С берега видны оба пути.'})}
            return {'content':json.dumps(self.proposal,ensure_ascii=False)}
        self.server.ai.client.complete = complete

    def create(self, count):
        status, game = self.request('/api/games',{'action':'create','campaign':'river-marches','name':'Долгая кампания',
            'party':[{'name':f'Игрок {i+1}','hero_id':self.heroes[i]['id']} for i in range(count)]})
        self.assertEqual(status,200,game)
        from urllib.parse import parse_qs
        self.room=game['room_id'];self.root='/api/rooms/'+self.room;self.players=game['players']
        self.tokens=[parse_qs(p['fragment'])['access'][0] for p in self.players]
        self.server.ai.config['session_narration']=False
        return game

    def state(self):return self.server.adventure.read(self.room)
    def edit(self, fn):
        with self.server.service.store._connection() as c:
            state=self.server.adventure._load(c,self.room);fn(state);self.server.adventure.save(c,self.room,state)
    def actor(self, i=0):
        with self.server.service.store._connection() as c:return self.server.adventure.actor(c,self.room,self.players[i]['character_id'])[0]

    def test_campaign_owns_world_and_keeps_starting_episode(self):
        state=self.state()
        self.assertEqual(state['scenario_id'],'crossroads')
        self.assertEqual(state['campaign']['id'],'river-marches')
        self.assertEqual(state['campaign']['episodes'],[{'id':'crossroads','status':'active'}])
        self.assertTrue(state['world']['places']['riverbank']['unexplored'])
        self.assertEqual(state['world']['entities']['mara']['trade']['cash_cp'],900)
        self.assertEqual(self.generated,0)
        self.assertEqual([self.actor(i)['coins'] for i in range(2)],[300,300])

    def test_first_visit_generates_once_and_retry_returns_identical_receipt(self):
        before=self.state();key=str(uuid4())
        command={'revision':before['revision'],'character_id':self.players[0]['character_id'],'text':'Иду к реке.'}
        self.proposal={'intent':'act','reply':'','steps':[{'rule':'move','target':'riverbank'}]}
        status, response=self.request(self.root+'/session',command,key=key)
        self.assertEqual(status,200,response)
        after=self.state();self.assertEqual(after['location'],'riverbank');self.assertEqual(self.generated,1)
        status, replay=self.request(self.root+'/session',command,key=key)
        self.assertEqual(status,200);self.assertEqual(response,replay);self.assertEqual(self.generated,1)
        self.say(1,{'rule':'move','target':'market'});self.say(0,{'rule':'move','target':'riverbank'})
        self.assertEqual(self.generated,1)
        self.assertEqual(self.state()['world']['entities']['riverbank:npc:0']['name'],'Нела')
        self.assertEqual(AdventureStore(self.server.ai).read(self.room)['campaign']['scene_refs'],after['campaign']['scene_refs'])

    def test_generated_canon_and_inventory_deltas_survive_hydration(self):
        self.say(0,{'rule':'move','target':'riverbank'})
        actor=self.actor()
        self.say(0,*[{'rule':'stow','target':'inventory:'+i['id']} for i in actor['items'] if i.get('hands')],
                 {'rule':'take','target':'riverbank:feature:0'})
        self.say(1,{'rule':'make_impression','target':'riverbank:npc:0'})
        self.roll(1,self.state()['pending_check'])
        after=self.state()
        self.assertEqual(after['world']['entities']['riverbank:feature:0']['owner'],self.players[0]['character_id'])
        self.assertTrue(after['world']['entities']['riverbank:npc:0'].get('impressions'))
        self.assertEqual(sum(i['name']=='Записка перевозчика' for i in self.actor()['items']),1)
        with self.server.service.store._connection() as c:
            raw=json.loads(c.execute('SELECT body FROM adventures WHERE room_id=?',(self.room,)).fetchone()[0])
            self.assertNotIn('riverbank:feature:1',raw['world']['entities'])
            self.assertIn('riverbank:feature:0',raw['world']['entities'])
        self.assertEqual(self.state(),after)

    def test_local_discovery_has_stable_identity_and_backtracking(self):
        self.say(0,{'rule':'move','target':'riverbank'})
        self.say(0,{'rule':'explore','target':'riverbank','method':'Старая лодочная'})
        site=self.state()['location'];self.assertTrue(site.startswith('site_'));self.assertEqual(self.generated,2)
        self.say(1,{'rule':'move','target':'riverbank'})
        self.say(1,{'rule':'explore','target':'riverbank','method':'СТАРАЯ ЛОДОЧНАЯ'})
        self.assertEqual(self.state()['location'],site);self.assertEqual(self.generated,2)
        self.assertEqual(self.state()['world']['places'][site]['parent'],'riverbank')

    def test_generator_cannot_patch_money_or_existing_characters(self):
        before=self.state();self.generated_value['coins']=999999
        self.assertEqual(self.say(0,{'rule':'move','target':'riverbank'})[0],400)
        after=self.state();self.assertEqual(after['world'],before['world']);self.assertFalse(after['campaign']['scene_refs'])
        self.assertEqual(self.actor()['coins'],300)
        self.generated_value=deepcopy(SCENE)
        self.say(0,{'rule':'move','target':'riverbank'})
        self.assertEqual(self.generated,3);self.assertEqual(self.state()['location'],'riverbank')

    def test_stale_generated_scene_is_not_committed(self):
        def interrupt():self.edit(lambda s:s.update(revision=s['revision']+1,newer_fact='сохранён'))
        self.on_generation=interrupt
        status,_=self.say(0,{'rule':'move','target':'riverbank'})
        self.assertEqual(status,409)
        state=self.state();self.assertTrue(state['world']['places']['riverbank']['unexplored'])
        self.assertFalse(state['campaign']['scene_refs']);self.assertEqual(state['newer_fact'],'сохранён')

    def test_daily_limit_includes_generation_without_partial_scene(self):
        self.server.ai.config['daily_request_limit']=1
        self.assertEqual(self.say(0,{'rule':'move','target':'riverbank'})[0],400)
        self.assertEqual(self.generated,0);self.assertEqual(self.state()['location'],'market')
        self.assertFalse(self.state()['campaign']['scene_refs'])

    def test_duplicate_character_is_rejected_then_corrected_once(self):
        def candidate():self.generated_value['npcs'][0]['name']='Селена' if self.generated==1 else 'Нела'
        self.on_generation=candidate
        self.assertEqual(self.say(0,{'rule':'move','target':'riverbank'})[0],200)
        self.assertEqual(self.generated,2)
        self.assertEqual(self.state()['world']['entities']['selena']['place'],'clinic')
        self.assertEqual(self.state()['world']['entities']['riverbank:npc:0']['name'],'Нела')

    def test_transport_error_is_not_retried_and_no_scene_enters_canon(self):
        def outage():raise ValueError('Provider unavailable')
        self.on_generation=outage
        self.assertEqual(self.say(0,{'rule':'move','target':'riverbank'})[0],400)
        self.assertEqual(self.generated,1)
        self.assertFalse(self.state()['campaign']['scene_refs'])

    def test_checkpoint_excludes_future_scenes_and_reuses_saved_canon(self):
        self.server.games.execute({'action':'save','room_id':self.room},str(uuid4()))
        self.say(0,{'rule':'move','target':'riverbank'});first=self.state()['campaign']['scene_refs']['riverbank']
        self.server.games.execute({'action':'load','room_id':self.room},str(uuid4()))
        self.assertTrue(self.state()['world']['places']['riverbank']['unexplored'])
        self.assertNotIn('riverbank:npc:0',self.state()['world']['entities'])
        self.generated_value['npcs'][0]['name']='Тая'
        self.say(0,{'rule':'move','target':'riverbank'})
        self.assertEqual(self.generated,2);self.assertNotEqual(self.state()['campaign']['scene_refs']['riverbank'],first)
        self.server.games.execute({'action':'save','room_id':self.room},str(uuid4()))
        self.say(0,{'rule':'move','target':'market'})
        self.server.games.execute({'action':'load','room_id':self.room},str(uuid4()))
        self.say(1,text='Как зовут смотрительницу?',intent='question')
        self.assertEqual(self.generated,2);self.assertEqual(self.state()['world']['entities']['riverbank:npc:0']['name'],'Тая')

    def test_old_history_is_losslessly_archived_and_recalled_with_source(self):
        speaker=self.players[1]['character_id'];source=str(uuid4())
        history=[{'player':'Моя сестра Альма ждёт пароль: семь тихих колоколов.','gm':'Запомнила.',
            'character_id':speaker,'actor_name':'Лира','listeners':['mara'],'player_request_id':source,'action':'question'}]
        history.extend({'player':f'Смотрю на дорогу {i}','gm':'Ветер у переправы.','character_id':self.players[0]['character_id'],
                        'listeners':['mara'],'action':'question'} for i in range(1199))
        self.edit(lambda s:s.update(history=deepcopy(history)))
        state=self.state();self.assertEqual(len(state['history']),64)
        self.assertEqual(state['campaign']['archived_entries'],1136)
        with self.server.service.store._connection() as c:
            blob=campaign_storage.get(c,state['campaign']['journal_head'],'journal')
            self.assertEqual(blob['entries'],history[:-64])
            found=campaign_storage.recall(c,state,'Альма семь тихих колоколов',speaker,'mara')
            other=campaign_storage.recall(c,state,'Альма семь тихих колоколов',speaker,'iven')
        self.assertTrue(any(x['source']==source and x['character_id']==speaker for x in found));self.assertEqual(other,[])
        self.say(0,text='Мара, вспомни: Альма и семь тихих колоколов?',intent='question')
        self.assertTrue(any(x.get('source')==source for x in self.calls[-1]['recollections']))

    def test_loaded_branch_cannot_recall_future_archived_text(self):
        self.edit(lambda s:s.update(history=[{'player':'Старый мир','gm':'До развилки.','action':'question'} for _ in range(120)]))
        self.server.games.execute({'action':'save','room_id':self.room},str(uuid4()))
        old_head=self.state()['campaign']['journal_head']
        self.edit(lambda s:s['history'].extend({'player':'Секрет будущего: яшмовый дракон','gm':'После развилки.','action':'question'} for _ in range(120)))
        self.assertNotEqual(self.state()['campaign']['journal_head'],old_head)
        self.server.games.execute({'action':'load','room_id':self.room},str(uuid4()))
        state=self.state();self.assertEqual(state['campaign']['journal_head'],old_head)
        with self.server.service.store._connection() as c:found=campaign_storage.recall(c,state,'яшмовый дракон',self.players[0]['character_id'])
        self.assertFalse(any('яшмовый' in x['player'] for x in found))

    def test_large_remote_world_does_not_expand_prompt_or_schema(self):
        state=self.state();actor=self.actor();base=len(json.dumps(context(state,actor,'Иду к реке.'),ensure_ascii=False).encode())
        for i in range(1000):
            key=f'distant_{i}'
            state['world']['places'][key]={'name':f'Дальняя поляна {i}','description':'Далёкое описание. '*100,'parent':'old_ferry','exits':[{'to':'old_ferry'}]}
            state['world']['entities'][key+':npc']={'kind':'npc','name':f'Житель {i}','place':key,'knowledge':['Старая история. '*100]}
        data=context(state,actor,'Иду к реке.');size=len(json.dumps(data,ensure_ascii=False).encode())
        self.assertLess(size-base,2000);self.assertLess(size,48000)
        encoded=json.dumps(schema(state,'Иду к реке.'),ensure_ascii=False)
        self.assertNotIn('distant_',encoded)
        self.assertIn('riverbank',encoded)

    def test_exploration_respects_region_and_depth(self):
        state=self.state()
        with self.assertRaises(ValueError):campaign.local_site(state,'riverbank','Лодочная')
        state['location']='riverbank'
        parent='riverbank'
        for i in range(3):
            parent=campaign.local_site(state,parent,f'Участок {i}');state['location']=parent
        with self.assertRaises(ValueError):campaign.local_site(state,parent,'Новый материк')

    def test_transit_does_not_generate_scenes_until_a_stop_or_inspection(self):
        state=self.state()
        steps=[{'rule':'move','target':key} for key in ('riverbank','new_bridge','watchtower')]
        trial,_,needed=campaign.creation_plan(state,{'intent':'act','reply':'','steps':steps})
        self.assertEqual(needed,['watchtower'])
        self.assertTrue(trial['world']['places']['new_bridge']['unexplored'])
        inspected=[steps[0],{'rule':'look','target':'riverbank'},steps[1]]
        _,_,needed=campaign.creation_plan(state,{'intent':'act','reply':'','steps':inspected})
        self.assertEqual(needed,['riverbank','new_bridge'])

    def test_generator_can_only_follow_leads_destined_for_this_area(self):
        self.say(0,{'rule':'move','target':'riverbank'})
        self.say(0,{'rule':'look','target':'riverbank:feature:0'})
        state=self.state()
        incoming=next(iter(state['world']['leads'].values()))['destination']
        other=next(key for key,p in state['world']['places'].items() if p.get('campaign_anchor') and key!=incoming)
        payload=campaign.generation_payload(state,other,'Иду дальше.',self.server.ai.config)
        data=json.loads(payload['messages'][1]['content'])
        self.assertEqual(data['open_leads'],[])
        fields=payload['response_format']['json_schema']['schema']['properties']['discoveries']['items']['properties']
        self.assertEqual(fields['follow_up']['enum'],[''])
