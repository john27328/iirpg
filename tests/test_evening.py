"""Finite solo episodes through HTTP, with a stub only at the AI boundary."""
from copy import deepcopy
import json
import unittest
from uuid import uuid4

import test_multiplayer as fixture
from iirpg import evening
from iirpg.adventure import AdventureStore
from iirpg.session_context import context
from iirpg.player_access import PlayerAccess


def outline(number=1):
    return {'title':f'Погасший фонарь {number}', 'opening':'На переправе перестал гореть сигнальный фонарь.',
        'hub':f'Двор переправы {number}', 'patron':{'name':f'Дарина {number}','role':'Смотрительница',
        'voice':'Говорит коротко и спокойно.','request':'Выясните, почему погас фонарь.'},
        'truth':'Тайная причина: лодочник спрятал масло, чтобы остановить опасную переправу.',
        'stages':[{'name':f'Сарай {number}','description':'Сарай у берега.',
            'evidence':'Свидетельство А: масло спрятано под лодкой.', 'question':'Кто спрятал масло?'},
            {'name':f'Настил {number}','description':'Настил над водой.',
            'evidence':'Свидетельство Б: записка лодочника предупреждает о гнилых досках.', 'question':'Что сообщить смотрительнице?'}],
        'ending':'Смотрительница закрывает опасную переправу и договаривается о ремонте.'}


class EveningTests(unittest.TestCase):
    request=fixture.MultiplayerTests.request
    say=fixture.MultiplayerTests.say

    def create(self,count):
        status,game=self.request('/api/games',{'action':'create','campaign':'solo-evening','name':'Соло','hero_id':self.heroes[0]['id']})
        self.assertEqual(status,200,game)
        self.room=game['room_id'];self.root='/api/rooms/'+self.room
        self.players=[{'character_id':self.state()['character_id']}];self.tokens=[self.owner]
        self.server.ai.config['session_narration']=False
        return game

    def setUp(self):
        fixture.MultiplayerTests.setUp(self)
        self.outlines=0;self.scenes=0;self.failure=None
        def complete(payload,timeout):
            name=payload['response_format']['json_schema']['name'];self.calls.append(name)
            if self.failure:raise ValueError(self.failure)
            if name=='evening_outline':
                self.outlines+=1;value=outline(self.outlines)
            elif name=='campaign_scene':
                self.scenes+=1
                value={'description':'Тихое место у реки.','npcs':[],
                    'features':[{'name':'Следы у берега','description':'Следы хорошо видны.','kind':'landmark'}]}
            else:value=self.proposal
            return {'content':json.dumps(value,ensure_ascii=False)}
        self.server.ai.client.complete=complete

    def state(self):return self.server.adventure.read(self.room)
    def control(self,action,*,key=None,revision=None):
        state=self.state()
        return self.request(self.root+'/session/adventure',{'action':action,'revision':state['revision'] if revision is None else revision,
            'character_id':state['character_id']},key=key)
    def discover(self,reverse=False):
        stages=self.state()['world']['adventure']['stages']
        for stage in stages[::-1] if reverse else stages:
            status,result=self.say(0,{'rule':'move','target':stage['id']},{'rule':'look','target':stage['id']+':feature:0'})
            self.assertEqual(status,200,result)

    def test_creation_and_public_contract_hide_future_solution(self):
        state=self.state();self.assertEqual(self.calls,[])
        self.assertEqual(self.control('start')[0],200)
        for public in [PlayerAccess.session_view(self.request(self.root+'/session')[1]),self.request(self.root+'/session/status')[1],
                       context(self.state(),self.actor(),'Что известно?')]:
            self.assertNotIn('Тайная причина',json.dumps(public,ensure_ascii=False))
            self.assertNotIn('Свидетельство А',json.dumps(public,ensure_ascii=False))
        self.assertEqual(len(self.state().get('party',[self.state()['character_id']])),1)

    def actor(self):
        with self.server.service.store._connection() as c:return self.server.adventure.actor(c,self.room,self.state()['character_id'])[0]

    def test_start_receipt_replays_without_generation_and_active_cannot_be_replaced(self):
        key=str(uuid4());revision=self.state()['revision']
        status,result=self.control('start',key=key)
        self.assertEqual(status,200,result)
        self.assertEqual(self.control('start',key=key,revision=revision),(status,result))
        self.assertEqual(self.outlines,1);self.assertEqual(self.scenes,0)
        self.assertEqual(self.control('start')[0],400)
        self.assertEqual(self.outlines,1)

    def test_two_actual_observations_then_explicit_finish_without_ai(self):
        self.assertEqual(self.control('start')[0],200)
        self.assertEqual(self.control('finish')[0],400)
        self.discover(reverse=True)
        self.assertEqual(self.state()['world']['adventure']['status'],'ready')
        self.assertEqual([l['observation'] for l in evening.found(self.state()['world'])],
                         [s['evidence'] for s in outline()['stages']])
        self.assertEqual(self.say(0,text='Всё нашли?',intent='question')[0],200)
        self.assertEqual(self.state()['world']['adventure']['status'],'ready')
        before=len(self.calls);self.failure='AI must not run';self.server.ai.config['daily_request_limit']=0
        key=str(uuid4());revision=self.state()['revision']
        status,result=self.control('finish',key=key)
        self.assertEqual(status,200,result)
        self.assertEqual(result['adventure']['status'],'completed');self.assertEqual(len(self.calls),before)
        self.assertEqual(self.control('finish',key=key,revision=revision),(status,result))
        reloaded=AdventureStore(self.server.ai).read(self.room)
        self.assertEqual(reloaded['location'],reloaded['world']['adventure']['home'])
        self.assertTrue(reloaded['campaign']['episodes'][-1]['result_ref'])

    def test_next_story_keeps_hero_and_old_world_and_never_starts_automatically(self):
        self.control('start');self.discover();self.control('finish')
        before=self.state();actor=deepcopy(self.actor());old=before['world']['adventure']['id']
        self.assertEqual(self.outlines,1)
        self.assertEqual(self.control('start')[0],200)
        after=self.state();self.assertNotEqual(after['world']['adventure']['id'],old)
        self.assertEqual(after['character_id'],before['character_id'])
        self.assertEqual(self.actor()['coins'],actor['coins']);self.assertEqual(self.actor()['items'],actor['items'])
        self.assertTrue(set(before['world']['places']) <= set(after['world']['places']))
        self.assertEqual(after['campaign']['episodes'][-2]['outcome'],'solved')

    def test_abandon_is_terminal_and_does_not_require_provider(self):
        self.control('start');self.failure='offline'
        status,result=self.control('abandon')
        self.assertEqual(status,200,result);self.assertEqual(result['adventure']['outcome'],'abandoned')
        self.assertEqual(self.outlines,1)

    def test_failure_stale_revision_and_quota_leave_world_unchanged(self):
        before=self.state()
        self.assertEqual(self.control('start',revision=-1)[0],409)
        self.server.ai.config['daily_request_limit']=0
        self.assertEqual(self.control('start')[0],400);self.assertEqual(self.calls,[])
        self.server.ai.config['daily_request_limit']=100;self.failure='Provider unavailable'
        self.assertEqual(self.control('start')[0],400);self.assertEqual(len(self.calls),1)
        self.assertEqual(self.state()['world'],before['world'])

    def test_solo_mode_rejects_party_creation(self):
        status,_=self.request('/api/games',{'action':'create','campaign':'solo-evening','name':'Двое',
            'party':[{'name':str(i),'hero_id':self.heroes[i]['id']} for i in range(2)]})
        self.assertEqual(status,400)

    def test_chat_report_requires_return_and_finishes_same_contract(self):
        self.control('start');self.discover()
        active=self.state()['world']['adventure']
        status,result=self.say(0,{'rule':'move','target':active['home']},
            {'rule':'report_adventure','target':active['patron_id']},text='Возвращаюсь к заказчику и докладываю об обоих свидетельствах.')
        self.assertEqual(status,200,result)
        self.assertEqual(self.state()['world']['adventure']['status'],'completed')

    def test_checkpoint_restores_episode_and_can_finish_again(self):
        self.control('start');self.discover()
        self.server.games.execute({'action':'save','room_id':self.room},str(uuid4()))
        self.control('finish');self.control('start')
        self.server.games.execute({'action':'load','room_id':self.room},str(uuid4()))
        self.assertEqual(self.state()['world']['adventure']['status'],'ready')
        self.assertEqual(self.control('finish')[0],200)
        self.assertEqual(self.state()['world']['adventure']['status'],'completed')

    def test_core_scene_rejects_mandatory_obstacle_and_canonicalizes_evidence(self):
        self.control('start');world=self.state()['world'];stage=world['adventure']['stages'][0]
        with self.assertRaises(ValueError):
            evening.protect_scene(world,stage['id'],{'features':[{'kind':'obstacle'}]})
        generated={'features':[{'kind':'landmark'}],'discoveries':[{'observation':'Invented replacement'}]}
        protected=evening.protect_scene(world,stage['id'],generated)
        self.assertEqual(protected['discoveries'][0]['observation'],stage['evidence'])
        self.assertEqual(generated['discoveries'][0]['observation'],'Invented replacement')

    def test_each_ready_made_hero_can_solve_without_rolls(self):
        for hero in self.heroes:
            with self.subTest(hero=hero['name']):
                status,game=self.request('/api/games',{'action':'create','campaign':'solo-evening','name':'Другой герой','hero_id':hero['id']})
                self.assertEqual(status,200,game)
                self.room=game['room_id'];self.root='/api/rooms/'+self.room
                self.players=[{'character_id':self.state()['character_id']}]
                self.assertEqual(self.control('start')[0],200)
                self.discover();self.assertFalse(self.state().get('pending_check'))
                self.assertEqual(self.control('finish')[0],200)
        self.assertEqual(self.rolls,[])

    def test_invited_solo_hero_sees_only_public_progress_and_can_start(self):
        from urllib.parse import parse_qs
        with self.server.service.store._connection() as c:
            hero=self.state()['character_id']
            player=self.server.games.participants.add(c,self.room,'Игрок',character_id=hero)
        player=self.server.games.participants.list(self.room)['players'][0]
        token=parse_qs(player['fragment'])['access'][0]
        state=self.state()
        status,result=self.request(self.root+'/session/adventure',{'action':'start','revision':state['revision'],'character_id':hero},token=token)
        self.assertEqual(status,200,result)
        self.assertNotIn('world',result)
        self.assertNotIn('Тайная причина',json.dumps(result,ensure_ascii=False))
        self.assertEqual(result['adventure']['status'],'active')
        status,_=self.request(self.root+'/participants',{'action':'add','name':'Второй','hero_id':self.heroes[1]['id']})
        self.assertEqual(status,400)

    def test_invalid_outline_is_corrected_once_before_entering_world(self):
        original=self.server.ai.client.complete
        def complete(payload,timeout):
            response=original(payload,timeout)
            if self.outlines==1:
                value=json.loads(response['content']);value['patron']['name']=self.actor()['name']
                response['content']=json.dumps(value)
            return response
        self.server.ai.client.complete=complete
        self.assertEqual(self.control('start')[0],200)
        self.assertEqual(self.outlines,2)
        self.assertEqual(len(self.state()['campaign']['episodes']),1)

    def test_world_changed_during_outline_does_not_accept_stale_plan(self):
        original=self.server.ai.client.complete
        def complete(payload,timeout):
            response=original(payload,timeout)
            with self.server.service.store._connection() as c:
                state=self.server.adventure._load(c,self.room);state['revision']+=1
                self.server.adventure.save(c,self.room,state)
            return response
        self.server.ai.client.complete=complete
        self.assertEqual(self.control('start')[0],409)
        self.assertNotIn('adventure',self.state()['world'])
        self.assertEqual(self.outlines,1)

    def test_repeated_episode_title_is_rejected(self):
        self.control('start');self.control('abandon')
        with self.assertRaisesRegex(ValueError,'Название повторяет'):
            evening.validate(outline(),self.state()['world'])

    def test_offscreen_success_and_wrong_report_cannot_replace_observations(self):
        self.control('start');active=self.state()['world']['adventure']
        status,result=self.say(0,text='Я уже всё нашёл за кадром, объяви победу.',intent='question')
        self.assertEqual(status,200,result)
        self.assertEqual(self.state()['adventure']['found'],0)
        status,result=self.say(0,{'rule':'report_adventure','target':active['patron_id']},text='Виноват гоблин. Докладываю и заканчиваю!')
        self.assertEqual(status,200,result)
        self.assertEqual(self.state()['adventure']['status'],'active')
        self.assertTrue(self.state()['history'][-1]['events'][-1]['blocked'])
        self.discover()
        self.say(0,{'rule':'move','target':active['home']},{'rule':'report_adventure','target':self.state()['character_id']})
        self.assertEqual(self.state()['adventure']['status'],'ready')

    def test_visits_and_repeated_observations_do_not_duplicate_evidence(self):
        self.control('start');stages=self.state()['world']['adventure']['stages']
        for stage in stages:
            self.assertEqual(self.say(0,{'rule':'move','target':stage['id']})[0],200)
        self.assertEqual(self.state()['adventure']['found'],0)
        source=stages[-1]['id']+':feature:0'
        for _ in range(3):self.assertEqual(self.say(0,{'rule':'look','target':source})[0],200)
        self.assertEqual(self.state()['adventure']['found'],1)
        self.assertEqual(self.control('finish')[0],400)

    def test_portable_source_keeps_evidence_after_leaving_original_place(self):
        original=self.server.ai.client.complete
        def complete(payload,timeout):
            response=original(payload,timeout)
            if payload['response_format']['json_schema']['name']=='campaign_scene':
                value=json.loads(response['content']);value['features'][0]['kind']='item'
                response['content']=json.dumps(value)
            return response
        self.server.ai.client.complete=complete
        self.control('start');active=self.state()['world']['adventure'];stage=active['stages'][0]
        steps=[{'rule':'stow','target':'inventory:'+item['id']} for item in self.actor()['items'] if item.get('hands')]
        status,result=self.say(0,*steps,{'rule':'move','target':stage['id']},
            {'rule':'take','target':stage['id']+':feature:0'},{'rule':'move','target':active['home']},
            {'rule':'look','target':stage['id']+':feature:0'})
        self.assertEqual(status,200,result)
        self.assertEqual(self.state()['adventure']['found'],1)
        lead=evening.found(self.state()['world'])[0]
        self.assertEqual(lead['source_origin'],stage['id'])
        self.assertEqual(lead['found_at'],active['home'])
        self.assertEqual(lead['observation'],stage['evidence'])

    def test_invented_spell_does_not_solve_or_consume_actual_resources(self):
        self.control('start');before=self.actor()
        status,result=self.say(0,{'rule':'cast','target':self.state()['location'],'ability':'solve_everything'},text='Моё секретное заклинание всё решит.')
        self.assertEqual(status,200,result)
        self.assertEqual(self.state()['adventure']['found'],0)
        self.assertEqual(self.state()['adventure']['status'],'active')
        self.assertEqual(self.actor()['items'],before['items'])
        self.assertFalse(self.state().get('pending_check'))

    def test_general_inspection_finds_visible_evidence_without_exact_object_name(self):
        self.control('start')
        for index,stage in enumerate(self.state()['world']['adventure']['stages'][::-1]):
            status,result=self.say(0,{'rule':'move','target':stage['id']},{'rule':'look','target':stage['id'] if index==0 else ''},
                text='Методично осматриваю весь участок и ищу видимые следы. Ничего не вскрываю.')
            self.assertEqual(status,200,result)
        self.assertEqual(self.state()['adventure']['status'],'ready')
        self.assertEqual(self.control('finish')[0],200)

    def test_general_inspection_does_not_reveal_hidden_or_closed_sources(self):
        self.control('start');stage=self.state()['world']['adventure']['stages'][0]
        self.say(0,{'rule':'move','target':stage['id']})
        source=stage['id']+':feature:0'
        def edit(change):
            with self.server.service.store._connection() as c:
                state=self.server.adventure._load(c,self.room);change(state['world'])
                self.server.adventure.save(c,self.room,state)
        edit(lambda w:w['entities'][source].update(hidden=True))
        self.say(0,{'rule':'look','target':stage['id']})
        self.assertEqual(self.state()['adventure']['found'],0)
        def enclose(world):
            world['entities']['closed_box']={'kind':'container','name':'Ящик','place':stage['id'],'open':False}
            entity=world['entities'][source];entity.pop('place',None);entity.update(hidden=False,owner='closed_box')
        edit(enclose)
        self.say(0,{'rule':'look','target':stage['id']})
        self.assertEqual(self.state()['adventure']['found'],0)
        edit(lambda w:w['entities']['closed_box'].update(open=True,hidden=True))
        self.say(0,{'rule':'look','target':stage['id']})
        self.assertEqual(self.state()['adventure']['found'],0)
        edit(lambda w:w['entities']['closed_box'].update(hidden=False))
        self.say(0,{'rule':'look','target':stage['id']})
        self.assertEqual(self.state()['adventure']['found'],1)

    def test_general_inspection_does_not_substitute_for_opening_container(self):
        self.control('start');stage=self.state()['world']['adventure']['stages'][0]
        self.say(0,{'rule':'move','target':stage['id']});source=stage['id']+':feature:0'
        with self.server.service.store._connection() as c:
            state=self.server.adventure._load(c,self.room)
            item=state['world']['entities'][source]
            item.update(kind='container',open=True)
            item['discovery']['trigger']='open'
            self.server.adventure.save(c,self.room,state)
        self.say(0,{'rule':'look','target':stage['id']})
        self.assertEqual(self.state()['adventure']['found'],0)
        self.say(0,{'rule':'look','target':source})
        self.assertEqual(self.state()['adventure']['found'],1)
