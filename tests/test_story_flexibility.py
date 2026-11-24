"""Sourced recall, open requests and post-commit narration; no external AI."""
import json
import unittest
from uuid import uuid4

from iirpg.action_rules import prepare
from iirpg.story_memory import dialogue, fit_context
from iirpg.story_narration import narrative_scope
from story_fixtures import StoryFixture


class RecallTests(StoryFixture, unittest.TestCase):
    def test_failed_force_open_reaction_knows_retry_is_possible_with_same_dc(self):
        self.edit(lambda state: state['world']['entities']['hostess'].update(place='store'))
        state = self.say('Пробую открыть.', {'rule': 'move', 'target': 'store'}, {'rule': 'force_open', 'target': 'chest'})
        offer = state['pending_check']['offer_id']
        self.roll_pending(self.session, state, 1)
        self.client.proposal = {'intent': 'question', 'reply': 'Можно попробовать снова с поправкой на заклинивание.', 'steps': []}
        self.session.react(self.room, offer)
        resolution = self.client.calls[-1]['last_check_resolution']
        self.assertTrue(resolution['another_attempt_possible'])
        self.assertTrue(resolution['target_after']['jammed'])
        self.assertEqual(resolution['retry_dc'], 15)
        self.assertEqual(resolution['retry_modifier_without_new_aid'], self.hero()['sheet_statistics']['athletics'] - 4)

    def test_no_npc_reaction_is_generated_in_player_only_scene(self):
        state = self.say('Пробую открыть.', {'rule': 'move', 'target': 'store'}, {'rule': 'force_open', 'target': 'chest'})
        offer = state['pending_check']['offer_id']
        after = self.roll_pending(self.session, state, 20)
        calls = len(self.client.calls)
        self.assertEqual(self.session.react(self.room, offer), after)
        self.assertEqual(len(self.client.calls), calls)

    def test_old_conversation_is_recalled_without_truncating_quoted_details(self):
        original = {'player': 'Меня зовут Кир. Я ищу сестру.',
                    'gm': 'Описание. ' * 70 + 'Пароль: семь тихих колоколов.', 'action': 'question',
                    'character_id': self.actor, 'listeners': ['hostess'], 'player_request_id': str(uuid4())}
        fillers = [{'player': f'Смотрю на стол {i}', 'gm': 'Стол тот же.', 'action': 'question',
                    'character_id': self.actor, 'listeners': ['hostess']} for i in range(20)]
        self.edit(lambda state: state.update(history=[original, *fillers]))
        self.say('Ты помнишь мою историю и пароль?', intent='question')
        memories = self.client.calls[-1]['recollections']
        self.assertTrue(any(m['source'] == original['player_request_id'] and m['gm'] == original['gm'] for m in memories))
        self.assertTrue(all(m['listeners'] == ['hostess'] for m in memories))

    def test_recall_comes_from_current_branch_not_a_global_archive(self):
        checkpoint = self.session.read(self.room)
        self.say('Я ищу сестру Альму.', intent='question')
        with self.store._connection() as c:
            self.session.save(c, self.room, checkpoint)
        self.say('Что я рассказывал?', intent='question')
        self.assertNotIn('Альму', json.dumps(self.client.calls[-1], ensure_ascii=False))

    def test_history_persists_past_old_sixty_entry_limit(self):
        self.edit(lambda state: state.update(history=[{'player': f'Событие {i}', 'gm': 'Ответ', 'action': 'question'} for i in range(65)]))
        state = self.say('Продолжаем.', intent='question')
        self.assertEqual(state['history'][0]['player'], 'Событие 0')

    def test_budget_preserves_latest_exchange_and_frozen_check(self):
        latest = {'player': 'Какой навык?', 'gm': 'Атлетика.'}
        data = {'player': 'Пока не бросаю.', 'history': [{'gm': 'старое ' * 100} for _ in range(12)] + [latest],
                'recollections': [{'gm': 'давнее ' * 100}], 'recent_chat': [{'text': 'дубликат ' * 100}],
                'pending_check': {'offer_id': 'fixed', 'modifier': 7, 'dc': 15}}
        trimmed = fit_context(data, 1000)
        self.assertEqual(trimmed['history'][-1], latest)
        self.assertEqual(trimmed['pending_check'], data['pending_check'])
        self.assertLessEqual(len(json.dumps(trimmed, ensure_ascii=False).encode()), 1000)
        self.assertEqual(data['history'][0]['gm'], 'старое ' * 100)

    def test_recalled_other_hero_keeps_speaker_and_known_audience(self):
        initial = {'player': 'Моя сестра пропала.', 'gm': 'Я запомню.', 'character_id': 'other',
                   'listeners': ['stablehand'], 'action': 'question'}
        _, recalled = dialogue([initial] + [{'gm': 'Фон'} for _ in range(12)], 'Кто ищет сестру?', self.actor, 'hostess')
        self.assertEqual(recalled[0]['character_id'], 'other')
        self.assertEqual(recalled[0]['listeners'], ['stablehand'])
        self.assertTrue(recalled[0]['audience_known'])


class OpenRequestTests(StoryFixture, unittest.TestCase):
    def friendly_npc(self):
        self.edit(lambda state: state['world']['entities']['hostess'].update(services={}, attitude='friendly', skills={'athletics': 3}))

    def test_new_request_needs_no_authored_service_and_freezes_its_terms(self):
        self.friendly_npc()
        step = {'rule': 'request', 'target': 'hostess', 'method': 'Не называй моего имени постояльцам.', 'skill': 'diplomacy'}
        state = self.say('Прошу не говорить моего имени.', step)
        self.assertIn(step['method'], state['pending_check']['description'])
        self.assertNotIn(self.actor, state['world']['entities']['hostess'].get('agreements', {}))
        state = self.roll_pending(self.session, state, 20)
        agreement = state['world']['entities']['hostess']['agreements'][self.actor]
        self.assertEqual(agreement['description'], step['method'])
        self.assertFalse(agreement['follow'])
        self.assertEqual(agreement['help_skill'], '')
        self.assertNotIn('service', agreement)

    def test_failed_request_grants_no_commitment(self):
        self.friendly_npc()
        state = self.say('Прошу встретиться позже.', {'rule': 'request', 'target': 'hostess', 'method': 'Встретиться вечером во дворе.'})
        state = self.roll_pending(self.session, state, 1)
        self.assertNotIn(self.actor, state['world']['entities']['hostess'].get('agreements', {}))

    def test_new_promise_does_not_erase_previous_help_agreement(self):
        self.friendly_npc()
        state = self.say('Прошу помочь.', {'rule': 'request', 'target': 'hostess', 'method': 'Придержать сундук.', 'task': 'chest', 'skill': 'athletics'})
        self.roll_pending(self.session, state)
        state = self.say('И не называй моего имени.', {'rule': 'request', 'target': 'hostess', 'method': 'Не называть моего имени.'})
        self.roll_pending(self.session, state)
        state = self.say('Идём помогать.', {'rule': 'move', 'target': 'store'}, {'rule': 'aid', 'target': 'hostess', 'task': 'chest', 'skill': 'athletics'})
        self.assertEqual(len(state['world']['entities']['hostess']['commitments']), 2)
        self.assertEqual(state['world']['aid'][self.actor]['task'], 'chest')

    def test_scoped_help_allows_aid_only_for_agreed_task_and_skill(self):
        self.friendly_npc()
        state = self.say('Прошу помочь с крышкой.', {'rule': 'request', 'target': 'hostess', 'method': 'Придержать сундук, пока я поддеваю крышку.', 'task': 'chest', 'skill': 'athletics'})
        state = self.roll_pending(self.session, state)
        state = self.say('Идём к сундуку.', {'rule': 'move', 'target': 'store'})
        self.assertEqual(state['world']['entities']['hostess']['place'], 'store')
        with self.store._connection() as c:
            actor, _ = self.session.actor(c, self.room, self.actor)
        for task, skill in [('rope', 'athletics'), ('chest', 'thievery')]:
            with self.assertRaises(ValueError):
                prepare(state['world'], actor, {'rule': 'aid', 'target': 'hostess', 'task': task, 'skill': skill})
        state = self.say('Придержи сундук.', {'rule': 'aid', 'target': 'hostess', 'task': 'chest', 'skill': 'athletics'})
        self.assertEqual(state['world']['aid'][self.actor]['task'], 'chest')
        self.combat.roll_die = lambda sides: 20
        state = self.say('Пробую открыть.', {'rule': 'force_open', 'target': 'chest'})
        frozen = state['pending_check']
        self.say('Это моя проверка?', intent='question')
        supplied = self.client.calls[-1]['pending_check']
        self.assertEqual(supplied['roll_formula'], f"d20 {frozen['modifier']:+d}")
        self.assertTrue(supplied['aid_already_included'])
        self.assertEqual(supplied['modifier_before_aid'] + supplied['aid']['bonus'], supplied['modifier'])

    def test_open_commitment_does_not_execute_task_or_invent_rewards(self):
        self.friendly_npc()
        before = self.hero()
        state = self.say('Прошу помочь с сундуком и потом подожду.', {'rule': 'request', 'target': 'hostess', 'method': 'Помочь открыть сундук.', 'task': 'chest', 'skill': 'athletics'}, {'rule': 'move', 'target': 'store'})
        state = self.roll_pending(self.session, state)
        self.assertEqual(state['location'], 'hall')
        self.assertFalse(state['world']['entities']['chest']['open'])
        self.assertEqual(self.hero()['coins'], before['coins'])

    def test_context_has_no_menu_of_services(self):
        self.say('Что скажешь?', intent='question')
        data = self.client.calls[-1]
        self.assertTrue(all('services' not in entity for entity in data['entities'].values()))
        self.assertTrue(all('services' not in entity for entity in data['entity_index'].values()))

    def test_new_physical_method_uses_existing_rules_without_authored_affordance(self):
        state = self.say('Иду и пробую открыть плечом.', {'rule': 'move', 'target': 'store'},
                         {'rule': 'improvise', 'target': 'chest', 'method': 'Упираюсь плечом под край крышки.', 'skill': 'athletics'})
        self.assertNotIn('affordances', state['world']['entities']['chest'])
        self.assertEqual(state['pending_check']['dc'], state['world']['entities']['chest']['force_dc'])
        self.assertEqual(state['pending_check']['prepared_action']['rule'], 'force_open')
        self.assertFalse(state['world']['entities']['chest']['open'])
        self.assertTrue(self.roll_pending(self.session, state, 20)['world']['entities']['chest']['open'])

    def test_new_method_does_not_grant_unknown_effect_or_bypass_lock_tools(self):
        for skill in ('thievery', 'diplomacy', 'invented_skill'):
            state = self.say('Пробую новый способ.', {'rule': 'move', 'target': 'store'},
                             {'rule': 'improvise', 'target': 'chest', 'method': 'Новый способ.', 'skill': skill})
            self.assertEqual(state['plan']['status'], 'blocked')
            self.assertFalse(state['world']['entities']['chest']['open'])


class NarrationTests(StoryFixture, unittest.TestCase):
    def test_narration_scope_allows_only_conscious_npcs_and_confirmed_currency(self):
        data = {'shared_party': [{'id': self.actor}], 'gm_knowledge': {
            'hostess': {'name': 'Хозяйка', 'status': 'defeated'},
            'stablehand': {'name': 'Конюх', 'status': 'standing'}}, 'confirmed_results': [
                {'effects': [{'type': 'transfer', 'entity': 'book', 'to': self.actor}]}]}
        world = {'entities': {'book': {'kind': 'item', 'name': 'Книга'},
                              'coins': {'kind': 'currency', 'name': '5 медных монет'}}, 'quests': {}}
        scoped = narrative_scope(data, world)
        self.assertEqual(scoped['npc_speakers'], {'stablehand': 'Конюх'})
        self.assertEqual(scoped['confirmed_rewards'], [])
        data['confirmed_results'].append({'effects': [{'type': 'transfer', 'entity': 'coins', 'to': self.actor}]})
        self.assertEqual(narrative_scope(data, world)['confirmed_rewards'][0]['name'], '5 медных монет')

    def setUp(self):
        super().setUp()
        self.session.ai.config['session_narration'] = True
        self.calls = []

    def configure(self, narration):
        def answer(payload, timeout):
            data = json.loads(payload['messages'][1]['content'])
            self.calls.append(data)
            if data['phase'] == 'AFTER_ACTION':
                return narration(data)
            return {'content': json.dumps({'intent': 'act', 'reply': 'Вы нашли книгу!', 'steps': [{'rule': 'move', 'target': 'store'}, {'rule': 'take', 'target': 'rope'}]})}
        self.client.complete = answer

    def test_narration_sees_committed_effects_and_replay_never_calls_twice(self):
        def response(data):
            self.assertEqual(data['location_id'], 'store')
            self.assertEqual(data['npc_speakers'], {})
            self.assertEqual(data['confirmed_rewards'], [])
            self.assertFalse(data['pending_check'])
            self.assertEqual(data['plan']['status'], 'completed')
            self.assertEqual(self.session.read(self.room)['location'], 'store')
            return {'content': json.dumps({'reply': 'В кладовой пахнет мукой и сыростью.'})}
        self.configure(response)
        state = self.session.read(self.room)
        key = str(uuid4())
        command = {'revision': state['revision'], 'character_id': self.actor, 'text': 'Иду в кладовую и беру верёвку.'}
        after = self.session.execute(self.room, command, key)
        self.assertEqual(after['history'][-1]['action'], 'narration')
        self.assertFalse(after['world']['entities']['chest']['open'])
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(self.session.execute(self.room, command, key), after)
        self.assertEqual(len(self.calls), 2)
        self.assertNotIn('Вы нашли книгу!', [m.get('text') for m in self.store.page(self.room)['messages']])

    def test_narration_failure_preserves_action_and_receipt(self):
        def failure(data):
            raise ValueError('OpenRouter недоступен.')
        self.configure(failure)
        state = self.session.read(self.room)
        key = str(uuid4())
        command = {'revision': state['revision'], 'character_id': self.actor, 'text': 'Пробую открыть.'}
        after = self.session.execute(self.room, command, key)
        self.assertEqual(after['location'], 'store')
        self.assertEqual(after['world']['entities']['rope']['owner'], self.actor)
        self.assertEqual(self.session.execute(self.room, command, key), after)
        self.assertEqual([t['status'] for t in self.session.ai.traces(self.room) if t['role'] == 'session_narration'], ['failed'])

    def test_narration_cannot_return_commands_or_extra_state(self):
        self.configure(lambda data: {'content': json.dumps({'reply': 'Готово', 'steps': [{'rule': 'take', 'target': 'book'}]})})
        after = self.say('Пробую.')
        self.assertEqual(after['history'][-1]['action'], 'act')
        self.assertFalse(after['book'])

    def test_daily_limit_skips_optional_prose_after_committed_action(self):
        self.session.ai.config['daily_request_limit'] = 1
        self.configure(lambda data: self.fail('Narration exceeded daily limit'))
        after = self.say('Пробую.')
        self.assertEqual(after['location'], 'store')
        self.assertEqual(len(self.calls), 1)

    def test_changed_revision_discards_prose_without_reverting_new_state(self):
        def response(data):
            with self.store._connection() as c:
                state = self.session._load(c, self.room)
                state['revision'] += 1
                state['test_newer_state'] = True
                self.session.save(c, self.room, state)
            return {'content': json.dumps({'reply': 'Устаревшее описание.'})}
        self.configure(response)
        self.say('Пробую.')
        self.assertTrue(self.session.read(self.room)['test_newer_state'])
        self.assertFalse(any(h['gm'] == 'Устаревшее описание.' for h in self.session.read(self.room)['history']))
        self.assertEqual([t['status'] for t in self.session.ai.traces(self.room) if t['role'] == 'session_narration'], ['stale'])

    def test_pending_attempt_waits_for_roll_before_narrating_outcome(self):
        def answer(payload, timeout):
            self.calls.append(json.loads(payload['messages'][1]['content']))
            self.assertNotEqual(self.calls[-1]['phase'], 'AFTER_ACTION')
            return {'content': json.dumps({'intent': 'act', 'reply': 'Крышка уже поддалась!',
                'steps': [{'rule': 'move', 'target': 'store'}, {'rule': 'force_open', 'target': 'chest'}]})}
        self.client.complete = answer
        state = self.say('Пробую открыть.')
        self.assertTrue(state['pending_check'])
        self.assertFalse(state['world']['entities']['chest']['open'])
        self.assertEqual(len(self.calls), 1)
        self.assertNotIn('поддалась', state['history'][-1]['gm'])

    def test_rules_question_keeps_exact_gm_answer_without_rewriting_as_npc(self):
        def answer(payload, timeout):
            self.calls.append(payload)
            return {'content': json.dumps({'intent': 'question', 'reply': 'Ваша Атлетика с однократной помощью NPC.', 'steps': [], 'voice': 'gm'})}
        self.client.complete = answer
        state = self.say('Чья проверка?', intent='question')
        self.assertEqual(state['history'][-1]['gm'], 'Ваша Атлетика с однократной помощью NPC.')
        self.assertEqual(len(self.calls), 1)

    def test_npc_conversation_gets_separate_reply_without_effects(self):
        def answer(payload, timeout):
            data = json.loads(payload['messages'][1]['content'])
            self.calls.append(data)
            value = {'reply': 'Я дорожу доверием постояльцев.'} if data['phase'] == 'DIALOGUE' else {
                'intent': 'question', 'reply': 'Личный вопрос собеседнику.', 'steps': [], 'voice': 'npc'}
            return {'content': json.dumps(value)}
        self.client.complete = answer
        state = self.say('Что тебе дорого?', intent='question')
        self.assertEqual(state['history'][-1]['gm'], 'Я дорожу доверием постояльцев.')
        self.assertEqual(state['location'], 'hall')
        self.assertEqual(len(self.calls), 2)

    def test_identical_npc_reply_is_not_published_twice(self):
        text = 'Я помню ваши истории и не перепутаю вас.'
        def answer(payload, timeout):
            data = json.loads(payload['messages'][1]['content'])
            self.calls.append(data)
            value = {'reply': text} if data['phase'] == 'DIALOGUE' else {
                'intent': 'question', 'reply': text, 'steps': [], 'voice': 'npc'}
            return {'content': json.dumps(value)}
        self.client.complete = answer
        before = self.session.read(self.room)
        state = self.say('Ты запомнила?', intent='question')
        self.assertEqual(len(state['history']), len(before['history']) + 1)
        self.assertEqual(sum(m.get('text') == text for m in self.store.page(self.room)['messages']), 1)
        self.assertEqual(len(self.calls), 2)
        self.assertTrue(next(t for t in self.session.ai.traces(self.room) if t['role'] == 'session_narration')['deduplicated'])


if __name__ == '__main__':
    unittest.main()
