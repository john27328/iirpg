"""Scenario-independent rules and player approaches, including failure and retries."""
from copy import deepcopy
import unittest
from uuid import uuid4
from story_fixtures import StoryFixture
from iirpg.world import scenario, location, route, validate, public_view
from iirpg.action_rules import prepare, apply, aid_roll, attitude, validate_proposal


class ActionRulesTests(unittest.TestCase):
    def worlds(self):
        for identifier, start, container, item, npc in (
                ('missing-book','hall','chest','book','hostess'),
                ('watch-post','road','gate','horn','warden')):
            value = deepcopy(scenario(identifier)['world'])
            value['entities']['player'] = {'kind':'pc','place':start,'name':'Игрок'}
            yield value, container, item, npc
        # Third dataset has different IDs, names and an extra room/edge; no engine edits.
        value = deepcopy(scenario('watch-post')['world'])
        import json
        serialized = json.dumps(value)
        for old,new in [('road','pier'),('bridge','path'),('tower','shed'),('gate','hatch'),('horn','parcel'),('warden','porter')]:
            serialized = serialized.replace('"'+old+'"','"'+new+'"')
        value = json.loads(serialized)
        value['entities']['porter']['name'] = 'Грузчик'
        value['entities']['hatch']['name'] = 'Люк'
        value['places']['yard2'] = {'name':'Внешний двор','description':'Другое место','exits':[{'to':'pier'}]}
        value['places']['pier']['exits'].append({'to':'yard2'})
        value['entities']['player'] = {'kind':'pc','place':'yard2','name':'Игрок'}
        yield value, 'hatch', 'parcel', 'porter'

    def actor(self, **extra):
        return {'id':'player','stats':{'athletics':7,'thievery':7,'diplomacy':5,'intimidation':5,'perception':5},
                'ranks':{'thievery':1}, 'items':[], **extra}

    def test_same_open_transfer_and_social_rules_on_three_different_worlds(self):
        for world, container, item, npc in self.worlds():
            with self.subTest(container=container):
                validate(world)
                actor = self.actor()
                target_place = location(world, container)
                move = prepare(world, actor, {'rule':'move','target':target_place}); apply(world, move)
                before = deepcopy(world)
                action = prepare(world, actor, {'rule':'force_open','target':container})
                self.assertEqual(world, before); self.assertEqual(action['dc'], world['entities'][container]['force_dc'])
                apply(world, action, 2)
                self.assertTrue(world['entities'][container]['open'])
                apply(world, prepare(world, actor, {'rule':'move','target':item}))
                result = apply(world, prepare(world, actor, {'rule':'take','target':item}))
                self.assertEqual(world['entities'][item]['owner'], actor['id'])
                self.assertEqual(result['effects'][0]['type'], 'transfer')
                apply(world, prepare(world, actor, {'rule':'move','target':npc}))
                apply(world, prepare(world, actor, {'rule':'give','target':item,'recipient':npc}))
                self.assertEqual(world['entities'][item]['owner'], npc)
                self.assertTrue(any(q.get('completed') for q in world['quests'].values()))
                social = prepare(world, actor, {'rule':'make_impression','target':npc})
                self.assertEqual(social['dc'], 10 + world['entities'][npc]['will'])
                apply(world, social, 3)
                self.assertEqual(attitude(world['entities'][npc], actor['id']), 'helpful')
                self.assertNotEqual(attitude(world['entities'][npc], 'other-player'), 'helpful')

    def test_force_open_all_degrees_have_only_registered_consequences(self):
        for degree in range(4):
            world, target, *_ = next(self.worlds()); actor = self.actor()
            apply(world, prepare(world, actor, {'rule':'move','target':target}))
            prepared = prepare(world, actor, {'rule':'force_open','target':target})
            self.assertEqual(prepared['modifier'],5)
            apply(world, prepared, degree); entity=world['entities'][target]
            self.assertEqual(entity['open'], degree>=2)
            self.assertEqual(entity.get('jammed',False), degree==0)
            self.assertEqual(entity.get('damaged',False), degree==2)
            self.assertEqual(world['entities']['book']['owner'],'chest')

    def test_lock_requires_training_and_real_accessible_tools_not_their_names(self):
        world, target, *_ = next(self.worlds()); actor=self.actor()
        apply(world, prepare(world, actor, {'rule':'move','target':target}))
        for changed, message in (({'ranks':{'thievery':0}},'trained'), ({},'набор'),
                                 ({'items':[{'id':'fake','name':'Волшебные отмычки','state':'held'}]},'набор'),
                                 ({'items':[{'id':'kit','tags':['thieves_tools'],'state':'stowed'}]},'наденьте')):
            with self.subTest(message=message),self.assertRaisesRegex(ValueError,message):
                prepare(world,{**actor,**changed},{'rule':'pick_lock','target':target})
        actor['items']=[{'id':'kit','tags':['thieves_tools'],'state':'worn'}]
        for outcome, progress in ((0,0),(1,0),(2,1),(3,2)):
            trial=deepcopy(world); result=apply(trial,prepare(trial,actor,{'rule':'pick_lock','target':target}),outcome)
            self.assertEqual(trial['entities'][target]['lock']['progress'],progress)
            self.assertEqual(any(e['type']=='break_tool' for e in result['effects']),outcome==0)
            self.assertFalse(trial['entities'][target]['open'])

    def test_locked_route_and_closed_contents_are_inaccessible_until_open(self):
        world, target, item, npc = list(self.worlds())[1]; actor=self.actor()
        with self.assertRaisesRegex(ValueError,'пути'):prepare(world,actor,{'rule':'move','target':item})
        apply(world,prepare(world,actor,{'rule':'force_open','target':target}),3)
        self.assertEqual(route(world,'road','tower'),['road','bridge','tower'])
        world, target, item, npc = next(self.worlds())
        apply(world,prepare(world,actor,{'rule':'move','target':target}))
        with self.assertRaisesRegex(ValueError,'недоступен'):prepare(world,actor,{'rule':'take','target':item})
        self.assertNotIn(item,public_view(world,'player','store')['entities'])

    def test_sense_motive_uses_deception_dc_and_does_not_read_full_knowledge(self):
        world,_,_,npc=next(self.worlds()); actor=self.actor(); entity=world['entities'][npc]
        entity.update(knowledge=['Секретное место клада'],deception=8,motive='защитить товар')
        prepared=prepare(world,actor,{'rule':'sense_motive','target':npc})
        self.assertEqual(prepared['dc'],18)
        result=apply(world,prepared,2)
        self.assertNotIn('клад',result['text']); self.assertNotIn('защитить',result['text'])
        with self.assertRaisesRegex(ValueError,'изменения'):prepare(world,actor,{'rule':'sense_motive','target':npc})
        apply(world,prepare(world,actor,{'rule':'make_impression','target':npc}),2)
        self.assertEqual(prepare(world,actor,{'rule':'sense_motive','target':npc})['dc'],18)

    def test_unknown_abilities_are_checked_against_registry_in_any_language(self):
        world,*_=next(self.worlds()); actor=self.actor(abilities={'light':{}})
        for name in ('fireball','Фаербол','Телепортация','novel-power-734'):
            with self.subTest(name=name),self.assertRaisesRegex(ValueError,'способности нет'):
                prepare(world,actor,{'rule':'cast','ability':name})
        self.assertEqual(prepare(world,actor,{'rule':'cast','ability':'light'})['rule'],'cast')

    def test_improvisation_requires_authored_bounded_effect_not_arbitrary_patch(self):
        world,target,*_=next(self.worlds());actor=self.actor()
        apply(world,prepare(world,actor,{'rule':'move','target':target}))
        with self.assertRaisesRegex(ValueError,'проверяемого эффекта'):prepare(world,actor,{'rule':'improvise','target':target,'method':'wish'})
        world['entities'][target]['affordances']={'lever':{'skill':'athletics','dc':12,'effect':'open'}}
        action=prepare(world,actor,{'rule':'improvise','target':target,'method':'lever'})
        self.assertEqual(action['dc'],12);apply(world,action,2)
        self.assertTrue(world['entities'][target]['open'])
        with self.assertRaises(ValueError):validate_proposal({'intent':'act','reply':'','steps':[{'rule':'improvise','target':target,'method':'lever','effects':{'hp':0}}]})

    def test_world_rejects_cycles_and_unbound_references(self):
        for identifier in (None, [], {}, 'not-a-scenario'):
            with self.subTest(identifier=identifier), self.assertRaises(ValueError): scenario(identifier)
        for rule in (None, [], {}, 4):
            with self.subTest(rule=rule), self.assertRaises(ValueError):
                validate_proposal({'intent':'act','reply':'','steps':[{'rule':rule}]})
        world,*_=next(self.worlds())
        world['entities']['book']['owner']='book'
        with self.assertRaisesRegex(ValueError,'Цикл'):validate(world)
        world['entities']['book']['owner']='missing'
        with self.assertRaisesRegex(ValueError,'владелец'):validate(world)


class SocialJourneyTests(StoryFixture,unittest.TestCase):
    def test_impression_request_follow_and_task_specific_aid_not_permanent_bonus(self):
        state=self.say('Иду к конюху и прошу помочь',{'rule':'move','target':'stablehand'},{'rule':'request','target':'stablehand','service':'help'})
        self.assertEqual(state['plan']['status'],'blocked');self.assertNotIn('pending_check',state)
        state=self.say('Спокойно знакомлюсь',{'rule':'make_impression','target':'stablehand'})
        self.assertEqual(state['pending_check']['dc'],13)
        self.roll_pending(self.session,state,20)
        state=self.say('Прошу помочь',{'rule':'request','target':'stablehand','service':'help'})
        self.roll_pending(self.session,state,20)
        state=self.say('Идём к сундуку',{'rule':'move','target':'chest'})
        self.assertEqual(state['world']['entities']['stablehand']['place'],'store')
        self.combat.roll_die=lambda sides:20
        state=self.say('Пусть помогает поддеть',{'rule':'aid','target':'stablehand','task':'chest','skill':'athletics'},{'rule':'force_open','target':'chest'})
        p=state['pending_check'];self.assertEqual(p['aid']['bonus'],2)
        self.assertEqual(p['modifier'],self.hero()['sheet_statistics']['athletics']) # −2 no crowbar +2 Aid
        self.session.cancel(self.room,p['offer_id'])
        state=self.say('Попробую сам',{'rule':'force_open','target':'chest'})
        self.assertNotIn('aid',state['pending_check'])
        self.assertEqual(state['pending_check']['modifier'],self.hero()['sheet_statistics']['athletics']-2)

    def test_failed_and_critical_coercion_have_recorded_reaction_and_retry_rules(self):
        self.say('Иду к собеседнику',{'rule':'move','target':'stablehand'})
        state=self.say('Угрожаю, требую помощи',{'rule':'coerce','target':'stablehand','service':'help'})
        state=self.roll_pending(self.session,state,1)
        npc=state['world']['entities']['stablehand']
        self.assertNotIn(self.actor,npc.get('agreements',{}))
        self.assertEqual(attitude(npc,self.actor),'unfriendly')
        self.assertIn('не соглашается',state['history'][-1]['gm'])
        state=self.say('Повторяю угрозу',{'rule':'coerce','target':'stablehand','service':'help'})
        self.assertEqual(state['plan']['status'],'blocked');self.assertNotIn('pending_check',state)
        state=self.say('Извиняюсь и пытаюсь наладить разговор',{'rule':'make_impression','target':'stablehand'})
        self.assertIn('pending_check',state)

    def test_living_npc_loot_is_not_free_and_defeated_loot_is_credited_once(self):
        self.say('Иду',{'rule':'move','target':'stablehand'})
        state=self.say('Беру его деньги',{'rule':'take','target':'stablehand_coins'})
        self.assertEqual(state['plan']['status'],'blocked');before=self.hero()['coins']
        self.edit(lambda s:s['world']['entities']['stablehand'].update(status='defeated'))
        state=self.say('Беру мелочь',{'rule':'take','target':'stablehand_coins'})
        self.assertEqual(self.hero()['coins'],before+5)
        self.say('Ещё раз беру',{'rule':'take','target':'stablehand_coins'})
        self.assertEqual(self.hero()['coins'],before+5)

    def test_move_and_light_are_two_real_steps_not_prose_only(self):
        self.roster_command('configure_build',id=self.actor,data={'class_id':'wizard','general_feat':'fleet'})
        state=self.say('Иду в кладовую и создаю свет',{'rule':'move','target':'store'},{'rule':'cast','ability':'light'})
        self.assertEqual(state['location'],'store');self.assertTrue(self.hero()['build']['light'])
        self.assertEqual(self.hero()['build']['spent'],0)
        self.assertEqual(len(state['plan']['receipts']),2)

    def test_failed_pick_lock_breaks_real_tool_and_repeat_is_blocked(self):
        self.roster_command('update_character',id=self.actor,data={'sheet':{'values':{'thievery_rank':'trained'}}})
        state=self.say('Беру инструмент и вскрываю',{'rule':'move','target':'store'},{'rule':'take','target':'lockpicks'},{'rule':'pick_lock','target':'chest'})
        state=self.roll_pending(self.session,state,1)
        self.assertTrue(next(i for i in self.hero()['items'] if 'thieves_tools' in i.get('tags',[]))['broken'])
        state=self.say('Снова вскрываю',{'rule':'pick_lock','target':'chest'})
        self.assertEqual(state['plan']['status'],'blocked');self.assertNotIn('pending_check',state)

    def test_lock_progress_is_not_item_success_and_does_not_auto_repeat_rolls(self):
        self.roster_command('update_character',id=self.actor,data={'sheet':{'values':{'thievery_rank':'trained'}}})
        state=self.say('Беру инструмент, вскрываю и забираю',{'rule':'move','target':'store'},{'rule':'take','target':'lockpicks'},{'rule':'pick_lock','target':'chest'},{'rule':'open','target':'chest'},{'rule':'take','target':'book'})
        natural=20-self.hero()['sheet_statistics']['thievery'] # total20, ordinary success
        state=self.roll_pending(self.session,state,natural)
        self.assertEqual(state['world']['entities']['chest']['lock']['progress'],1)
        self.assertFalse(state['book']);self.assertNotIn('pending_check',state)
        state=self.say('Продолжаю вскрытие',{'rule':'pick_lock','target':'chest'},{'rule':'open','target':'chest'},{'rule':'take','target':'book'})
        self.assertTrue(self.roll_pending(self.session,state,20)['book'])

    def test_impression_is_per_interaction_but_coercion_consequences_persist(self):
        self.say('Иду',{'rule':'move','target':'stablehand'})
        state=self.say('Располагаю',{'rule':'make_impression','target':'stablehand'})
        state=self.roll_pending(self.session,state,20)
        self.assertEqual(attitude(state['world']['entities']['stablehand'],self.actor),'helpful')
        self.say('Ухожу',{'rule':'move','target':'hall'})
        state=self.say('Возвращаюсь',{'rule':'move','target':'stablehand'})
        self.assertEqual(attitude(state['world']['entities']['stablehand'],self.actor),'indifferent')
        state=self.say('Угрожаю',{'rule':'coerce','target':'stablehand','service':'help'})
        self.roll_pending(self.session,state,1)
        self.say('Ухожу',{'rule':'move','target':'hall'})
        state=self.say('Возвращаюсь',{'rule':'move','target':'stablehand'})
        self.assertEqual(attitude(state['world']['entities']['stablehand'],self.actor),'unfriendly')
        state=self.say('Снова угрожаю',{'rule':'coerce','target':'stablehand','service':'help'})
        self.assertEqual(state['plan']['status'],'blocked')

    def test_existing_help_is_not_rerolled_after_interaction_attitude_ends(self):
        self.say('Иду к NPC',{'rule':'move','target':'stablehand'})
        state=self.say('Располагаю',{'rule':'make_impression','target':'stablehand'})
        self.roll_pending(self.session,state,20)
        state=self.say('Прошу помочь',{'rule':'request','target':'stablehand','service':'help'})
        self.roll_pending(self.session,state,20)
        state=self.say('Идём и готовим помощь',{'rule':'move','target':'store'},{'rule':'request','target':'stablehand','service':'help'},{'rule':'aid','target':'stablehand','task':'chest','skill':'athletics'})
        self.assertNotIn('pending_check',state)
        self.assertEqual(state['plan']['status'],'completed')
        self.assertEqual(state['world']['aid'][self.actor]['task'],'chest')

    def test_unique_service_can_be_selected_but_multiple_services_require_clarification(self):
        self.edit(lambda s:s['world']['entities']['hostess'].update(services={'work':{'name':'Помощь с работой','acceptable':True,'skills':['athletics']}}))
        state=self.say('Прошу об услуге',{'rule':'request','target':'hostess'})
        self.assertEqual(state['pending_check']['prepared_action']['step']['service'],'work')
        self.assertIn('Помощь с работой',state['pending_check']['description'])
        self.session.cancel(self.room,state['pending_check']['offer_id'])
        self.edit(lambda s:s['world']['entities']['hostess']['services'].update(other={'name':'Другая услуга','acceptable':True}))
        state=self.say('Прошу об услуге',{'rule':'request','target':'hostess'})
        self.assertEqual(state['plan']['status'],'blocked');self.assertNotIn('pending_check',state)

    def test_real_inventory_id_resolves_to_same_entity_without_creating_duplicate(self):
        state=self.say('Беру вещь',{'rule':'move','target':'store'},{'rule':'take','target':'rope'})
        identity=state['world']['entities']['rope']['roster_item_id']
        self.say('Возвращаюсь и отдаю',{'rule':'move','target':'hostess'},{'rule':'give','target':identity,'recipient':'hostess'})
        self.assertFalse(any(i['id']==identity for i in self.hero()['items']))
        self.assertEqual(self.session.read(self.room)['world']['entities']['rope']['owner'],'hostess')

    def test_exhausted_spell_cannot_start_combat_or_spend_again(self):
        from iirpg.character_resources import save_character
        self.roster_command('configure_build',id=self.actor,data={'class_id':'wizard','general_feat':'fleet'})
        hero=self.hero();hero['build']['spent']=2
        with self.store._connection() as c:save_character(c,self.room,hero)
        state=self.say('Пробую боевое заклинание',{'rule':'cast','ability':'force_barrage','target':'hostess'})
        self.assertEqual(state['plan']['status'],'blocked')
        self.assertNotIn('active_combat',state)
        self.assertEqual(self.hero()['build']['spent'],2)

    def test_detect_magic_uses_world_entities_instead_of_scenario_specific_answer(self):
        self.roster_command('configure_build',id=self.actor,data={'class_id':'wizard','general_feat':'fleet'})
        self.edit(lambda s:s['world']['entities']['book'].update(magical=True))
        state=self.say('Ищу магию здесь',{'rule':'cast','ability':'detect_magic'})
        self.assertIn('не обнаружено',state['history'][-1]['gm'])
        state=self.say('Ищу магию там',{'rule':'move','target':'store'},{'rule':'cast','ability':'detect_magic'})
        self.assertIn('ощущается',state['history'][-1]['gm'])
        self.assertEqual(self.hero()['build']['spent'],0)

    def test_ordinary_and_critical_failed_coercion_both_have_actual_result_and_gm_reaction(self):
        self.say('Иду к собеседнику',{'rule':'move','target':'stablehand'})
        for natural,expected in ((10,1),(1,0)):
            with self.subTest(natural=natural):
                state=self.say('Угрожаю и требую помощи',{'rule':'coerce','target':'stablehand','service':'help'})
                offer=state['pending_check']['offer_id']
                state=self.roll_pending(self.session,state,natural)
                self.assertEqual(state['history'][-1]['check']['degree'],expected)
                self.assertIn('не соглашается',state['history'][-1]['gm'])
                self.assertNotIn(self.actor,state['world']['entities']['stablehand'].get('agreements',{}))
                self.client.proposal={'intent':'question','reply':'Собеседник не соглашается и требует прекратить угрозы.','steps':[]}
                state=self.session.react(self.room,offer)
                self.assertEqual(state['history'][-1]['action'],'reaction')
                self.assertEqual(self.client.calls[-1]['confirmed_results'][-1]['check']['degree'],expected)
