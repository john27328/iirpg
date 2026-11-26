"""Sourced improvisation becomes lazy, persistent world state; no external AI."""
from copy import deepcopy
import json
import unittest
from uuid import uuid4

import test_campaign as campaign_fixture
from iirpg import world_growth
from iirpg.adventure import AdventureStore
from iirpg.session_context import context


SITE = {'name':'Мельница у протоки', 'premise':'Небольшая старая мельница у рыночной протоки.',
        'rumor':'Мара слышала, что по вечерам возле мельницы разгружают мешки.',
        'question':'Что за мешки разгружают у мельницы?', 'speaker_id':'mara'}


class WorldGrowthTests(unittest.TestCase):
    create = campaign_fixture.CampaignTests.create
    request = campaign_fixture.CampaignTests.request
    say = campaign_fixture.CampaignTests.say
    roll = campaign_fixture.CampaignTests.roll
    state = campaign_fixture.CampaignTests.state
    edit = campaign_fixture.CampaignTests.edit
    actor = campaign_fixture.CampaignTests.actor

    def setUp(self):
        campaign_fixture.CampaignTests.setUp(self)
        self.server.ai.config['session_narration'] = True
        self.additions = [deepcopy(SITE)]
        self.on_narration = None
        self.narrations = 0
        original = self.server.ai.client.complete
        def complete(payload, timeout):
            if payload['response_format']['json_schema']['name'] == 'scene_narration':
                self.narrations += 1
                self.calls.append(json.loads(payload['messages'][1]['content']))
                if self.on_narration:self.on_narration()
                return {'content':json.dumps({'reply':'Мара рассказывает о мельнице у протоки.',
                                             'world_additions':self.additions},ensure_ascii=False)}
            return original(payload,timeout)
        self.server.ai.client.complete = complete

    def introduce(self, **kwargs):
        status, result = self.say(0,text='Мара, есть ли поблизости что исследовать?',intent='question',**kwargs)
        self.assertEqual(status,200,result)
        return self.state()

    def test_dialogue_adds_reachable_seed_and_sourced_unverified_question(self):
        state = self.introduce()
        hook = next(iter(state['world']['story_hooks'].values()))
        site = state['world']['places'][hook['destination']]
        self.assertTrue(site['unexplored'])
        self.assertEqual(site['canon'],[SITE['premise']])
        self.assertIn({'to':hook['destination']},state['world']['places']['market']['exits'])
        self.assertEqual(hook['status'],'unverified')
        self.assertEqual(hook['speaker_id'],'mara')
        self.assertEqual(hook['rumor'],SITE['rumor'])
        self.assertEqual(self.generated,0)
        self.assertEqual([self.actor(i)['coins'] for i in range(2)],[300,300])
        self.assertEqual(AdventureStore(self.server.ai).read(self.room),state)
        with self.server.service.store._connection() as c:
            raw=json.loads(c.execute('SELECT body FROM adventures WHERE room_id=?',(self.room,)).fetchone()[0])
        self.assertNotIn(hook['destination'],raw['world']['places'])
        self.assertNotIn(hook['id'],raw['world']['story_hooks'])
        self.assertIn(hook['id'],raw['campaign']['hook_refs'])

    def test_request_replay_does_not_regenerate_or_duplicate_seeds(self):
        key = str(uuid4())
        before = self.state()
        after = self.introduce(key=key)
        calls = self.narrations
        status,_ = self.say(0,text='Мара, есть ли поблизости что исследовать?',intent='question',
                            key=key,revision=before['revision'])
        self.assertEqual(status,200)
        self.assertEqual(self.state(),after)
        self.assertEqual(self.narrations,calls)
        self.introduce()
        self.assertEqual(len(self.state()['world']['story_hooks']),1)

    def test_first_visit_generates_once_and_evidence_advances_without_proving_rumor(self):
        state = self.introduce()
        hook = next(iter(state['world']['story_hooks'].values()))
        site = hook['destination']
        self.additions = []
        self.assertEqual(self.say(0,{'rule':'move','target':site})[0],200)
        self.assertEqual(self.generated,1)
        state = self.state()
        self.assertEqual(state['world']['story_hooks'][hook['id']]['status'],'visited')
        with self.server.service.store._connection() as c:
            trace=json.loads(c.execute("SELECT body FROM ai_requests WHERE json_extract(body,'$.role')='campaign_generation'").fetchone()[0])
        data=json.loads(trace['request']['messages'][1]['content'])
        self.assertEqual(data['story_hooks'][0]['rumor'],SITE['rumor'])
        self.assertEqual(data['seed']['canon'],[SITE['premise']])
        self.assertEqual(self.say(0,{'rule':'look','target':site+':feature:0'})[0],200)
        state = self.state()
        progress = state['world']['story_hooks'][hook['id']]
        self.assertEqual(progress['status'],'investigated')
        self.assertEqual(progress['rumor'],SITE['rumor'])
        self.assertEqual(len(progress['evidence']),1)
        self.assertIn(progress['evidence'][0],state['world']['leads'])
        self.assertFalse(any(q.get('completed') for q in state['world']['quests'].values()))
        self.assertEqual(self.actor()['coins'],300)
        self.say(0,{'rule':'move','target':'market'})
        self.say(0,{'rule':'move','target':site})
        self.assertEqual(self.generated,1)
        self.assertEqual(AdventureStore(self.server.ai).read(self.room)['world']['story_hooks'],state['world']['story_hooks'])

    def test_checkpoint_excludes_future_seeds_hooks_and_recollections(self):
        self.server.games.execute({'action':'save','room_id':self.room},str(uuid4()))
        after = self.introduce()
        destination = next(iter(after['world']['story_hooks'].values()))['destination']
        self.server.games.execute({'action':'load','room_id':self.room},str(uuid4()))
        state = self.state()
        self.assertNotIn(destination,state['world']['places'])
        self.assertFalse(state['world'].get('story_hooks'))
        self.assertFalse(state['campaign'].get('hook_refs'))
        data=context(state,self.actor(),'Где мельница?')
        self.assertEqual(data['world_growth']['hooks'],[])
        self.assertNotIn('Мельница',json.dumps(data,ensure_ascii=False))

    def test_old_observation_can_inform_a_new_hook_without_copying_the_clue(self):
        state=self.introduce()
        first=next(iter(state['world']['story_hooks'].values()))
        site=first['destination'];self.additions=[]
        self.say(0,{'rule':'move','target':site})
        self.say(0,{'rule':'look','target':site+':feature:0'})
        original=deepcopy(self.state()['world']['leads'])
        self.say(0,{'rule':'move','target':'market'})
        self.additions=[{**SITE,'premise':'','question':'Кто оставил записку у мельницы?'}]
        state=self.introduce();second=next(h for h in state['world']['story_hooks'].values() if h['id']!=first['id'])
        self.additions=[]
        self.say(1,{'rule':'move','target':site})
        self.say(1,{'rule':'look','target':site+':feature:0'})
        state=self.state()
        self.assertEqual(state['world']['leads'],original)
        self.assertEqual(state['world']['story_hooks'][second['id']]['status'],'investigated')
        self.assertEqual(state['world']['story_hooks'][second['id']]['evidence'],list(original))

    def test_invalid_batch_keeps_neither_seed_nor_prose_but_preserves_question(self):
        self.additions.append({**SITE,'name':'Чужой двор','coins':100000})
        before = self.state()
        after = self.introduce()
        self.assertEqual(after['world'],before['world'])
        self.assertEqual(after['history'][-1]['action'],'question')
        self.assertNotIn('Мара рассказывает',after['history'][-1]['gm'])
        self.assertFalse(after['campaign']['scene_refs'])
        self.assertFalse(after['campaign'].get('hook_refs'))

    def test_stale_narration_cannot_add_places_to_a_new_revision(self):
        self.on_narration=lambda:self.edit(lambda s:s.update(revision=s['revision']+1))
        state = self.introduce()
        self.assertFalse(state['world'].get('story_hooks'))
        self.assertFalse(state['campaign']['scene_refs'])
        traces=self.server.ai.traces(self.room)
        self.assertEqual(next(t for t in traces if t['role']=='session_narration')['status'],'stale')

    def test_remote_or_player_source_cannot_be_fabricated(self):
        for speaker in ('selena',self.players[0]['character_id'],'invented'):
            with self.subTest(speaker=speaker):
                state=self.state();before=deepcopy(state)
                with self.assertRaises(ValueError):
                    world_growth.apply(state,[{**SITE,'speaker_id':speaker}],self.players[0]['character_id'],str(uuid4()))
                self.assertEqual(state,before)

    def test_existing_anchor_cannot_be_renamed_or_relocated(self):
        state=self.state();before=deepcopy(state)
        with self.assertRaises(ValueError):
            world_growth.apply(state,[{**SITE,'name':'Новый мост'}],self.players[0]['character_id'],str(uuid4()))
        self.assertEqual(state,before)

    def test_new_question_can_reuse_existing_geography_without_rewriting_it(self):
        state=self.state();before=deepcopy(state['world']['places'])
        result=world_growth.apply(state,[{**SITE,'name':'Старый паром','premise':''}],self.players[0]['character_id'],str(uuid4()))
        self.assertEqual(state['world']['places'],before)
        self.assertFalse(result['places'])
        self.assertEqual(next(iter(result['hooks'].values()))['destination'],'old_ferry')
        world_growth.apply(state,[{**SITE,'name':'Старый паром','premise':'','question':'Кто видел пропавшую лодку?'}],
                           self.players[0]['character_id'],str(uuid4()))
        self.assertEqual(len(state['world']['story_hooks']),2)
        self.assertEqual(state['world']['places'],before)

    def test_batch_feedback_reports_both_conflicting_places_without_mutation(self):
        state=self.state();before=deepcopy(state)
        with self.assertRaises(ValueError) as failure:
            world_growth.validate_additions(state,[{**SITE,'name':'Старый паром'},
                                                  {**SITE,'name':'Новый мост'}],self.players[0]['character_id'],str(uuid4()))
        self.assertIn('world_additions[0]',str(failure.exception))
        self.assertIn('world_additions[1]',str(failure.exception))
        self.assertEqual(state,before)

    def test_evening_and_non_campaign_world_do_not_spawn_new_quest_branches(self):
        state=self.state()
        state['campaign']['format']='solo_evening'
        self.assertEqual(world_growth.capacity(state),0)
        with self.assertRaises(ValueError):
            world_growth.apply(state,[SITE],self.players[0]['character_id'],str(uuid4()))
        state.pop('campaign')
        self.assertEqual(world_growth.capacity(state),0)

    def test_context_keeps_a_bounded_selection_of_hooks(self):
        state=self.introduce()
        original=next(iter(state['world']['story_hooks'].values()))
        for index in range(1000):
            key='distant_'+str(index)
            state['world']['places'][key]={'name':'Дальний участок '+str(index),'description':'Далеко.', 'parent':'quarry','exits':[]}
            state['world']['story_hooks'][key]={**deepcopy(original),'id':key,'destination':key}
        data=context(state,self.actor(),'Что мы слышали о мельнице?')
        self.assertEqual(len(data['world_growth']['hooks']),6)
        self.assertLess(len(json.dumps(data,ensure_ascii=False).encode()),48000)

    def test_far_heard_destination_remains_addressable_without_repeating_its_name(self):
        from iirpg.session_context import schema
        state=self.introduce()
        hook=next(iter(state['world']['story_hooks'].values()))
        state['location']='quarry'
        actor=self.actor()
        data=context(state,actor,'Иду туда, куда советовала Мара.')
        self.assertIn(hook['destination'],data['places'])
        self.assertEqual(data['world_growth']['hooks'][0]['destination_name'],SITE['name'])
        self.assertIn(hook['destination'],schema(state,'Иду по слуху Мары.',actor['id'])['properties']['steps']['items']['properties']['target']['enum'])
        state['world']['story_hooks'][hook['id']]['heard_by']=[self.players[1]['character_id']]
        self.assertNotIn(hook['destination'],schema(state,'Иду по слуху Мары.',actor['id'])['properties']['steps']['items']['properties']['target']['enum'])
