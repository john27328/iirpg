"""Game heroes cannot be rebuilt through either owner or invitation HTTP APIs."""
import unittest
import json
from uuid import uuid4
import test_multiplayer as fixture


class GameCharacterLockTests(unittest.TestCase):
    setUp=fixture.MultiplayerTests.setUp
    create=fixture.MultiplayerTests.create
    request=fixture.MultiplayerTests.request

    def test_owner_cannot_modify_stats_class_identity_or_replace_game_hero(self):
        before=self.server.roster.read(self.room);hero=self.players[0]['character_id']
        attempts=[{'action':'update_character','id':hero,'data':data} for data in (
            {'attributes':{'strength':9}},{'sheet':{'values':{'hp_current':99,'level':20}}},
            {'coins':99999},{'kind':'npc'},{'name':'Обход запрета'})]
        attempts += [{'action':'configure_build','id':hero,'data':{'class_id':'wizard','general_feat':'fleet'}},
            {'action':'delete_character','id':hero},
            {'action':'create_character','data':{'name':'Сверхгерой','kind':'pc','attributes':{'strength':9}}}]
        for attempt in attempts:
            with self.subTest(attempt=attempt):
                status,result=self.request(self.root+'/roster',{'revision':before['revision'],**attempt})
                self.assertEqual(status,403,result)
                self.assertEqual(self.server.roster.read(self.room),before)
        self.assertTrue(before['is_game'])

    def test_invited_player_cannot_change_stats_but_can_manage_own_equipment(self):
        before=self.server.roster.read(self.room);hero=self.players[0]['character_id']
        status,_=self.request(self.root+'/roster',{'revision':before['revision'],'action':'update_character','id':hero,
            'data':{'attributes':{'strength':9}}},token=self.tokens[0])
        self.assertEqual(status,403)
        actor=next(a for a in before['characters'] if a['id']==hero)
        held=next(item for item in actor['items'] if item.get('hands'))
        status,result=self.request(self.root+'/roster',{'revision':before['revision'],'action':'update_item','character_id':hero,
            'id':held['id'],'data':{'state':'stowed','hands':0}},token=self.tokens[0])
        self.assertEqual(status,200,result)
        after=self.server.roster.read(self.room)
        self.assertEqual(next(a for a in after['characters'] if a['id']==hero)['attributes'],actor['attributes'])

    def test_npc_stats_and_manual_spawning_are_also_locked_in_game(self):
        npc_id=str(uuid4())
        npc=self.server.roster.rules.character({'name':'Свидетель','kind':'npc'},[],self.server.roster.character_rules)
        with self.server.service.store._connection() as c:
            c.execute('INSERT INTO characters VALUES(?,?,?,?)',(npc_id,self.room,None,json.dumps(npc)))
        before=self.server.roster.read(self.room)
        for attempt in ({'action':'update_character','id':npc_id,'data':{'attributes':{'wisdom':-5}}},
                        {'action':'create_character','data':{'name':'Новый свидетель','kind':'npc'}}):
            status,result=self.request(self.root+'/roster',{'revision':before['revision'],**attempt})
            self.assertEqual(status,403,result)
        self.assertEqual(self.server.roster.read(self.room),before)
