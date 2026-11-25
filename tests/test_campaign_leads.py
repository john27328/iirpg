"""Discoveries persist their evidence and author; prose never resolves a case."""
from copy import deepcopy
import json
import unittest
from uuid import uuid4

import test_campaign as fixture
from iirpg import campaign, campaign_leads
from iirpg.session_context import context
from iirpg.story_narration import render_references

SECRET = 'На обороте: отправка №17 принята у старого парома.'


def observation(destination='old_ferry', follow_up='', trigger='look'):
    return {'source':0,'trigger':trigger,'observation':SECRET,'destination':destination,
            'question':'Кто принял отправку №17?','follow_up':follow_up}


class CampaignLeadTests(unittest.TestCase):
    create=fixture.CampaignTests.create
    request=fixture.CampaignTests.request
    say=fixture.CampaignTests.say
    roll=fixture.CampaignTests.roll
    state=fixture.CampaignTests.state
    edit=fixture.CampaignTests.edit
    actor=fixture.CampaignTests.actor

    def setUp(self):
        fixture.CampaignTests.setUp(self)
        self.closed=False
        def scene():
            data=self.calls[-1]
            value=deepcopy(fixture.SCENE)
            lead=observation()
            if data['seed']['name']=='Старый паром':
                parent=next(x for x in data['open_leads'] if x['destination']=='old_ferry')
                lead=observation('quarry',parent['id'])
                lead.update(observation='В журнале: часть отправки вернули в каменоломни.',question='Почему груз вернули в каменоломни?')
            if self.closed:
                value['features'][0]={'name':'Запертый ящик','kind':'obstacle','description':'Закрытый ящик без надписей.'}
                lead['trigger']='open'
            value['discoveries']=[lead]
            self.generated_value=value
        self.on_generation=scene

    def bank(self):
        self.assertEqual(self.say(0,{'rule':'move','target':'riverbank'})[0],200)
        return self.state()

    def read(self,player=0,where='riverbank'):
        self.assertEqual(self.say(player,{'rule':'look','target':where+':feature:0'},text='Изучаю запись на этом предмете.')[0],200)
        return self.state()

    def test_observation_is_hidden_from_context_until_examined(self):
        state=self.bank()
        self.assertFalse(state['world'].get('leads'))
        supplied=context(state,self.actor(),'Что здесь видно?')
        self.assertNotIn(SECRET,json.dumps(supplied,ensure_ascii=False))
        self.assertEqual(supplied['investigation']['unexamined'][0]['name'],'Записка перевозчика')
        self.assertEqual(supplied['investigation']['leads'],[])
        state=self.read()
        lead=state['world']['leads']['riverbank:lead:0']
        self.assertEqual(lead['observation'],SECRET)
        self.assertEqual(lead['source_origin'],'riverbank')
        self.assertEqual(lead['discovered_by'],self.players[0]['character_id'])
        self.assertIn(SECRET,state['history'][-1]['gm'])
        self.assertEqual(lead['status'],'open')
        public=context(state,self.actor(),'Кто нашёл запись?')['investigation']['leads'][0]
        self.assertEqual(public['found_by'],self.players[0]['character_name'])
        self.assertEqual(public['source_place'],'Высокий берег реки')
        self.assertNotIn('riverbank:lead',json.dumps(public,ensure_ascii=False))

    def test_repeat_read_preserves_original_author_and_current_progress(self):
        self.bank();state=self.read();first=deepcopy(state['world']['leads'])
        state=self.read(1)
        self.assertEqual(state['world']['leads'],first)
        self.assertIn(SECRET,state['history'][-1]['gm'])
        self.assertEqual(self.generated,1)

    def test_destination_generation_continues_known_lead_and_examination_advances_it(self):
        self.bank();state=self.read()
        original=deepcopy(state['world']['leads']['riverbank:lead:0'])
        self.say(1,{'rule':'move','target':'old_ferry'})
        state=self.state()
        self.assertEqual(state['world']['leads']['riverbank:lead:0']['status'],'visited')
        request=next(c for c in reversed(self.calls) if 'open_leads' in c)
        self.assertEqual(request['open_leads'][0]['id'],'riverbank:lead:0')
        self.assertEqual(request['open_leads'][0]['observation'],SECRET)
        state=self.read(1,'old_ferry')
        parent=state['world']['leads']['riverbank:lead:0'];child=state['world']['leads']['old_ferry:lead:0']
        self.assertEqual(parent['status'],'followed')
        self.assertEqual(parent['continuations'],[child['id']])
        self.assertEqual(parent['observation'],original['observation'])
        self.assertEqual(child['follow_up'],parent['id'])
        self.assertEqual(child['discovered_by'],self.players[1]['character_id'])
        self.assertEqual(child['destination'],'quarry')
        self.assertFalse(any(q.get('completed') for q in state['world']['quests'].values()))

    def test_locked_source_is_not_revealed_by_looking_or_a_failed_roll(self):
        self.closed=True;self.bank();state=self.read()
        self.assertFalse(state['world'].get('leads'))
        self.assertNotIn(SECRET,state['history'][-1]['gm'])
        self.say(0,{'rule':'force_open','target':'riverbank:feature:0'})
        self.assertFalse(self.state()['world'].get('leads'))
        self.server.service.dice.roll_die=lambda sides:1
        self.roll(0,self.state()['pending_check'])
        self.assertFalse(self.state()['world'].get('leads'))
        self.server.service.dice.roll_die=lambda sides:min(sides,20)
        self.say(0,{'rule':'force_open','target':'riverbank:feature:0'})
        self.roll(0,self.state()['pending_check'])
        self.assertIn('riverbank:lead:0',self.state()['world']['leads'])
        first=deepcopy(self.state()['world']['leads']['riverbank:lead:0'])
        self.edit(lambda s:s['world']['entities'].update({
            'inside':{'kind':'item','name':'Бирка перевозчика','owner':'riverbank:feature:0','item':{'name':'Бирка перевозчика','category':'gear','bulk':'L'}},
            'concealed':{'kind':'item','name':'Скрытый вкладыш','owner':'riverbank:feature:0','hidden':True,'item':{'name':'Скрытый вкладыш','category':'gear','bulk':'L'}}}))
        state=self.read(1)
        text=state['history'][-1]['gm']
        self.assertIn('открыто',text)
        self.assertIn('Бирка перевозчика',text)
        self.assertNotIn('Скрытый вкладыш',text)
        self.assertNotIn('Закрытый ящик без надписей',text)
        self.assertIn(SECRET,text)
        self.assertEqual(state['world']['leads']['riverbank:lead:0'],first)

    def test_reading_carried_evidence_preserves_its_origin(self):
        self.bank();self.read();self.say(1,{'rule':'move','target':'old_ferry'})
        self.say(1,{'rule':'take','target':'old_ferry:feature:0'})
        self.say(1,{'rule':'move','target':'market'})
        state=self.read(1,'old_ferry')
        child=state['world']['leads']['old_ferry:lead:0']
        self.assertEqual(child['found_at'],'market')
        self.assertEqual(child['source_origin'],'old_ferry')
        self.assertEqual(state['world']['leads']['riverbank:lead:0']['status'],'followed')

    def test_invalid_sources_and_fabricated_followups_are_rejected(self):
        state=self.state()
        for change in ({'source':True},{'source':4},{'destination':'unknown'},{'destination':'riverbank'},
                       {'follow_up':'invented:lead'},{'trigger':'open'}):
            with self.subTest(change=change):
                value=deepcopy(fixture.SCENE);value['discoveries']=[{**observation(),**change}]
                with self.assertRaises(ValueError):campaign.scene(state['world'],'riverbank',value)
        value=deepcopy(fixture.SCENE);value['discoveries']=[observation(),observation()]
        with self.assertRaises(ValueError):campaign.scene(state['world'],'riverbank',value)

    def test_destination_cannot_ignore_the_reason_players_came(self):
        self.bank();self.read()
        value=deepcopy(fixture.SCENE);value['npcs']=[]
        value['discoveries']=[observation('quarry')]
        with self.assertRaisesRegex(ValueError,'продолжить'):
            campaign.scene(self.state()['world'],'old_ferry',value)

    def test_checkpoint_restores_undiscovered_evidence_without_future_authors(self):
        self.bank();self.server.games.execute({'action':'save','room_id':self.room},str(uuid4()))
        self.read(1);self.say(0,{'rule':'move','target':'old_ferry'});self.read(0,'old_ferry')
        self.server.games.execute({'action':'load','room_id':self.room},str(uuid4()))
        state=self.state()
        self.assertFalse(state['world'].get('leads'))
        self.assertNotIn('old_ferry:feature:0',state['world']['entities'])
        state=self.read(0)
        self.assertEqual(state['world']['leads']['riverbank:lead:0']['discovered_by'],self.players[0]['character_id'])
        self.assertEqual(state['world']['leads']['riverbank:lead:0']['status'],'open')

    def test_return_lead_can_continue_in_new_surroundings_without_rewriting_old_place(self):
        original=self.on_generation
        def candidates():
            original()
            data=self.calls[-1]
            row=self.generated_value['discoveries'][0]
            if data['seed']['name']=='Старый паром':
                row.update(destination='riverbank',question='Что означает знак у берега?')
            elif data['seed'].get('parent')=='riverbank':
                incoming=next(x for x in data['open_leads'] if x['destination']=='riverbank' and x['status']!='followed')
                row.update(destination='quarry',follow_up=incoming['id'])
        self.on_generation=candidates
        self.bank();self.read();original_description=self.state()['world']['places']['riverbank']['description']
        self.say(1,{'rule':'move','target':'old_ferry'});self.read(1,'old_ferry')
        self.say(0,{'rule':'move','target':'riverbank'},{'rule':'explore','target':'riverbank','method':'Следы у старой тропы'})
        local=self.state()['location'];self.assertTrue(local.startswith('site_'))
        state=self.read(0,local)
        self.assertEqual(state['world']['places']['riverbank']['description'],original_description)
        self.assertEqual(state['world']['leads']['old_ferry:lead:0']['status'],'followed')
        self.assertEqual(state['world']['leads'][local+':lead:0']['follow_up'],'old_ferry:lead:0')
        self.assertEqual(self.generated,3)

    def test_many_leads_keep_generation_and_play_context_bounded(self):
        state=self.bank();self.read()
        state=self.state();original=state['world']['leads']['riverbank:lead:0']
        for i in range(1000):
            row={**deepcopy(original),'id':f'other:{i}','observation':f'Запись другого путешествия {i}.','status':'followed'}
            state['world']['leads'][row['id']]=row
        supplied=context(state,self.actor(),'Куда ведёт запись?')
        self.assertLessEqual(len(supplied['investigation']['leads']),6)
        self.assertLess(len(json.dumps(supplied,ensure_ascii=False).encode()),48000)
        generated=campaign.generation_payload(state,'old_ferry','Иду по найденному следу.',self.server.ai.config)
        data=json.loads(generated['messages'][1]['content'])
        self.assertEqual(data['open_leads'][0]['id'],'riverbank:lead:0')
        self.assertLessEqual(len(data['open_leads']),6)
        self.assertLess(len(json.dumps(data,ensure_ascii=False).encode()),20000)

    def test_references_render_as_names_without_changing_evidence_ids(self):
        self.bank();state=self.read()
        text='След riverbank:lead:0: `riverbank:feature:0`, затем old_ferry. Код К-7. Не old_ferry_extra и не old_ferry:unknown.'
        before=deepcopy(state['world'])
        result=render_references(text,state['world'])
        self.assertNotIn('riverbank:',result)
        self.assertIn('Записка перевозчика',result)
        self.assertIn('Старый паром',result)
        self.assertIn('Код К-7',result)
        self.assertIn('old_ferry_extra',result)
        self.assertIn('old_ferry:unknown',result)
        self.assertEqual(state['world'],before)

    def test_both_planner_and_narration_publish_names_for_internal_references(self):
        self.bank();self.read()
        self.proposal={'intent':'question','voice':'gm','reply':'Запись riverbank:lead:0 ведёт к old_ferry.','steps':[]}
        command={'revision':self.state()['revision'],'character_id':self.players[0]['character_id'],'text':'Куда ведёт запись?'}
        key=str(uuid4())
        status,response=self.request(self.root+'/session',command,key=key)
        self.assertEqual(status,200)
        self.assertIn('Записка перевозчика',response['history'][-1]['gm'])
        self.assertNotIn('riverbank:',response['history'][-1]['gm'])
        self.assertEqual(self.request(self.root+'/session',command,key=key)[1],response)
        original=self.server.ai.client.complete
        def complete(payload,timeout):
            if payload['response_format']['json_schema']['name']=='scene_narration':
                return {'content':json.dumps({'reply':'Осмотрите riverbank:feature:0, прежде чем идти к old_ferry.'})}
            return original(payload,timeout)
        self.server.ai.client.complete=complete
        self.server.ai.config['session_narration']=True
        self.say(1,text='Что осмотреть дальше?',intent='question')
        text=self.state()['history'][-1]['gm']
        self.assertIn('Записка перевозчика',text)
        self.assertIn('Старый паром',text)
        self.assertNotIn('riverbank:',text)
