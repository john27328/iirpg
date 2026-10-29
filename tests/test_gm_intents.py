"""GM proposes bounded mechanics; only the player's confirmation rolls dice."""
from copy import deepcopy
import json
import unittest
from uuid import uuid4

import test_party_combat
from iirpg.ai import AIService, load_config
from iirpg.combat import CombatStore
from iirpg.combat_conditions import armor_class
from iirpg.demoralize import prepare_demoralize, demoralize
from iirpg.storage import MessageConflict


class GMClient:
    def __init__(self):
        self.calls = []
        self.choice = 'demoralize_nonlinguistic_2'

    def ready(self):
        return True

    def complete(self, payload, timeout):
        self.calls.append(deepcopy(payload))
        return {'content': json.dumps({'choice': self.choice, 'explanation': 'Грохот и крик могут устрашить врага. Предлагаю Деморализацию без слов.'})}


class GMIntentTests(unittest.TestCase):
    setUp = test_party_combat.PartyCombatTests.setUp
    command = test_party_combat.PartyCombatTests.command
    roster_command = test_party_combat.PartyCombatTests.roster_command
    start = test_party_combat.PartyCombatTests.start
    act = test_party_combat.PartyCombatTests.act

    def pending(self):
        state = self.start()
        state = self.act(state, 'move', position=[3, 3])
        state = self.act(state, 'intent', text='Грозно кричу и бью мечем по щиту', target_id=state['fighters'][2]['entity_id'])
        self.client = GMClient()
        self.ai = AIService(self.combat, self.scene, load_config(), self.client)
        return state

    def analyze(self, state, key=None):
        return self.ai.analyze_intent(self.room, {'revision':state['revision'], 'intent_id':state['intents'][-1]['id']}, key or str(uuid4()))

    def test_proposal_requires_confirmation_and_replays_without_new_dice_or_model(self):
        state = self.pending();rolls = list(self.rolls);key = str(uuid4())
        response = self.analyze(state, key);offered = response['combat'];proposal = offered['intents'][-1]['analysis']
        self.assertEqual(proposal['effect'], 'demoralize')
        self.assertEqual(proposal['check']['language_penalty'], -4)
        self.assertEqual(offered['actions'], state['actions'])
        self.assertEqual(self.rolls, rolls)
        self.assertEqual(offered['fighters'], state['fighters'])
        self.assertEqual(self.analyze(state, key), response)
        self.assertEqual(len(self.client.calls), 1)
        context = response['trace']['context']
        self.assertEqual(context['role'], 'gm')
        self.assertEqual(context['intent']['text'], state['intents'][-1]['text'])
        self.assertNotIn('chat', context)
        self.assertEqual(self.client.calls[0]['provider']['max_price'], {'prompt':0,'completion':0})
        self.combat.roll_die = lambda sides: self.rolls.append(sides) or 20
        command = {'revision':offered['revision'], 'action':'accept_intent', 'intent_id':offered['intents'][-1]['id']}
        accept_key = str(uuid4());result = self.combat.execute(self.room, command, accept_key)
        self.assertEqual(result['actions'], state['actions'] - 1)
        self.assertEqual(result['attacks'], state['attacks'])
        self.assertEqual(result['fighters'][2]['frightened'], 2)
        self.assertEqual(result['intents'][-1]['status'], 'resolved')
        self.assertEqual(result['journal'][-1]['action'], 'demoralize')
        self.assertEqual(self.rolls, rolls + [20])
        self.assertEqual(self.combat.execute(self.room, command, accept_key), result)
        self.assertEqual(CombatStore(self.roster, lambda sides:1).read(self.room), result)
        self.assertEqual(len(self.client.calls), 1)
        with self.assertRaises(ValueError):self.act(result, 'accept_intent', intent_id=command['intent_id'])

    def test_invalid_choice_and_failed_analysis_leave_intent_pending(self):
        state = self.pending();self.client.choice = 'invented_roll'
        with self.assertRaises(ValueError):self.analyze(state)
        self.assertEqual(self.combat.read(self.room), state)
        self.client.choice = 'manual';offered = self.analyze(state)['combat']
        self.assertEqual(offered['intents'][-1]['status'], 'pending')
        with self.assertRaises(ValueError):self.act(offered, 'accept_intent', intent_id=state['intents'][-1]['id'])

    def test_stale_response_and_wrong_actor_never_apply(self):
        state = self.pending();complete = self.client.complete
        def interrupted(payload, timeout):
            self.act(state, 'shield')
            return complete(payload, timeout)
        self.client.complete = interrupted
        with self.assertRaises(MessageConflict):self.analyze(state)
        current = self.combat.read(self.room)
        self.assertNotIn('analysis', current['intents'][-1])
        self.assertEqual(self.ai.traces(self.room)[0]['status'], 'stale')
        current = self.act(current, 'end_turn')
        with self.assertRaises(ValueError):self.analyze(current)
        self.assertEqual(len(self.client.calls), 1)

    def test_legacy_encounter_hydrates_skills_without_restarting(self):
        state = self.pending()
        del state['skill_rules']
        for fighter in state['fighters']:
            fighter.pop('will', None);fighter.pop('intimidation', None)
        with self.store._connection() as c:
            c.execute('UPDATE encounters SET body=? WHERE room_id=?', (json.dumps(state), self.room))
        offered = self.analyze(state)['combat']
        self.assertIn('check', offered['intents'][-1]['analysis'])
        self.assertEqual(offered['id'], state['id'])

    def test_confirmation_revalidates_changed_check_and_immunity(self):
        state = self.pending();offered = self.analyze(state)['combat']
        offered['fighters'][0]['frightened'] = 1
        with self.store._connection() as c:
            c.execute('UPDATE encounters SET body=? WHERE room_id=?', (json.dumps(offered), self.room))
        rolls = list(self.rolls)
        with self.assertRaisesRegex(ValueError, 'изменились'):self.act(offered, 'accept_intent', intent_id=offered['intents'][-1]['id'])
        self.assertEqual(self.rolls, rolls)

    def test_demoralize_degrees_penalties_immunity_and_turn_decay(self):
        state = self.pending();scene = self.scene.read(self.room);victim = state['fighters'][2]['entity_id']
        state['fighters'][0]['intimidation'] = 8
        for natural, amount in [(1,0), (5,0), (10,1), (20,2)]:
            trial = deepcopy(state)
            demoralize(trial, scene, victim, 'nonlinguistic', lambda sides:natural)
            self.assertEqual(trial['fighters'][2]['frightened'], amount)
            self.assertEqual(trial['actions'], state['actions']-1)
            self.assertEqual(trial['attacks'], 0)
            with self.assertRaisesRegex(ValueError, 'невосприимчива'):prepare_demoralize(trial, scene, victim)
            trial['round'] += 100
            prepare_demoralize(trial, scene, victim)
        trial = deepcopy(state);trial['fighters'][0]['frightened'] = 2;trial['fighters'][2]['frightened'] = 1
        check = prepare_demoralize(trial, scene, victim, 'nonlinguistic')
        self.assertEqual(check['modifier'], 2);self.assertEqual(check['dc'], 13)
        self.assertEqual(armor_class(trial, trial['fighters'][2]), trial['fighters'][2]['ac']-1)
        self.combat.end_turn(trial)
        self.assertEqual(trial['fighters'][0]['frightened'], 1)
        self.assertEqual(trial['fighters'][2]['frightened'], 1)
        self.combat.end_turn(trial);self.combat.end_turn(trial)
        self.assertEqual(trial['fighters'][2]['frightened'], 0)

    def test_illegal_target_no_dice_and_language_penalty_is_not_double(self):
        state = self.pending();scene = self.scene.read(self.room);victim = state['fighters'][2]['entity_id']
        state['fighters'][2]['languages'] = ['different']
        for delivery in ('speech','nonlinguistic'):
            self.assertEqual(prepare_demoralize(state, scene, victim, delivery)['language_penalty'], -4)
        for update in ({'deafened':True}, {'immunities':['fear']}, {'position':[10,1]}):
            trial = deepcopy(state);trial['fighters'][2].update(update)
            with self.assertRaises(ValueError):demoralize(trial, scene, victim, 'speech', lambda sides:self.fail('invalid action rolled'))
        with self.assertRaises(ValueError):prepare_demoralize(state, scene, self.second)

    def test_frightened_changes_attack_and_defence_but_not_damage(self):
        state = self.pending();state['mapped'] = False
        state['fighters'][0]['frightened'] = 2
        state['fighters'][2]['frightened'] = 1
        base = deepcopy(state);base['fighters'][0]['frightened'] = 0;base['fighters'][2]['frightened'] = 0
        self.combat.roll_die = lambda sides:10 if sides==20 else 1
        victim = state['fighters'][2]['entity_id']
        self.combat.strike(state, victim);self.combat.strike(base, victim)
        self.assertEqual(state['last_attack']['total'], base['last_attack']['total']-2)
        self.assertEqual(state['last_attack']['dc'], base['last_attack']['dc']-1)
        self.assertEqual(state['last_attack']['damage'], base['last_attack']['damage'])

    def test_frightened_changes_athletics_and_npc_context_without_enemy_numbers(self):
        from iirpg.scene_rules import prepare_tip_over
        from iirpg.ai_context import npc_context
        state = self.pending();scene = self.scene.read(self.room)
        actor = next(e for e in scene['entities'] if e['id']==self.actor)
        normal = prepare_tip_over(scene, actor, 'table', None, self.roster.read(self.room))
        afraid = prepare_tip_over(scene, actor, 'table', None, self.roster.read(self.room), 2)
        self.assertEqual(afraid[1], normal[1]-2)
        state['turn'] = 2;state['fighters'][2]['frightened'] = 2
        context, _ = npc_context(state, scene, load_config()['npc'])
        self.assertEqual(context['self']['frightened'], 2)
        self.assertEqual(context['self_effective_ac'], state['fighters'][2]['ac']-2)
        for contact in context['opponents']:
            self.assertNotIn('will', contact);self.assertNotIn('frightened', contact)

    def test_group_without_selected_target_cannot_offer_a_random_enemy(self):
        state = self.pending()
        state['intents'][-1]['selected_target_id'] = None
        from iirpg.gm_context import intent_options
        context, commands = intent_options(state, self.scene.read(self.room), self.roster.read(self.room), state['intents'][-1])
        self.assertTrue(context['target_selection_required'])
        self.assertFalse(any(c['effect'] in ('strike','demoralize') for c in commands.values()))
        self.assertIn('need_target', commands)
        context, commands = intent_options(state, self.scene.read(self.room), self.roster.read(self.room), state['intents'][-1], state['fighters'][2]['entity_id'])
        self.assertFalse(context['target_selection_required'])
        self.assertTrue(any(c['effect']=='demoralize' for c in commands.values()))

    def test_clarification_keeps_dialogue_cancels_old_offer_and_requires_fresh_analysis(self):
        state = self.pending();offered = self.analyze(state)['combat'];intent = offered['intents'][-1]
        self.assertEqual([m['role'] for m in intent['messages']], ['user','assistant'])
        key = str(uuid4());command = {'action':'clarify_intent','revision':offered['revision'], 'intent_id':intent['id'], 'text':'Я говорю врагу: сдавайся или пожалеешь!'}
        clarified = self.combat.execute(self.room, command, key);rolls = list(self.rolls)
        self.assertEqual(clarified['actions'], offered['actions'])
        self.assertEqual(clarified['fighters'], offered['fighters'])
        self.assertNotIn('analysis', clarified['intents'][-1])
        self.assertFalse(any(e['action']=='clarify_intent' for memory in clarified['awareness'].values() for e in memory['events']))
        self.assertEqual(self.combat.execute(self.room, command, key), clarified)
        self.assertEqual(CombatStore(self.roster, lambda sides:1).read(self.room), clarified)
        with self.assertRaises(ValueError):self.act(clarified, 'accept_intent', intent_id=intent['id'])
        self.client.choice = 'demoralize_speech_2'
        response = self.analyze(clarified);context = response['trace']['context']
        self.assertEqual([m['role'] for m in context['conversation']], ['user','assistant','user'])
        self.assertEqual(context['conversation'][-1]['text'], command['text'])
        self.assertEqual(context['conversation'][0]['text'], state['intents'][-1]['text'])
        self.assertEqual(response['combat']['intents'][-1]['analysis']['check']['language_penalty'], 0)
        self.assertEqual(self.rolls, rolls)
        with self.assertRaises(MessageConflict):self.combat.execute(self.room, {'action':'accept_intent','revision':offered['revision'],'intent_id':intent['id']}, str(uuid4()))

    def test_reviewed_button_and_known_text_are_proposals_not_immediate_actions(self):
        state = self.start();rolls = list(self.rolls)
        state = self.act(state, 'intent', text='Поднять щит', review=True, requested_action={'action':'shield'})
        self.assertEqual(state['actions'], 3);self.assertFalse(state['fighters'][0]['shield'])
        self.client = GMClient();self.ai = AIService(self.combat, self.scene, load_config(), self.client)
        from iirpg.gm_context import intent_options
        context, commands = intent_options(state, self.scene.read(self.room), self.roster.read(self.room), state['intents'][-1])
        self.assertEqual(context['requested_action'], {'action':'shield'})
        self.client.choice = next(k for k,c in commands.items() if c['effect']=='shield')
        offered = self.analyze(state)['combat'];self.assertEqual(offered['actions'], 3)
        result = self.act(offered, 'accept_intent', intent_id=offered['intents'][-1]['id'])
        self.assertTrue(result['fighters'][0]['shield']);self.assertEqual(result['actions'], 2)
        self.assertEqual(self.rolls, rolls)
        result = self.act(result, 'intent', text='Идти 3,3', review=True)
        context, commands = intent_options(result, self.scene.read(self.room), self.roster.read(self.room), result['intents'][-1])
        self.assertEqual(commands['text_command']['position'], [3,3])
        self.assertEqual(result['fighters'][0]['position'], state['fighters'][0]['position'])

    def test_dialogue_is_scoped_bounded_and_wrong_turn_cannot_clarify(self):
        state = self.pending();intent = state['intents'][-1]
        for invalid in ('', 'x'*1001, None):
            with self.assertRaises(ValueError):self.act(state, 'clarify_intent', intent_id=intent['id'], text=invalid)
            self.assertEqual(self.combat.read(self.room), state)
        other = self.act(state, 'intent', text='ОТДЕЛЬНЫЙ замысел', review=True)
        other = self.act(other, 'clarify_intent', intent_id=intent['id'], text='Уточнение первого замысла')
        from iirpg.gm_context import intent_options
        context, _ = intent_options(other, self.scene.read(self.room), self.roster.read(self.room), other['intents'][0])
        self.assertNotIn('ОТДЕЛЬНЫЙ', json.dumps(context,ensure_ascii=False))
        state = self.act(other, 'end_turn')
        with self.assertRaises(ValueError):self.act(state, 'clarify_intent', intent_id=intent['id'], text='Чужой ход')
        from iirpg.intent_dialogue import append_message
        trial = {'text':'Начало'}
        for _ in range(14):append_message(trial, 'user', 'уточнение')
        with self.assertRaisesRegex(ValueError, 'лимит'):append_message(trial, 'user', 'ещё')

    def test_clarification_during_model_call_invalidates_old_response(self):
        state = self.pending();complete = self.client.complete
        def interrupted(payload, timeout):
            self.act(state, 'clarify_intent', intent_id=state['intents'][-1]['id'], text='Я передумал, хочу укрыться')
            return complete(payload, timeout)
        self.client.complete = interrupted
        with self.assertRaises(MessageConflict):self.analyze(state)
        current = self.combat.read(self.room)
        self.assertNotIn('analysis', current['intents'][-1])
        self.assertEqual(current['actions'], state['actions'])

    def test_legacy_proposal_metadata_does_not_invalidate_mechanical_confirmation(self):
        state = self.pending();offered = self.analyze(state)['combat']
        check = offered['intents'][-1]['analysis']['check'];check.pop('targeting');check.pop('outcomes')
        with self.store._connection() as c:c.execute('UPDATE encounters SET body=? WHERE room_id=?', (json.dumps(offered), self.room))
        result = self.act(offered, 'accept_intent', intent_id=offered['intents'][-1]['id'])
        self.assertEqual(result['actions'], offered['actions']-1)
