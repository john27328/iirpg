import unittest,json
from uuid import uuid4
import test_gm_intents as fixtures
from iirpg.ai import AIService,load_config
from iirpg.adventure import AdventureStore
from iirpg.game_sessions import GameSessions
from iirpg.storage import MessageConflict

class GameSessionTests(unittest.TestCase):
    setUp=fixtures.GMIntentTests.setUp
    command=fixtures.GMIntentTests.command
    roster_command=fixtures.GMIntentTests.roster_command
    def games(self):return GameSessions(AdventureStore(AIService(self.combat,self.scene,load_config(),__import__('story_fixtures').Client())))
    def act(self,games,action,**extra):return games.execute({'action':action,**extra},str(uuid4()))
    def test_create_save_restore_and_delete_are_isolated_and_idempotent(self):
        games=self.games();key=str(uuid4());command={'action':'create','name':'Проба','scenario':'missing-book'}
        created=games.execute(command,key);self.assertEqual(games.execute(command,key),created)
        room=created['room_id'];initial=games.adventure.read(room)
        self.assertTrue(initial['character_id']);self.assertEqual(len(games.list()['games']),1)
        self.assertTrue(games.list()['games'][0]['has_save'])
        with self.store._connection() as c:
            changed=dict(initial,location='yard',book=True,revision=initial['revision']+1)
            changed.pop('place',None);changed.pop('places',None);changed.pop('location_id',None)
            c.execute('UPDATE adventures SET body=? WHERE room_id=?',(json.dumps(changed),room))
        other_before=self.roster.read(self.room)
        self.act(games,'load',room_id=room);loaded=games.adventure.read(room)
        self.assertEqual(loaded['location'],'hall');self.assertFalse(loaded['book']);self.assertGreater(loaded['revision'],changed['revision'])
        self.assertEqual(self.roster.read(self.room),other_before)
        before=self.store.page(room);games.adventure.read(room);self.assertEqual(self.store.page(room),before)
        self.act(games,'save',room_id=room)
        delete={'action':'delete','room_id':room};key=str(uuid4());deleted=games.execute(delete,key)
        self.assertEqual(games.execute(delete,key),deleted)
        self.assertEqual(games.list()['games'],[])
        with self.assertRaises(KeyError):games.adventure.read(room)
        self.assertEqual(self.roster.read(self.room),other_before)
    def test_register_preserves_progress_and_invalid_scenario_creates_nothing(self):
        games=self.games();before=self.roster.read(self.room)
        self.act(games,'register',room_id=self.room);self.act(games,'register',room_id=self.room)
        self.assertEqual(len(games.list()['games']),1);self.assertEqual(self.roster.read(self.room),before)
        with self.assertRaises(ValueError):self.act(games,'create',scenario='no',name='Bad')
        self.assertEqual(len(games.list()['games']),1)
    def test_checkpoint_preserves_pending_player_roll_and_deleted_usage(self):
        games=self.games();self.act(games,'register',room_id=self.room)
        client=games.adventure.ai.client
        client.complete=lambda *a:{'content':json.dumps({'intent':'act','reply':'','steps':[{'rule':'move','target':'store'},{'rule':'force_open','target':'chest'}]})}
        story=games.adventure.execute(self.room,{'revision':0,'character_id':self.actor,'text':'Поднимаю груз'},str(uuid4()))
        # This checkpoint tests an NPC reaction, so keep a witness at the check.
        with self.store._connection() as c:
            saved=json.loads(c.execute('SELECT body FROM adventures WHERE room_id=?',(self.room,)).fetchone()[0])
            saved['world']['entities']['hostess']['place']='store'
            games.adventure.save(c,self.room,saved)
        pending=story['pending_check'];self.act(games,'save',room_id=self.room)
        from iirpg.application import ChatService
        from iirpg.dice import DiceTool
        service=ChatService(self.store,DiceTool(roll_die=lambda sides:20))
        payload={'sender':'first','type':'tool.call','tool':'dice.roll','request_id':pending['roll_request_id'],'offer_id':pending['offer_id']}
        service.send(self.room,payload);self.assertNotIn('pending_check',games.adventure.read(self.room))
        client.complete=lambda *a:{'content':json.dumps({'intent':'question','reply':'Попытка завершена.','steps':[]})}
        games.adventure.react(self.room,pending['offer_id'])
        used=games.adventure.ai.status()['requests_today']
        self.act(games,'load',room_id=self.room);restored=games.adventure.read(self.room)
        self.assertEqual(restored['pending_check'],pending)
        page=self.store.page(self.room);games.adventure.read(self.room);self.assertEqual(self.store.page(self.room),page)
        self.assertTrue(any(m.get('type')=='tool.offer' for m in page['messages']))
        self.assertFalse(any(m.get('type')=='tool.result' for m in page['messages']))
        self.assertEqual(games.adventure.ai.status()['requests_today'],used)
        service.send(self.room,payload);games.adventure.read(self.room)
        branch=games.adventure.react(self.room,pending['offer_id'])
        self.assertEqual(branch['history'][-1]['action'],'reaction')
        self.assertEqual(games.adventure.ai.status()['requests_today'],used+1)
        self.act(games,'delete',room_id=self.room)
        self.assertEqual(games.adventure.ai.status()['requests_today'],used+1)
    def test_found_item_survives_checkpoint_load_without_duplication(self):
        games=self.games();room=self.act(games,'create',name='Находки',scenario='missing-book')['room_id']
        state=games.adventure.read(room)
        with self.store._connection() as c:
            state['location']='store'
            c.execute('UPDATE adventures SET body=? WHERE room_id=?',(json.dumps(state),room))
        games.adventure.ai.client.complete=lambda *a:{'content':json.dumps({'intent':'act','reply':'','steps':[{'rule':'move','target':'store'},{'rule':'take','target':'rope'}]})}
        roster=self.roster.read(room)
        hero=next(a for a in roster['characters'] if a['id']==state['character_id'])
        for item in hero['items']:
            if item.get('hands',0):
                self.roster.execute(room,{'action':'update_item','revision':self.roster.read(room)['revision'],'character_id':hero['id'],'id':item['id'],'data':{**{k:v for k,v in item.items() if k!='id'},'state':'stowed','hands':0}},str(uuid4()))
        state=games.adventure.execute(room,{'revision':state['revision'],'character_id':state['character_id'],'text':'Беру верёвку'},str(uuid4()))
        self.act(games,'save',room_id=room)
        roster=self.roster.read(room);actor=next(a for a in roster['characters'] if a['id']==state['character_id'])
        self.assertEqual(sum(i['name']=='Верёвка из кладовой' for i in actor['items']),1)
        self.roster.execute(room,{'action':'remove_item','revision':roster['revision'],'character_id':actor['id'],'id':next(i['id'] for i in actor['items'] if i['name']=='Верёвка из кладовой')},str(uuid4()))
        self.act(games,'load',room_id=room);games.adventure.read(room)
        actor=next(a for a in self.roster.read(room)['characters'] if a['id']==state['character_id'])
        self.assertEqual(sum(i['name']=='Верёвка из кладовой' for i in actor['items']),1)
        self.assertTrue(any(i['name']=='Верёвка из кладовой' for i in actor['items']))
