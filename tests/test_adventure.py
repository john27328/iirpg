"""Player journeys through the real session, inventory, queue, dice and combat stores."""
import json
import unittest
from uuid import uuid4, UUID, uuid5
from copy import deepcopy
from story_fixtures import StoryFixture
from iirpg.adventure import AdventureStore
from iirpg.application import ChatService
from iirpg.dice import DiceTool
from iirpg.storage import MessageConflict


class AdventureTests(StoryFixture, unittest.TestCase):
    def test_compound_attempt_waits_for_player_then_takes_and_returns_real_item(self):
        self.combat.roll_die = lambda sides: self.fail('GM rolled before player')
        state = self.say('Иду в кладовую, выламываю сундук и беру книгу',
                         {'rule':'move','target':'store'}, {'rule':'force_open','target':'chest'}, {'rule':'take','target':'book'})
        self.assertEqual(state['location'], 'store'); self.assertFalse(state['book'])
        self.assertEqual(state['pending_check']['dc'], 15)
        self.assertEqual(AdventureStore(self.session.ai).read(self.room), state)
        state = self.roll_pending(self.session, state)
        self.assertTrue(state['book']); self.assertEqual(state['plan']['status'], 'completed')
        self.assertEqual(sum(i['name']=='Дорожная книга' for i in self.hero()['items']), 1)
        state = self.say('Иду в зал', {'rule':'move','target':'hall'})
        self.assertFalse(state['completed'])
        state = self.say('Отдаю хозяйке', {'rule':'give','target':'book','recipient':'hostess'})
        self.assertTrue(state['completed'])
        self.assertFalse(any(i['name']=='Дорожная книга' for i in self.hero()['items']))
        self.assertEqual(self.session.read(self.room), state)

    def test_partial_plan_commits_movement_but_does_not_take_from_closed_container(self):
        state = self.say('Иду, открываю, беру', {'rule':'move','target':'store'}, {'rule':'open','target':'chest'}, {'rule':'take','target':'book'})
        self.assertEqual(state['location'], 'store'); self.assertEqual(state['plan']['index'], 1)
        self.assertEqual(state['plan']['status'], 'blocked'); self.assertFalse(state['book'])
        self.assertNotIn('pending_check', state)
        state = self.say('Тогда возьму верёвку', {'rule':'take','target':'rope'})
        self.assertEqual(state['world']['entities']['rope']['owner'], self.actor)

    def test_failed_check_stops_remaining_steps_and_allows_another_approach(self):
        state = self.say('Подхожу, ломаю и беру', {'rule':'move','target':'store'}, {'rule':'force_open','target':'chest'}, {'rule':'take','target':'book'})
        state = self.roll_pending(self.session, state, 1)
        self.assertFalse(state['book']); self.assertTrue(state['world']['entities']['chest']['jammed'])
        self.assertEqual(state['plan']['status'], 'failed')
        state = self.say('Возьму лом и повторю', {'rule':'hold','target':'crowbar'}, {'rule':'force_open','target':'chest'}, {'rule':'take','target':'book'})
        self.assertEqual(state['pending_check']['modifier'], self.hero()['sheet_statistics']['athletics'] - 2)
        self.assertTrue(self.roll_pending(self.session, state, 20)['book'])

    def test_request_and_roll_retries_do_not_repeat_effects_or_model_calls(self):
        state = self.session.read(self.room); key = str(uuid4())
        self.client.proposal = {'intent':'act','reply':'','steps':[{'rule':'move','target':'store'},{'rule':'take','target':'rope'}]}
        command = {'revision':state['revision'],'character_id':self.actor,'text':'Беру верёвку в кладовой'}
        result = self.session.execute(self.room, command, key)
        self.assertEqual(self.session.execute(self.room, command, key), result)
        self.assertEqual(len(self.client.calls), 1)
        state = self.say('Беру ещё раз', {'rule':'take','target':'rope'})
        self.assertEqual(state['plan']['status'], 'blocked')
        self.assertEqual(sum(i['name']=='Верёвка из кладовой' for i in self.hero()['items']), 1)

    def test_questions_and_clarifications_during_pending_do_not_roll_or_cancel(self):
        state = self.say('Открою силой', {'rule':'move','target':'store'}, {'rule':'force_open','target':'chest'})
        pending = deepcopy(state['pending_check'])
        self.combat.roll_die = lambda sides: self.fail('Unexpected roll')
        state = self.say('А какой навык?', intent='question', reply='Атлетика; бросок ещё не выполнен.')
        self.assertEqual(state['pending_check'], pending)
        state = self.say('Лучше уйду', {'rule':'move','target':'yard'})
        self.assertEqual(state['location'], 'store'); self.assertEqual(state['pending_check'], pending)

    def test_cancelled_offer_cannot_roll_even_after_reload_and_new_plan_is_allowed(self):
        state = self.say('Открою силой', {'rule':'move','target':'store'}, {'rule':'force_open','target':'chest'})
        p = state['pending_check']; calls = len(self.client.calls)
        state = self.session.cancel(self.room, p['offer_id'])
        self.assertEqual(self.session.cancel(self.room, p['offer_id']), state)
        self.assertEqual(len(self.client.calls), calls)
        self.assertEqual(state['location'], 'store'); self.assertNotIn('pending_check', state)
        service = ChatService(self.store, DiceTool(roll_die=lambda sides: self.fail('Cancelled dice rolled')))
        with self.assertRaisesRegex(MessageConflict, 'отменена'):
            service.send(self.room, {'sender':'first','type':'tool.call','tool':'dice.roll','request_id':p['roll_request_id'],'offer_id':p['offer_id']})
        self.assertEqual(self.say('Ухожу', {'rule':'move','target':'yard'})['location'], 'yard')

    def test_model_cannot_mutate_world_via_question_or_extra_effect_fields(self):
        for proposal in ({'intent':'question','reply':'Сундук открыт','steps':[{'rule':'open','target':'chest'}]},
                         {'intent':'act','reply':'','steps':[{'rule':'look','target':'hall','effects':{'coins':100}}]},
                         {'action':'take','objective':'book','narration':'Готово'}):
            self.client.proposal = proposal; before = self.session.read(self.room)
            with self.assertRaises(ValueError):
                self.session.execute(self.room, {'revision':before['revision'],'character_id':self.actor,'text':'Как это работает?'}, str(uuid4()))
            self.assertEqual(self.session.read(self.room), before)
            self.assertTrue(any(m.get('metadata',{}).get('ai_error') for m in self.store.page(self.room)['messages']))

    def test_narrative_cannot_claim_check_results_or_loot(self):
        state = self.say('Попробую открыть', {'rule':'move','target':'store'}, {'rule':'force_open','target':'chest'}, reply='Вы провалились и потеряли 50 монет.')
        self.assertNotIn('потеряли', state['history'][-1]['gm'])
        self.assertIn('pending_check', state); self.assertFalse(state['book'])

    def test_player_message_is_committed_before_slow_provider_response(self):
        def answer(payload, timeout):
            self.assertTrue(any(m.get('text')=='Привет' for m in self.store.page(self.room)['messages']))
            return {'content':json.dumps({'intent':'question','reply':'Хозяйка здоровается.','steps':[]})}
        self.client.complete = answer
        self.say('Привет', intent='question')
        self.assertEqual(sum(m.get('text')=='Привет' for m in self.store.page(self.room)['messages']), 1)

    def test_reaction_uses_resolved_degree_and_is_idempotent(self):
        state = self.say('Располагаю хозяйку к себе', {'rule':'make_impression','target':'hostess'})
        offer = state['pending_check']['offer_id']; self.roll_pending(self.session, state, 1)
        self.client.proposal = {'intent':'question','reply':'Хозяйка раздражена вашей манерой разговора.','steps':[]}
        result = self.session.react(self.room, offer)
        self.assertEqual(self.client.calls[-1]['phase'], 'AFTER_ROLL')
        self.assertEqual(self.client.calls[-1]['confirmed_results'][-1]['check']['degree'], 0)
        self.assertEqual(result['history'][-1]['action'], 'reaction')
        calls = len(self.client.calls)
        self.assertEqual(self.session.react(self.room, offer), result); self.assertEqual(len(self.client.calls), calls)

    def test_attack_starts_combat_without_free_damage_and_tracks_arbitrary_target_name(self):
        self.edit(lambda s: s['world']['entities']['hostess'].update(name='Торговка Мира'))
        state = self.say('Нападаю', {'rule':'combat','target':'hostess'})
        battle = self.combat.read(self.room)
        self.assertEqual(state['active_combat']['id'], battle['id'])
        self.assertEqual(battle['fighters'][1]['name'], 'Торговка Мира')
        self.assertEqual(battle['fighters'][1]['hp'], battle['fighters'][1]['max_hp'])
        self.assertNotIn('pending_check', state)
        self.combat.roll_die = lambda sides: 20 if sides == 20 else 4
        for _ in range(40):
            if battle['status'] == 'finished': break
            battle = self.combat.execute(self.room, {'revision':battle['revision'],'action':'strike' if battle['actions'] else 'end_turn'}, str(uuid4()))
        self.assertEqual(battle['status'], 'finished')
        result = self.session.read(self.room)
        self.assertNotIn('active_combat', result)
        self.assertEqual(result['world']['entities']['hostess']['status'], 'defeated')
        self.assertEqual(self.session.read(self.room), result)

    def test_remote_attack_is_blocked_without_spawning_encounter(self):
        result = self.say('Бью далёкую цель', {'rule':'combat','target':'stablehand'})
        self.assertEqual(result['plan']['status'], 'blocked')
        self.assertEqual(self.combat.read(self.room)['status'], 'idle')

    def test_unrelated_training_is_archived_before_story_combat(self):
        old = self.combat.execute(self.room, {'action':'start','revision':0,'character_id':self.actor}, str(uuid4()))
        result = self.say('Атакую', {'rule':'combat','target':'hostess'})
        self.assertNotEqual(old['id'], result['active_combat']['id'])
        with self.store._connection() as c:
            self.assertIsNotNone(c.execute('SELECT body FROM session_training_archive WHERE room_id=? AND encounter_id=?', (self.room,old['id'])).fetchone())

    def test_deleted_loot_is_not_respawned_from_story_flags(self):
        self.say('Беру', {'rule':'move','target':'store'}, {'rule':'take','target':'rope'})
        item = next(i for i in self.hero()['items'] if i['name']=='Верёвка из кладовой')
        self.roster_command('remove_item',character_id=self.actor,id=item['id'])
        result = self.session.read(self.room)
        self.assertIsNone(result['world']['entities']['rope']['owner'])
        result = self.say('Беру снова', {'rule':'take','target':'rope'})
        self.assertEqual(result['plan']['status'], 'blocked')

    def test_hold_picks_up_ground_tool_and_stow_frees_real_hands(self):
        self.roster_command('add_item',character_id=self.actor,data={'name':'Тестовый щит','category':'shield','state':'held'})
        state=self.say('Подхожу и беру лом в руку',{'rule':'move','target':'store'},{'rule':'hold','target':'crowbar'})
        item=next(i for i in self.hero()['items'] if i['name']=='Лом')
        self.assertEqual(item['state'],'held');self.assertEqual(item['hands'],1)
        self.assertEqual(state['plan']['status'],'completed')
        state=self.say('Убираю лом',{'rule':'stow','target':'crowbar'})
        self.assertEqual(next(i for i in self.hero()['items'] if i['name']=='Лом')['hands'],0)

    def test_cancel_does_not_republish_earlier_dialogue_on_next_turn(self):
        state=self.say('Иду и пробую',{'rule':'move','target':'store'},{'rule':'force_open','target':'chest'})
        old_messages=self.store.page(self.room)['messages']
        old_text=state['history'][-1]['gm']
        self.session.cancel(self.room,state['pending_check']['offer_id'])
        self.say('Уточняю',intent='question',reply='Попытку можно начать заново.')
        messages=self.store.page(self.room)['messages']
        self.assertEqual(sum(m.get('text')==old_text for m in messages),1)
        self.assertTrue(all(any(m['request_id']==old['request_id'] for m in messages) for old in old_messages))

    def test_inventory_cannot_change_frozen_pending_check(self):
        self.say('Пробую',{'rule':'move','target':'store'},{'rule':'force_open','target':'chest'})
        with self.assertRaises(MessageConflict):
            self.roster_command('add_item',character_id=self.actor,data={'name':'Подсунутый лом','category':'gear','tags':['crowbar'],'state':'held'})

    def test_npc_decision_is_a_choice_boundary_not_automatic_work_by_player(self):
        state=self.say('Прошу помочь открыть сундук',{'rule':'request','target':'hostess','service':'help'},{'rule':'move','target':'store'})
        self.assertEqual(state['plan']['status'],'blocked') # no authored service
        self.edit(lambda s:s['world']['entities']['hostess'].update(services={'help':{'acceptable':True,'name':'Помощь','skills':['athletics'],'dc':12}}))
        state=self.say('Прошу помочь с сундуком',{'rule':'request','target':'hostess','service':'help'},{'rule':'move','target':'store'})
        state=self.roll_pending(self.session,state,20)
        self.assertEqual(state['location'],'hall')
        self.assertEqual(state['plan']['status'],'awaiting_choice')
        self.assertNotIn('pending_check',state)

    def test_roll_continuation_can_start_linked_combat_and_publishes_battle_link(self):
        state=self.say('Выламываю крышку, иду в зал и нападаю',{'rule':'move','target':'store'},{'rule':'force_open','target':'chest'},{'rule':'move','target':'hall'},{'rule':'combat','target':'hostess'})
        offer=state['pending_check']['offer_id']
        state=self.roll_pending(self.session,state,20)
        self.assertIn('active_combat',state)
        self.assertEqual(state['history'][-1]['combat_id'],state['active_combat']['id'])
        self.assertTrue(any(m.get('metadata',{}).get('combat_id')==state['active_combat']['id'] for m in self.store.page(self.room)['messages']))
        calls=len(self.client.calls);self.session.react(self.room,offer)
        self.assertEqual(len(self.client.calls),calls)

    def test_over_capacity_stops_pickup_after_movement_without_losing_prefix(self):
        self.edit(lambda s:s['world']['entities'].update(anvil={'kind':'item','name':'Тяжёлая наковальня','owner':'store','item':{'name':'Тяжёлая наковальня','category':'gear','bulk':'100'}}))
        state=self.say('Иду и беру наковальню',{'rule':'move','target':'store'},{'rule':'take','target':'anvil'})
        self.assertEqual(state['location'],'store')
        self.assertEqual(state['plan']['status'],'blocked')
        self.assertIn('Bulk',state['history'][-1]['gm'])
        self.assertFalse(any(i['name']=='Тяжёлая наковальня' for i in self.hero()['items']))

    def test_inflight_model_request_blocks_parallel_turn_and_inventory_change(self):
        def answer(payload,timeout):
            with self.assertRaises(MessageConflict):
                self.roster_command('add_item',character_id=self.actor,data={'name':'Подмена','category':'gear'})
            with self.assertRaises(MessageConflict):
                self.session.execute(self.room,{'revision':0,'character_id':self.actor,'text':'Другой ход'},str(uuid4()))
            return {'content':json.dumps({'intent':'question','reply':'Продолжаем разговор.','steps':[]})}
        self.client.complete=answer
        self.say('Разговариваю',intent='question')
        self.assertFalse(any(i['name']=='Подмена' for i in self.hero()['items']))

    def test_cancel_via_dialogue_publishes_one_revocation(self):
        state=self.say('Пробую',{'rule':'move','target':'store'},{'rule':'force_open','target':'chest'})
        offer=state['pending_check']['offer_id']
        state=self.say('Передумал, отменяю',intent='cancel')
        self.assertNotIn('pending_check',state)
        messages=self.store.page(self.room)['messages']
        self.assertEqual(sum(m.get('metadata',{}).get('cancelled_offer_id')==offer for m in messages),1)
        self.assertEqual(sum(m.get('text')==state['history'][-1]['gm'] for m in messages),1)
