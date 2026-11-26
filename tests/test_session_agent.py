"""Verified proposals, bounded repair, quotas and no effects before acceptance."""
from copy import deepcopy
import json
import unittest
from uuid import uuid4

import test_world_growth as fixture


class SessionAgentTests(unittest.TestCase):
    create=fixture.WorldGrowthTests.create
    request=fixture.WorldGrowthTests.request
    say=fixture.WorldGrowthTests.say
    roll=fixture.WorldGrowthTests.roll
    state=fixture.WorldGrowthTests.state
    edit=fixture.WorldGrowthTests.edit
    actor=fixture.WorldGrowthTests.actor

    def setUp(self):
        fixture.WorldGrowthTests.setUp(self)
        self.server.ai.config.update(session_agent=True,session_agent_repairs=1)
        self.records=[]
        self.plan_review=lambda data:[]
        self.prose_review=lambda data:[]
        self.plan_repair=None
        self.prose={'reply':'Мара отвечает на вопрос.','world_additions':[]}
        self.prose_repair=None
        self.on_review=None
        def complete(payload,timeout):
            self.records.append(deepcopy(payload))
            name=payload['response_format']['json_schema']['name']
            data=json.loads(payload['messages'][1]['content'])
            repaired=len(payload['messages'])>2
            if name=='session_verification':
                if self.on_review:self.on_review(data)
                issues=(self.plan_review if data['verification_stage']=='plan' else self.prose_review)(data)
                return {'content':json.dumps({'ok':not issues,'issues':issues})}
            if name=='action_plan':
                value=self.plan_repair if repaired and self.plan_repair is not None else self.proposal
            elif name=='scene_narration':
                value=self.prose_repair if repaired and self.prose_repair is not None else self.prose
            else:
                self.fail('Unexpected model boundary: '+name)
            return {'content':json.dumps(value,ensure_ascii=False)}
        self.server.ai.client.complete=complete

    def names(self):return [p['response_format']['json_schema']['name'] for p in self.records]

    def test_rejected_unrequested_purchase_is_repaired_before_any_effect(self):
        self.plan_review=lambda data:['Покупка не запрошена в player.'] if data['candidate']['steps'] else []
        self.plan_repair={'intent':'question','reply':'Здравствуйте.','steps':[],'voice':'npc','interlocutor':'mara'}
        status,result=self.say(0,{'rule':'buy','target':'travel_rope'},text='Мара, добрый день.')
        self.assertEqual(status,200,result)
        self.assertEqual(self.actor()['coins'],300)
        self.assertEqual(self.state()['world']['entities']['travel_rope']['owner'],'mara')
        self.assertEqual(self.names().count('action_plan'),2)
        repair=next(p for p in self.records if len(p['messages'])>2)
        self.assertIn('Покупка не запрошена',repair['messages'][-1]['content'])
        self.assertTrue(all(p['provider']['max_price']=={'prompt':0,'completion':0} for p in self.records))

    def test_unregistered_place_in_prose_is_repaired_into_a_persistent_seed(self):
        self.prose={'reply':'Там есть неизвестная мельница.','world_additions':[]}
        self.prose_review=lambda data:['Место из reply отсутствует в world_additions.'] if not data['candidate']['world_additions'] else []
        self.prose_repair={'reply':'Мара рассказывает о мельнице у протоки.','world_additions':[fixture.SITE]}
        status,result=self.say(0,text='Мара, что тут можно исследовать?',intent='question')
        self.assertEqual(status,200,result)
        self.assertEqual(len(self.state()['world']['story_hooks']),1)
        self.assertEqual(self.names().count('scene_narration'),2)
        self.assertNotIn('неизвестная мельница',json.dumps(self.state()['history'],ensure_ascii=False))
        traces=self.server.ai.traces(self.room)
        self.assertEqual(sum(t['role']=='session_verify_narration' for t in traces),2)

    def test_exhausted_plan_repairs_never_apply_purchase(self):
        self.plan_review=lambda data:['Покупка не запрошена.']
        status,_=self.say(0,{'rule':'buy','target':'travel_rope'},text='Я просто здороваюсь.')
        self.assertEqual(status,400)
        self.assertEqual(self.actor()['coins'],300)
        self.assertEqual(self.names(),['action_plan','session_verification','action_plan','session_verification'])
        self.assertFalse(self.state()['world'].get('story_hooks'))

    def test_invalid_narration_repairs_do_not_undo_a_verified_trade(self):
        self.prose={'reply':'У тебя 9999 медных монет.','world_additions':[]}
        self.prose_repair=deepcopy(self.prose)
        status,result=self.say(0,{'rule':'buy','target':'travel_rope'},text='Покупаю верёвку за сто медных.')
        self.assertEqual(status,200,result)
        self.assertEqual(self.actor()['coins'],200)
        self.assertIn('покупает',self.state()['history'][-1]['gm'])
        self.assertEqual(self.names().count('scene_narration'),2)
        self.assertNotIn('9999',json.dumps(self.state()['history'],ensure_ascii=False))

    def test_daily_quota_counts_verifier_and_stops_before_repair_or_effects(self):
        self.server.ai.config['daily_request_limit']=2
        self.plan_review=lambda data:['Исправь покупку.']
        status,_=self.say(0,{'rule':'buy','target':'travel_rope'},text='Здравствуйте.')
        self.assertEqual(status,400)
        self.assertEqual(self.server.ai.status()['requests_today'],2)
        self.assertEqual(self.names(),['action_plan','session_verification'])
        self.assertEqual(self.actor()['coins'],300)

    def test_verifier_transport_failure_is_not_retried(self):
        def outage(data):raise ValueError('Provider unavailable')
        self.on_review=outage
        status,_=self.say(0,{'rule':'buy','target':'travel_rope'})
        self.assertEqual(status,400)
        self.assertEqual(self.names(),['action_plan','session_verification'])
        self.assertEqual(self.actor()['coins'],300)

    def test_stale_rejected_candidate_cannot_run_a_repair_in_a_new_revision(self):
        self.on_review=lambda data:self.edit(lambda s:s.update(revision=s['revision']+1))
        self.plan_review=lambda data:['План неверен.']
        status,_=self.say(0,{'rule':'buy','target':'travel_rope'})
        self.assertEqual(status,409)
        self.assertEqual(self.names(),['action_plan','session_verification'])
        self.assertEqual(self.actor()['coins'],300)

    def test_gm_can_request_automatic_world_growth_without_a_speaking_npc(self):
        self.edit(lambda s:[e.update(place='clinic') for e in s['world']['entities'].values() if e['kind']=='npc'])
        self.proposal={'intent':'question','reply':'Осмотримся.','steps':[],'voice':'gm','world_request':True}
        self.prose={'reply':'У протоки видна старая мельница.','world_additions':[{**fixture.SITE,'speaker_id':''}]}
        state=self.state()
        status,result=self.request(self.root+'/session',{'revision':state['revision'],
            'character_id':self.players[0]['character_id'],'text':'Что необычного можно исследовать вокруг?'},token=self.tokens[0])
        self.assertEqual(status,200,result)
        hook=next(iter(self.state()['world']['story_hooks'].values()))
        self.assertEqual(hook['speaker_name'],'Ведущий')
        self.assertEqual(self.names().count('session_verification'),2)
        self.assertEqual(self.state()['history'][-1]['voice'],'gm')

    def test_empty_world_request_is_repaired_even_if_verifier_would_approve_it(self):
        self.proposal={'intent':'question','reply':'Спросим о новом месте.','steps':[],
                       'voice':'npc','interlocutor':'mara','world_request':True}
        self.prose={'reply':'Не знаю, сходи поищи потом.','world_additions':[]}
        self.prose_repair={'reply':'Мара рассказывает о мельнице у протоки.','world_additions':[fixture.SITE]}
        state=self.state()
        status,result=self.request(self.root+'/session',{'revision':state['revision'],
            'character_id':self.players[0]['character_id'],'text':'Мара, что нового можно исследовать?'},token=self.tokens[0])
        self.assertEqual(status,200,result)
        self.assertEqual(len(self.state()['world']['story_hooks']),1)
        self.assertEqual(self.names(),['action_plan','session_verification','scene_narration','scene_narration','session_verification'])
        self.assertNotIn('Не знаю',json.dumps(self.state()['history'],ensure_ascii=False))

    def test_npc_draft_is_not_published_before_or_after_narration_review(self):
        self.proposal={'intent':'question','reply':'НЕПРОВЕРЕННЫЙ ЧЕРНОВИК: здесь выдают золото.',
                       'steps':[],'voice':'npc','interlocutor':'mara'}
        def inspect(data):
            if data['verification_stage']=='plan':self.assertEqual(data['candidate']['reply'],'')
            messages=self.server.service.store.page(self.room)['messages']
            self.assertFalse(any('НЕПРОВЕРЕННЫЙ' in m.get('text','') for m in messages))
        self.on_review=inspect
        state=self.state()
        status,result=self.request(self.root+'/session',{'revision':state['revision'],
            'character_id':self.players[0]['character_id'],'text':'Мара, здравствуйте.'},token=self.tokens[0])
        self.assertEqual(status,200,result)
        self.assertNotIn('НЕПРОВЕРЕННЫЙ',json.dumps(self.state()['history'],ensure_ascii=False))
        self.assertEqual(self.state()['history'][-1]['gm'],self.prose['reply'])

    def test_scene_is_repaired_and_verified_before_entering_canon(self):
        original=self.server.ai.client.complete
        scene={'description':'С высокого берега видны паром и дорога к мосту.','npcs':[],
               'features':[{'name':'Старый столб','description':'На нём отметка перевозчика.','kind':'landmark'}],
               'discoveries':[{'source':0,'trigger':'look','observation':'На столбе указана стоянка у парома.',
                               'destination':'old_ferry','question':'Кто ждал груз у парома?','follow_up':''}]}
        def complete(payload,timeout):
            if payload['response_format']['json_schema']['name']=='campaign_scene':
                self.records.append(deepcopy(payload))
                value=scene if len(payload['messages'])>2 else {**scene,'coins':100000}
                return {'content':json.dumps(value,ensure_ascii=False)}
            return original(payload,timeout)
        self.server.ai.client.complete=complete
        self.prose={'reply':'На берегу шумят ивы.','world_additions':[]}
        status,result=self.say(0,{'rule':'move','target':'riverbank'},text='Иду на высокий берег.')
        self.assertEqual(status,200,result)
        self.assertEqual(self.state()['location'],'riverbank')
        self.assertEqual(self.actor()['coins'],300)
        self.assertEqual(self.names().count('campaign_scene'),2)
        traces=self.server.ai.traces(self.room)
        self.assertEqual(sum(t['role']=='session_repair_scene' for t in traces),1)
        self.assertEqual(sum(t['role']=='session_verify_scene' for t in traces),1)

    def test_rejected_feedback_stays_private_instead_of_spoiling_the_game(self):
        secret='СКРЫТАЯ ПРИЧИНА: заказчик сам спрятал груз.'
        self.plan_review=lambda data:[secret]
        status,result=self.say(0,text='Здравствуйте.',intent='question')
        self.assertEqual(status,400)
        self.assertNotIn(secret,json.dumps(result,ensure_ascii=False))
        with self.server.service.store._connection() as c:
            trace=json.loads(c.execute("SELECT body FROM ai_requests WHERE json_extract(body,'$.role')='session_gm' ORDER BY started DESC LIMIT 1").fetchone()[0])
        self.assertEqual(trace['verification_issues'],[secret])

    def test_new_evening_outline_repairs_all_conflicting_names_before_creation(self):
        import test_evening
        game=self.server.games.execute({'action':'create','campaign':'solo-evening','hero_id':self.heroes[0]['id']},str(uuid4()))
        room=game['room_id'];before=self.server.adventure.read(room)
        actor=next(a for a in self.server.roster.read(room)['characters'] if a['id']==before['character_id'])
        valid=test_evening.outline(987)
        invalid=deepcopy(valid);invalid['hub']=next(iter(before['world']['places'].values()))['name']
        invalid['patron']['name']=actor['name']
        original=self.server.ai.client.complete
        def complete(payload,timeout):
            if payload['response_format']['json_schema']['name']=='evening_outline':
                self.records.append(deepcopy(payload))
                return {'content':json.dumps(valid if len(payload['messages'])>2 else invalid,ensure_ascii=False)}
            return original(payload,timeout)
        self.server.ai.client.complete=complete
        result=self.server.adventure.adventure_control(room,{'action':'start','revision':before['revision'],
                                                            'character_id':actor['id']},str(uuid4()))
        self.assertEqual(result['world']['adventure']['title'],valid['title'])
        self.assertEqual(result['character_id'],actor['id'])
        self.assertTrue(set(before['world']['places'])<=set(result['world']['places']))
        repair=next(p for p in self.records if len(p['messages'])>2)
        self.assertIn('patron.name',repair['messages'][-1]['content'])
        self.assertIn('hub=',repair['messages'][-1]['content'])
        self.assertEqual(self.names(),['evening_outline','evening_outline','session_verification'])
