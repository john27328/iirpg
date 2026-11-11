import json
import unittest
from uuid import uuid4
import test_gm_intents as fixtures
GMClient = fixtures.GMClient
from iirpg.adventure import AdventureStore
from iirpg.ai import AIService,load_config

class AdventureTests(unittest.TestCase):
    setUp=fixtures.GMIntentTests.setUp
    command=fixtures.GMIntentTests.command
    roster_command=fixtures.GMIntentTests.roster_command
    def roll_pending(self,session,result,natural=20):
        from iirpg.application import ChatService
        from iirpg.dice import DiceTool
        service=ChatService(self.store,DiceTool(roll_die=lambda sides:natural))
        pending=result['pending_check']
        payload={'sender':'first','type':'tool.call','tool':'dice.roll','request_id':pending['roll_request_id'],'offer_id':pending['offer_id']}
        service.send(self.room,payload)
        after=session.read(self.room)
        service.send(self.room,payload)
        self.assertEqual(session.read(self.room),after)
        return after

    def test_session_moves_checks_and_replays(self):
        client=GMClient();ai=AIService(self.combat,self.scene,load_config(),client);session=AdventureStore(ai)
        decision={'narration':'Вы направляетесь в кладовую.','action':'move','destination':'store','skill':'','objective':''}
        client.complete=lambda *args:{'content':json.dumps(decision)}
        command={'revision':0,'character_id':self.actor,'text':'Бегу в кладовую'};key=str(uuid4())
        moved=session.execute(self.room,command,key);self.assertEqual(moved['location'],'store');self.assertEqual(session.execute(self.room,command,key),moved)
        decision.update(narration='Попробуйте приподнять сундук.',action='check',destination='',skill='athletics',objective='book')
        self.combat.roll_die=lambda sides:20
        command.update(revision=1,text='Поднимаю сундук рычагом');key=str(uuid4());result=session.execute(self.room,command,key)
        self.assertFalse(result['book']);self.assertIn('pending_check',result);self.assertEqual(session.execute(self.room,command,key),result)
        result=self.roll_pending(session,result)
        self.assertTrue(result['book']);self.assertIn('check',result['history'][-1])
        decision.update(narration='Вы возвращаетесь.',action='move',destination='hall',skill='',objective='')
        command.update(revision=result['revision'],text='Возвращаюсь с книгой в зал');result=session.execute(self.room,command,str(uuid4()));self.assertFalse(result.get('completed',False))
        decision.update(action='return_book',destination='')
        command.update(revision=result['revision'],text='Возвращаю книгу хозяйке');result=session.execute(self.room,command,str(uuid4()));self.assertTrue(result['completed'])
        self.assertEqual(AdventureStore(ai).read(self.room),result)
    def test_invalid_response_does_not_move_or_roll(self):
        client=GMClient();session=AdventureStore(AIService(self.combat,self.scene,load_config(),client))
        client.complete=lambda *args:{'content':json.dumps({'narration':'Телепорт','action':'move','destination':'moon','skill':'','objective':''})}
        before=session.read(self.room)
        with self.assertRaises(ValueError):session.execute(self.room,{'revision':0,'character_id':self.actor,'text':'Ухожу'},str(uuid4()))
        self.assertEqual(session.read(self.room),before)
    def test_chat_history_migration_is_idempotent_and_check_cannot_narrate_failure(self):
        client=GMClient();session=AdventureStore(AIService(self.combat,self.scene,load_config(),client))
        client.complete=lambda *args:{'content':json.dumps({'narration':'Сундук не поддаётся, вы провалились.','action':'check','destination':'','skill':'athletics','objective':''})}
        self.combat.roll_die=lambda sides:20
        result=session.execute(self.room,{'revision':0,'character_id':self.actor,'text':'Поднимаю тяжёлый предмет'},str(uuid4()))
        self.assertNotIn('провалились', result['history'][-1]['gm'])
        self.assertIn('pending_check',result)
        result=self.roll_pending(session,result)
        self.assertEqual(result['history'][-1]['check']['degree'],3)
        before=self.store.page(self.room)
        session.read(self.room);session.read(self.room)
        self.assertEqual(self.store.page(self.room),before)
        from iirpg.adventure import current_world
        result['history'].append({'player':'Говорю с конюхом','gm':'Он успокоился'})
        self.assertNotIn('испуганный',current_world(result)['yard']['description'])
    def test_pending_check_waits_for_player_and_survives_reload(self):
        client=GMClient();ai=AIService(self.combat,self.scene,load_config(),client);session=AdventureStore(ai)
        client.complete=lambda *args:{'content':json.dumps({'narration':'Проверим усилие.','action':'check','destination':'','skill':'athletics','objective':''})}
        self.combat.roll_die=lambda sides:self.fail('GM rolled before player clicked')
        result=session.execute(self.room,{'revision':0,'character_id':self.actor,'text':'Приподнимаю груз'},str(uuid4()))
        self.assertEqual(AdventureStore(ai).read(self.room),result)
        with self.assertRaisesRegex(ValueError,'Сначала бросьте'):
            session.execute(self.room,{'revision':result['revision'],'character_id':self.actor,'text':'Иду дальше'},str(uuid4()))
        result=self.roll_pending(session,result,natural=1)
        self.assertNotIn('pending_check',result)
        self.assertEqual(result['history'][-1]['check']['degree'],0)
    def test_npc_reaction_receives_aggression_and_completed_roll_once(self):
        client=GMClient();session=AdventureStore(AIService(self.combat,self.scene,load_config(),client))
        client.complete=lambda *args:{'content':json.dumps({'narration':'Пробуете ударить.','action':'check','destination':'','skill':'athletics','objective':''})}
        pending=session.execute(self.room,{'revision':0,'character_id':self.actor,'text':'Бью конюха в живот'},str(uuid4()))
        offer=pending['pending_check']['offer_id'];self.roll_pending(session,pending,20)
        calls=[]
        def answer(payload,timeout):
            context=json.loads(payload['messages'][1]['content']);calls.append(context)
            return {'content':json.dumps({'narration':'Конюх сгибается от удара, отступает и зовёт на помощь.','action':'narrate','destination':'','skill':'','objective':''})}
        client.complete=answer
        result=session.react(self.room,offer)
        self.assertEqual(result['history'][-1]['action'],'reaction')
        self.assertEqual(result['history'][-1]['player'],'')
        self.assertIn('Бью конюха',json.dumps(calls[0],ensure_ascii=False))
        self.assertIn('check_result',json.dumps(calls[0]))
        self.assertEqual(session.react(self.room,offer),result)
        self.assertEqual(len(calls),1)
    def test_player_message_is_persisted_before_model_response(self):
        client=GMClient();session=AdventureStore(AIService(self.combat,self.scene,load_config(),client))
        def answer(*args):
            messages=self.store.page(self.room)['messages']
            self.assertTrue(any(m.get('text')=='Привет конюху' for m in messages))
            return {'content':json.dumps({'narration':'Конюх кивает.','action':'narrate','destination':'','skill':'','objective':''})}
        client.complete=answer
        session.execute(self.room,{'revision':0,'character_id':self.actor,'text':'Привет конюху'},str(uuid4()))
        messages=self.store.page(self.room)['messages']
        self.assertEqual(sum(m.get('text')=='Привет конюху' for m in messages),1)
    def test_failed_and_critically_failed_intimidation_both_get_reaction(self):
        client=GMClient();session=AdventureStore(AIService(self.combat,self.scene,load_config(),client))
        for natural,expected in ((10,1),(1,0)):
            with self.subTest(natural=natural):
                client.complete=lambda *args:{'content':json.dumps({'narration':'Попытка запугать.','action':'check','destination':'','skill':'intimidation','objective':''})}
                state=session.read(self.room)
                pending=session.execute(self.room,{'revision':state['revision'],'character_id':self.actor,'text':'Угрожаю конюху, требуя ответить'},str(uuid4()))
                offer=pending['pending_check']['offer_id'];rolled=self.roll_pending(session,pending,natural)
                self.assertEqual(rolled['history'][-1]['check']['degree'],expected)
                contexts=[]
                def answer(payload,timeout):
                    contexts.append(json.loads(payload['messages'][1]['content']))
                    return {'content':json.dumps({'narration':'Конюх отказывается отвечать и требует прекратить угрозы.','action':'narrate','destination':'','skill':'','objective':''})}
                client.complete=answer
                result=session.react(self.room,offer)
                self.assertEqual(contexts[0]['resolved_check']['skill'],'intimidation')
                self.assertEqual(contexts[0]['resolved_check']['degree'],expected)
                self.assertEqual(result['history'][-1]['action'],'reaction')
                self.assertEqual(session.react(self.room,offer),result)
                self.assertEqual(len(contexts),1)
    def test_location_after_moves_even_if_model_labels_it_narration(self):
        client=GMClient();session=AdventureStore(AIService(self.combat,self.scene,load_config(),client))
        client.complete=lambda *args:{'content':json.dumps({'narration':'Вы выходите во двор.','action':'narrate','destination':'','location_after':'yard','skill':'','objective':''})}
        result=session.execute(self.room,{'revision':0,'character_id':self.actor,'text':'Иду во двор'},str(uuid4()))
        self.assertEqual(result['location'],'yard')
        self.assertEqual(result['history'][-1]['action'],'move')
