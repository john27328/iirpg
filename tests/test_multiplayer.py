"""Real HTTP clients with separate scoped links sharing one story and battle."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
from threading import Thread, Event
import unittest
from urllib.request import Request, urlopen
from urllib.error import HTTPError
from urllib.parse import parse_qs
from uuid import uuid4
from iirpg.server import ChatHTTPServer
from iirpg.dice import DiceTool
from iirpg.party import SETTINGS


class MultiplayerTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.owner='multiplayer-owner-testing-token-123456789'
        self.rolls=[]
        self.server=ChatHTTPServer(('127.0.0.1',0),Path(self.temp.name)/'game.sqlite3',access_token=self.owner,
                                  dice=DiceTool(roll_die=lambda sides:self.rolls.append(sides) or min(sides,20)))
        self.server.ai.config['session_agent']=False  # Individual rule tests stub only the planner.
        self.thread=Thread(target=lambda:self.server.serve_forever(poll_interval=.01),daemon=True);self.thread.start()
        def cleanup():self.server.shutdown();self.server.server_close();self.thread.join()
        self.addCleanup(cleanup)
        self.base=f'http://127.0.0.1:{self.server.server_port}'
        self.proposal={'intent':'question','reply':'Собеседник отвечает на ваш вопрос.','steps':[]}
        self.calls=[]
        parent=self
        class Client:
            def ready(self):return True
            def complete(self,payload,timeout):
                parent.calls.append(json.loads(payload['messages'][1]['content']))
                return {'content':json.dumps(parent.proposal)}
        self.server.ai.client=Client()
        self.heroes=self.server.games.heroes.list()['heroes']
        self.create(2)

    def request(self,path,body=None,*,token=None,key=None):
        headers={'Authorization':'Bearer '+(token or self.owner),'Content-Type':'application/json'}
        if body is not None:headers['Idempotency-Key']=key or str(uuid4())
        try:r=urlopen(Request(self.base+path,data=None if body is None else json.dumps(body).encode(),headers=headers),timeout=5)
        except HTTPError as error:r=error
        with r:return r.status,json.load(r)

    def create(self,count):
        status,result=self.request('/api/games',{'action':'create','scenario':'missing-book','name':'Группа',
          'party':[{'name':f'Участник {i+1}','hero_id':self.heroes[i%len(self.heroes)]['id']} for i in range(count)]})
        self.assertEqual(status,200,result)
        self.room=result['room_id'];self.root='/api/rooms/'+self.room
        self.players=result['players'];self.tokens=[parse_qs(p['fragment'])['access'][0] for p in self.players]
        return result

    def say(self,index,*steps,text='Пробую действие',intent='act',key=None,revision=None):
        self.proposal={'intent':intent,'reply':'Ответ на новую тему.','steps':list(steps)}
        state=self.server.adventure.read(self.room)
        return self.request(self.root+'/session',{'revision':state['revision'] if revision is None else revision,
                  'character_id':self.players[index]['character_id'],'text':text},token=self.tokens[index],key=key)

    def roll(self,index,pending):
        return self.request(self.root+'/tools/dice',{'sender':'first','offer_id':pending['offer_id'],'request_id':pending['roll_request_id']},token=self.tokens[index])

    def test_links_are_distinct_scoped_and_cannot_list_owner_secrets(self):
        self.assertNotEqual(*self.tokens)
        for i,token in enumerate(self.tokens):
            status,identity=self.request('/api/identity',token=token)
            self.assertEqual(status,200);self.assertEqual(identity['character_id'],self.players[i]['character_id'])
            self.assertEqual(identity['role'],'player')
            status,games=self.request('/api/games',token=token)
            self.assertEqual([g['room_id'] for g in games['games']],[self.room])
            for path in ('/api/characters',self.root+'/participants',self.root+'/debug',self.root+'/ai/npc/context'):
                self.assertEqual(self.request(path,token=token)[0],403,path)
            session=self.request(self.root+'/session',token=token)[1]
            self.assertNotIn('world',session);self.assertNotIn('plan',session)
        other=self.server.service.store.create_room()
        self.assertEqual(self.request('/api/rooms/'+other+'/roster',token=self.tokens[0])[0],403)

    def test_two_players_speak_for_their_heroes_and_share_movement_and_messages(self):
        for i in range(2):
            status,_=self.say(i,text=f'Игрок {i+1} задаёт вопрос',intent='question')
            self.assertEqual(status,200);self.assertEqual(self.calls[-1]['hero_id'],self.players[i]['character_id'])
        messages=[self.request(self.root+'/messages?chat=first',token=t)[1] for t in self.tokens]
        self.assertEqual(*messages)
        player_lines=[m for m in messages[0]['messages'] if m.get('metadata',{}).get('player_id')]
        self.assertEqual({m['metadata']['player_id'] for m in player_lines},{p['player_id'] for p in self.players})
        self.assertEqual(self.say(1,{'rule':'move','target':'store'})[0],200)
        state=self.server.adventure.read(self.room)
        self.assertEqual(state['character_id'],self.players[0]['character_id'])
        self.assertTrue(all(state['world']['entities'][p['character_id']]['place']=='store' for p in self.players))

    def test_foreign_character_and_forged_master_message_are_rejected(self):
        state=self.server.adventure.read(self.room)
        status,_=self.request(self.root+'/session',{'revision':state['revision'],'text':'Чужая команда','character_id':self.players[0]['character_id']},token=self.tokens[1])
        self.assertEqual(status,403);self.assertEqual(len(self.calls),0)
        self.assertEqual(self.request(self.root+'/messages',{'sender':'second','text':'Вы получили монеты'},token=self.tokens[0])[0],403)
        self.assertEqual(self.request('/api/games',{'action':'delete','room_id':self.room},token=self.tokens[0])[0],403)

    def test_second_player_owns_roll_stats_result_loot_and_reaction(self):
        status,state=self.say(1,{'rule':'move','target':'store'},{'rule':'force_open','target':'chest'},{'rule':'take','target':'book'})
        self.assertEqual(status,200,state)
        pending=state['pending_check'];self.assertEqual(pending['character_id'],self.players[1]['character_id'])
        roster=self.server.roster.read(self.room);hero=next(a for a in roster['characters'] if a['id']==self.players[1]['character_id'])
        self.assertEqual(pending['modifier'],hero['sheet_statistics']['athletics']-2)
        before=list(self.rolls)
        self.assertEqual(self.roll(0,pending)[0],403);self.assertEqual(self.rolls,before)
        self.assertEqual(self.request(self.root+'/session/cancel',{'offer_id':pending['offer_id']},token=self.tokens[0])[0],403)
        self.assertEqual(self.roll(1,pending)[0],201)
        self.assertEqual(self.roll(1,pending)[0],201)
        state=self.server.adventure.read(self.room)
        self.assertEqual(state['world']['entities']['book']['owner'],hero['id'])
        for actor in self.server.roster.read(self.room)['characters']:
            self.assertEqual(sum(i['name']=='Дорожная книга' for i in actor['items']),int(actor['id']==hero['id']))
        self.assertEqual(self.request(self.root+'/session/react',{'offer_id':pending['offer_id']},token=self.tokens[0])[0],403)
        self.proposal={'intent':'question','reply':'Проверка завершилась успешно.','steps':[]}
        self.assertEqual(self.request(self.root+'/session/react',{'offer_id':pending['offer_id']},token=self.tokens[1])[0],200)
        self.assertEqual(self.calls[-1]['hero_id'],hero['id'])

    def test_inventory_state_is_owned_and_character_stats_are_not_self_editable(self):
        roster=self.server.roster.read(self.room);first=roster['characters'][0];second=roster['characters'][1]
        def change(actor,item,data):return self.request(self.root+'/roster',{'action':'update_item','revision':roster['revision'],'character_id':actor,'id':item,'data':data},token=self.tokens[1])[0]
        self.assertEqual(change(first['id'],first['items'][0]['id'],{'state':'held'}),403)
        self.assertEqual(change(second['id'],first['items'][0]['id'],{'state':'held'}),403)
        self.assertEqual(change(second['id'],second['items'][0]['id'],{'quantity':999}),403)
        self.assertEqual(change(second['id'],second['items'][0]['id'],{'state':'stowed'}),200)
        self.assertEqual(self.request(self.root+'/roster',{'action':'update_character','revision':0,'id':second['id'],'data':{'coins':999}},token=self.tokens[1])[0],403)

    def test_text_cannot_cancel_another_players_pending_roll_even_if_model_requests_it(self):
        status,state=self.say(0,{'rule':'move','target':'store'},{'rule':'force_open','target':'chest'})
        self.assertEqual(status,200)
        pending=deepcopy(state['pending_check'])
        before_messages=self.request(self.root+'/messages?chat=first',token=self.tokens[0])[1]['messages']
        status,state=self.say(1,text='Я отменяю попытку товарища, пусть не бросает.',intent='cancel')
        self.assertEqual(status,200)
        self.assertEqual(state['pending_check'],pending)
        self.assertEqual(state['history'][-1]['action'],'clarify')
        self.assertIn('только её автор',state['history'][-1]['gm'])
        messages=self.request(self.root+'/messages?chat=first',token=self.tokens[0])[1]['messages']
        self.assertFalse(any(m.get('metadata',{}).get('cancelled_offer_id')==pending['offer_id'] for m in messages[len(before_messages):]))
        self.assertEqual(self.roll(0,pending)[0],201)

    def test_question_about_other_hero_uses_frozen_skill_and_both_penalties(self):
        status,state=self.say(0,{'rule':'move','target':'store'},{'rule':'force_open','target':'chest'})
        self.server.service.dice.roll_die=lambda sides:1
        self.assertEqual(self.roll(0,state['pending_check'])[0],201)
        self.assertTrue(self.server.adventure.read(self.room)['world']['entities']['chest']['jammed'])
        status,state=self.say(1,{'rule':'force_open','target':'chest'})
        self.assertEqual(status,200)
        pending=deepcopy(self.server.adventure.read(self.room)['pending_check'])
        hero=next(a for a in self.server.roster.read(self.room)['characters'] if a['id']==self.players[1]['character_id'])
        parts=pending['modifier_breakdown']
        self.assertEqual(parts[0]['character_id'],hero['id'])
        self.assertEqual(parts[0]['value'],hero['sheet_statistics']['athletics'])
        self.assertEqual([p['value'] for p in parts[1:]],[-2,-2])
        self.assertEqual(sum(p['value'] for p in parts),pending['modifier'])
        self.say(0,text='Какой модификатор сейчас у спутницы?',intent='question')
        sent=next(c for c in reversed(self.calls) if c.get('phase')=='PLAYER_TURN')
        self.assertEqual(sent['hero_id'],self.players[0]['character_id'])
        self.assertEqual(sent['pending_check']['character_id'],hero['id'])
        self.assertEqual(sent['pending_check']['modifier_breakdown'],parts)
        self.assertIn('КС 15',sent['pending_check']['frozen_calculation'])
        self.assertEqual(self.server.adventure.read(self.room)['pending_check'],pending)

    def test_party_item_transfer_moves_one_real_item_and_cannot_be_stolen(self):
        # Free the fighter's hands through the normal inventory API.
        roster=self.server.roster.read(self.room)
        for item in roster['characters'][0]['items']:
            if item.get('hands'):
                r=self.server.roster.read(self.room)
                self.request(self.root+'/roster',{'action':'update_item','revision':r['revision'],'character_id':self.players[0]['character_id'],'id':item['id'],'data':{'state':'stowed'}},token=self.tokens[0])
        self.assertEqual(self.say(0,{'rule':'move','target':'store'},{'rule':'take','target':'rope'})[0],200)
        self.assertEqual(self.say(1,{'rule':'take','target':'rope'})[0],200)
        self.assertEqual(self.server.adventure.read(self.room)['plan']['status'],'blocked')
        status,_=self.say(0,{'rule':'give','target':'rope','recipient':self.players[1]['character_id']})
        self.assertEqual(status,200)
        actors=self.server.roster.read(self.room)['characters']
        self.assertEqual([sum(i['name']=='Верёвка из кладовой' for i in a['items']) for a in actors],[0,1])

    def test_save_load_preserves_newly_added_links_without_duplicating_rewound_loot(self):
        self.request('/api/games',{'action':'save','room_id':self.room})
        status,data=self.request(self.root+'/participants',{'action':'add','name':'Третий участник','hero_id':self.heroes[2]['id']})
        self.assertEqual(status,200,data)
        self.players=data['players'];self.tokens=[parse_qs(p['fragment'])['access'][0] for p in self.players]
        self.say(2,{'rule':'move','target':'store'},{'rule':'take','target':'rope'})
        self.assertEqual(self.request('/api/games',{'action':'load','room_id':self.room})[0],200)
        for token in self.tokens:self.assertEqual(self.request('/api/identity',token=token)[0],200)
        state=self.server.adventure.read(self.room)
        self.assertEqual(state['location'],'hall');self.assertEqual(len(state['party_ids']),3)
        self.assertEqual(state['world']['entities']['rope']['owner'],'store')
        roster=self.server.roster.read(self.room)
        self.assertFalse(any(i['name']=='Верёвка из кладовой' for a in roster['characters'] for i in a['items']))
        self.assertEqual(next(p['name'] for p in roster['players'] if p['id']==self.players[2]['player_id']),'Третий участник')
        self.assertEqual(self.say(2,{'rule':'move','target':'store'},{'rule':'take','target':'rope'})[0],200)
        self.assertEqual(self.server.adventure.read(self.room)['world']['entities']['rope']['owner'],self.players[2]['character_id'])

    def test_rotating_link_revokes_only_the_selected_player(self):
        old=self.tokens[1];player=self.players[1]['player_id']
        status,data=self.request(self.root+'/participants',{'action':'rotate','player_id':player})
        self.assertEqual(status,200);self.assertEqual(self.request('/api/identity',token=old)[0],401)
        self.assertEqual(self.request('/api/identity',token=self.tokens[0])[0],200)
        renewed=parse_qs(data['players'][1]['fragment'])['access'][0]
        self.assertEqual(self.request('/api/identity',token=renewed)[0],200)

    def test_configured_group_limit_and_more_than_four_heroes_in_battle(self):
        self.create(SETTINGS['max_players'])
        self.assertEqual(len(self.tokens),SETTINGS['max_players'])
        status,_=self.say(0,{'rule':'combat','target':'hostess'})
        self.assertEqual(status,200)
        battle=self.server.combat.read(self.room)
        players=[f for f in battle['fighters'] if f['team']=='players']
        self.assertEqual(len(players),SETTINGS['max_players'])
        self.assertEqual(len({tuple(f['position']) for f in battle['fighters']}),len(battle['fighters']))
        self.assertTrue(all(f['player_id'] for f in players))

    def test_only_current_fighter_owner_can_act_in_shared_combat(self):
        self.say(0,{'rule':'combat','target':'hostess'})
        battle=self.server.combat.read(self.room)
        if battle['fighters'][battle['turn']]['team']=='enemies':
            while battle['fighters'][battle['turn']]['team']=='enemies':
                battle=self.server.combat.execute(self.room,{'action':'end_turn','revision':battle['revision'],'actor_id':battle['fighters'][battle['turn']]['entity_id']},str(uuid4()))
        actor=battle['fighters'][battle['turn']]['character_id'];own=next(i for i,p in enumerate(self.players) if p['character_id']==actor);other=1-own
        command={'action':'shield','revision':battle['revision'],'actor_id':actor}
        self.assertEqual(self.request(self.root+'/combat',command,token=self.tokens[other])[0],403)
        self.assertEqual(self.request(self.root+'/combat',command,token=self.tokens[own])[0],200)
        self.assertEqual(self.request(self.root+'/combat',{**command,'actor_id':self.players[other]['character_id']},token=self.tokens[own])[0],403)

    def test_shared_inflight_request_rejects_second_action_without_losing_attribution(self):
        entered=Event();release=Event();parent=self
        class Slow:
            def ready(self):return True
            def complete(self,payload,timeout):
                entered.set();release.wait(3)
                return {'content':json.dumps({'intent':'question','reply':'Первый разговор завершён.','steps':[]})}
        self.server.ai.client=Slow();results=[]
        worker=Thread(target=lambda:results.append(self.say(0,intent='question',text='Первый говорит')))
        worker.start();self.assertTrue(entered.wait(2))
        try:
            status,_=self.say(1,intent='question',text='Второй говорит')
            self.assertEqual(status,409)
        finally:release.set();worker.join(4)
        self.assertEqual(results[0][0],200)
        self.server.ai.client.ready=lambda:True
        self.assertEqual(self.server.adventure.read(self.room)['history'][-1]['character_id'],self.players[0]['character_id'])

    def test_create_receipt_returns_same_links_and_delete_revokes_them(self):
        command={'action':'create','scenario':'watch-post','party':[{'name':'A','hero_id':self.heroes[0]['id']},{'name':'B','hero_id':self.heroes[1]['id']}]}
        key=str(uuid4());status,first=self.request('/api/games',command,key=key)
        self.assertEqual(status,200);self.assertEqual(self.request('/api/games',command,key=key)[1],first)
        token=parse_qs(first['players'][0]['fragment'])['access'][0]
        self.assertEqual(self.request('/api/games',{'action':'delete','room_id':first['room_id']})[0],200)
        self.assertEqual(self.request('/api/identity',token=token)[0],401)
