"""Executable regressions from the second Erin session, without model/network calls."""
import json
import unittest
from uuid import uuid4
import test_adventure as fixtures
from iirpg.adventure import AdventureStore
from iirpg.ai import AIService,load_config
from iirpg.adventure_inventory import available_items
from iirpg.story_context import requested_check,travel_route


class StoryActionTests(unittest.TestCase):
    setUp=fixtures.AdventureTests.setUp
    command=fixtures.AdventureTests.command
    roster_command=fixtures.AdventureTests.roster_command
    roll_pending=fixtures.AdventureTests.roll_pending

    def session(self):
        self.client=fixtures.GMClient();self.calls=[]
        self.decision={'narration':'Конюх слушает.','action':'narrate','destination':'','location_after':'yard','skill':'','objective':''}
        def answer(payload,timeout):
            self.calls.append(json.loads(payload['messages'][1]['content']))
            return {'content':json.dumps(self.decision)}
        self.client.complete=answer
        session=AdventureStore(AIService(self.combat,self.scene,load_config(),self.client))
        state={'revision':0,'location':'yard','book':False,'history':[],'character_id':self.actor}
        with self.store._connection() as c:c.execute('INSERT INTO adventures VALUES(?,?)',(self.room,json.dumps(state)))
        return session

    def say(self,session,text,key=None):
        return session.execute(self.room,{'revision':session.read(self.room)['revision'],'character_id':self.actor,'text':text},key or str(uuid4()))

    def test_question_does_not_authorize_move_even_when_model_changes_location(self):
        session=self.session();self.decision.update(action='move',destination='hall',location_after='hall')
        result=self.say(session,'Как открыть сундук?')
        self.assertEqual(result['location'],'yard')
        self.assertEqual(result['history'][-1]['action'],'narrate')

    def test_travel_uses_existing_intermediate_rooms(self):
        session=self.session();self.decision.update(action='move',destination='store',location_after='store')
        result=self.say(session,'Иду в кладовую ищу чем вскрыть замок')
        self.assertEqual(result['location'],'store')
        self.assertEqual(result['history'][-1]['route'],['yard','hall','store'])
        self.assertIn('Зал постоялого двора',result['history'][-1]['result'])
        with self.assertRaises(ValueError):travel_route({'a':{'exits':[]},'b':{'exits':[]}},'a','b')

    def test_move_with_light_really_casts_and_survives_reload(self):
        self.roster_command('configure_build',id=self.actor,data={'class_id':'wizard','general_feat':'fleet'})
        session=self.session();self.decision.update(action='move',destination='store',location_after='store')
        result=self.say(session,'Иду в кладовую, заклинание свет')
        self.assertEqual(result['location'],'store')
        self.assertEqual(result['history'][-1]['effects'],[{'action':'cast','spell_id':'light'}])
        hero=next(a for a in self.roster.read(self.room)['characters'] if a['id']==self.actor)
        self.assertTrue(hero['build']['light'])
        self.assertEqual(hero['build']['spent'],0)
        self.assertEqual(AdventureStore(session.ai).read(self.room),result)

    def test_npc_coins_are_gated_credited_once_and_preserved_on_retry(self):
        session=self.session();self.decision.update(action='take',objective='stablehand_coins')
        self.assertNotIn('stablehand_coins',available_items(session.read(self.room)))
        with self.assertRaises(ValueError):self.say(session,'Забираю мелочь')
        with self.store._connection() as c:
            state=session._load(c,self.room);state['npc_outcomes']={'stablehand':'defeated'}
            c.execute('UPDATE adventures SET body=? WHERE room_id=?',(json.dumps(state),self.room))
        before=next(a for a in self.roster.read(self.room)['characters'] if a['id']==self.actor)
        command={'revision':state['revision'],'character_id':self.actor,'text':'Забираю мелочь'};key=str(uuid4())
        result=session.execute(self.room,command,key)
        hero=next(a for a in self.roster.read(self.room)['characters'] if a['id']==self.actor)
        self.assertEqual(hero['coins'],before['coins']+5)
        self.assertEqual(hero['items'],before['items'])
        self.assertIn('stablehand_coins',result['collected_items'])
        self.assertEqual(session.execute(self.room,command,key),result)
        self.assertEqual(next(a for a in self.roster.read(self.room)['characters'] if a['id']==self.actor)['coins'],hero['coins'])
        with self.assertRaises(ValueError):self.say(session,'Забираю мелочь')

    def test_persuasion_roll_has_real_help_and_aid_effect(self):
        session=self.session()
        self.say(session,'Помоги мне открыть сундук')
        pending=self.say(session,'Бросок на убеждение')
        self.assertEqual(pending['pending_check']['skill'],'diplomacy')
        self.assertEqual(pending['pending_check']['objective'],'help_chest')
        state=self.roll_pending(session,pending,20)
        self.assertTrue(state['npc_help']['stablehand'])
        self.decision.update(action='move',destination='store',location_after='store')
        state=self.say(session,'Идём в кладовую')
        self.assertEqual(state['npc_locations']['stablehand'],'store')
        self.decision.update(action='check',destination='',skill='athletics',objective='book')
        self.combat.roll_die=lambda sides:20
        base=next(a for a in self.roster.read(self.room)['characters'] if a['id']==self.actor)['sheet_statistics']['athletics']
        state=self.say(session,'Поддеваю сундук, конюх помогает')
        self.assertEqual(state['pending_check']['aid']['bonus'],2)
        self.assertEqual(state['pending_check']['modifier'],base+2)
        state=self.roll_pending(session,state,20)
        self.assertTrue(state['book'])
        self.assertEqual(state['history'][-1]['check']['aid']['bonus'],2)

    def test_usual_skill_names_and_implicit_sense_motive_are_understood(self):
        for text in ('бросок на убеждение','убедить пытаюсь','пытаюсь уговорить'):
            self.assertEqual(requested_check(text),'diplomacy')
        self.assertEqual(requested_check('я хочу проверить, не подозревает что то мой герой'),'perception')
