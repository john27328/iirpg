"""Regressions from the Erin/stablehand session: grounding, checks and repetition."""
from copy import deepcopy
import json
import unittest
from uuid import uuid4
import test_adventure as fixtures
from iirpg.adventure import AdventureStore
from iirpg.ai import AIService, load_config
from iirpg.story_context import conversation_context, requested_check, unavailable_spell


class StoryContextTests(unittest.TestCase):
    setUp=fixtures.AdventureTests.setUp
    command=fixtures.AdventureTests.command
    roster_command=fixtures.AdventureTests.roster_command
    roll_pending=fixtures.AdventureTests.roll_pending

    def session(self,action='narrate'):
        self.client=fixtures.GMClient();self.calls=[]
        def answer(payload,timeout):
            self.calls.append(json.loads(payload['messages'][1]['content']))
            return {'content':json.dumps({'narration':'Конюх снова поправляет ремни и отводит взгляд.','action':action,'destination':'','location_after':'yard','skill':'','objective':'stablehand' if action=='combat' else ''})}
        self.client.complete=answer
        session=AdventureStore(AIService(self.combat,self.scene,load_config(),self.client))
        state={'revision':0,'location':'yard','book':False,'history':[{'player':'Осматриваю конюха','gm':'Конюх поправляет ремни.','action':'narrate'}],'character_id':self.actor}
        with self.store._connection() as c:c.execute('INSERT INTO adventures VALUES(?,?)',(self.room,json.dumps(state)))
        return session

    def test_explicit_perception_request_cannot_be_replaced_with_narration(self):
        session=self.session();before=self.store.page(self.room)
        command={'revision':0,'character_id':self.actor,'text':'хочу проверить нимательность'};key=str(uuid4())
        result=session.execute(self.room,command,key)
        self.assertEqual(result['pending_check']['skill'],'perception')
        self.assertEqual(result['history'][-1]['action'],'check')
        self.assertEqual(self.calls[-1]['current_turn']['explicit_check'],'perception')
        self.assertNotEqual(self.store.page(self.room),before)
        self.assertEqual(session.execute(self.room,command,key),result)
        result=self.roll_pending(session,result,15)
        self.assertEqual(result['history'][-1]['check']['natural'],15)

    def test_unavailable_spell_is_explained_even_if_model_suggests_attack(self):
        self.roster_command('configure_build',id=self.actor,data={'class_id':'wizard','general_feat':'fleet'})
        session=self.session('combat');before=self.roster.read(self.room)
        result=session.execute(self.room,{'revision':0,'character_id':self.actor,'text':'кидаю в него фаербол'},str(uuid4()))
        entry=result['history'][-1]
        self.assertEqual(entry['action'],'narrate')
        self.assertIn('недоступно',entry['gm'])
        self.assertIn('Силовой залп',entry['gm'])
        self.assertEqual(self.roster.read(self.room),before)
        self.assertEqual(self.combat.read(self.room)['status'],'idle')
        self.assertNotIn('active_combat',result)

    def test_npc_knowledge_survives_meeting_and_repeated_history_is_only_compacted_in_context(self):
        repeated='Конюх сжимает ремни упряжи и смотрит в сторону зала. Я ничего не скажу под угрозой.'
        state={'location':'yard','history':[{'player':'Запугиваю конюха','gm':repeated,'action':'narrate'},
                                          {'player':'Где книга?','gm':repeated,'action':'narrate'}]}
        before=deepcopy(state);context=conversation_context(state,'Я просто хочу расспросить про книгу')
        self.assertEqual(state,before)
        self.assertEqual([n['id'] for n in context['present_npcs']],['stablehand'])
        self.assertIn('кладовой',context['present_npcs'][0]['knowledge'][0])
        self.assertNotEqual(context['history'][-1]['gm'],repeated)
        self.assertEqual(context['history'][-1]['player'],'Где книга?')

    def test_explicit_guards_leave_questions_negation_ordinary_inspection_and_supported_actions_to_gm(self):
        for text in ('Осматриваю конюха','Почему он нервничает?','Проверяю, есть ли выход во двор','Как бросить проверку восприятия?','Не хочу проверку внимательности'):
            self.assertIsNone(requested_check(text))
        for text in ('Не кидаю фаербол','Как работает фаербол?','Говорю: сейчас кину фаербол',
                     'Кидаю камень','Применяю Силовой залп','Хочу проверить perception и athletics'):
            self.assertIsNone(unavailable_spell(text))
        self.assertIsNone(requested_check('Хочу проверить perception и athletics'))
        self.assertEqual(unavailable_spell('Кастую fireball в конюха'),'Фаербол')
        self.assertEqual(requested_check('Прошу проверку внимательности'),'perception')

    def test_repeated_narration_gets_one_metered_rewrite_without_repeating_player_message(self):
        session=self.session()
        repeated='Конюх сжимает ремни упряжи и смотрит в сторону зала. Я не скрываю ничего и не хочу проблем.'
        with self.store._connection() as c:
            state=session._load(c,self.room);state['history'][-1]['gm']=repeated
            c.execute('UPDATE adventures SET body=? WHERE room_id=?',(json.dumps(state),self.room))
        calls=[]
        def answer(payload,timeout):
            context=json.loads(payload['messages'][1]['content']);calls.append(context)
            return {'content':json.dumps({'narration':'Я видел, как книгу спрятали у сундука в кладовой. Только больше не угрожай.' if 'task' in context else repeated,'action':'narrate','destination':'','location_after':'yard','skill':'','objective':''})}
        self.client.complete=answer
        command={'revision':0,'character_id':self.actor,'text':'Я просто хочу расспросить про книгу'};key=str(uuid4())
        result=session.execute(self.room,command,key)
        self.assertIn('кладовой',result['history'][-1]['gm'])
        self.assertEqual(len(calls),2)
        self.assertNotIn('gm',calls[-1]['history'][-1])
        self.assertEqual(session.ai.status()['requests_today'],2)
        self.assertEqual(sum(m['text']==command['text'] for m in self.store.page(self.room)['messages']),1)
        self.assertEqual(session.execute(self.room,command,key),result)
        self.assertEqual(len(calls),2)

    def test_rewrite_cannot_perform_a_different_action(self):
        session=self.session();session.ai.config['daily_request_limit']=1
        repeated='Конюх сжимает ремни упряжи и смотрит в сторону зала. Я не скрываю ничего и не хочу проблем.'
        with self.store._connection() as c:
            state=session._load(c,self.room);state['history'][-1]['gm']=repeated
            c.execute('UPDATE adventures SET body=? WHERE room_id=?',(json.dumps(state),self.room))
        self.client.complete=lambda *a:{'content':json.dumps({'narration':repeated,'action':'narrate','destination':'','location_after':'yard','skill':'','objective':''})}
        with self.assertRaisesRegex(ValueError,'лимит'):session.execute(self.room,{'revision':0,'character_id':self.actor,'text':'Почему ты напряжён?'},str(uuid4()))
        self.assertEqual(session.read(self.room)['revision'],0)
        session.ai.config['daily_request_limit']=100
        context={'location_id':'yard','history':[]}
        self.client.complete=lambda *a:{'content':json.dumps({'narration':'Выходите.','action':'move','destination':'hall','location_after':'hall','skill':'','objective':''})}
        with self.assertRaisesRegex(ValueError,'только реплику'):session.rewrite(self.room,str(uuid4()),{'messages':[]},context)
