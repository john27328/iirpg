"""Class features, validated spells, daily resources and save branches; no AI network."""
from copy import deepcopy
import json
import unittest
from uuid import uuid4
import test_party_combat as fixtures
from iirpg.adventure import AdventureStore
from iirpg.ai import AIService, load_config
from iirpg.character_sheet import CharacterRules
from iirpg.game_sessions import GameSessions
from iirpg.gm_context import describe_proposal, matches_requested_action
from iirpg.magic import prepare_spell, cast_spell, flanking
from iirpg.progression import configure, describe
from iirpg.storage import MessageConflict


class ProgressionTests(unittest.TestCase):
    setUp = fixtures.PartyCombatTests.setUp
    command = fixtures.PartyCombatTests.command
    roster_command = fixtures.PartyCombatTests.roster_command
    start = fixtures.PartyCombatTests.start
    act = fixtures.PartyCombatTests.act

    def build(self, class_id='wizard', feat='fleet', actor=None):
        return self.roster_command('configure_build', id=actor or self.actor,
                                   data={'class_id': class_id, 'general_feat': feat})

    def hero(self):
        return next(a for a in self.roster.read(self.room)['characters'] if a['id'] == self.actor)

    def save_battle(self, state):
        with self.store._connection() as c:
            c.execute('UPDATE encounters SET body=? WHERE room_id=?', (json.dumps(state), self.room))

    def test_class_and_feat_change_mechanics_without_erasing_identity_or_items(self):
        self.roster_command('add_item', character_id=self.actor, data={'name':'Верёвка','category':'gear','bulk':'L'})
        self.build('fighter', 'incredible_initiative')
        hero = self.hero()
        self.assertEqual(hero['sheet']['values']['hp_max'], 20)
        self.assertEqual(hero['sheet']['values']['perception_rank'], 'expert')
        self.assertEqual(hero['player_id'], self.owner1)
        self.assertEqual(hero['items'][0]['name'], 'Верёвка')
        battle = self.start()
        self.assertEqual(battle['fighters'][0]['attack'], 9)
        self.assertEqual(battle['fighters'][0]['initiative_bonus'], 2)
        with self.assertRaises(MessageConflict): self.build('wizard')

    def test_invalid_build_and_injected_resource_fields_leave_roster_unchanged(self):
        before = self.roster.read(self.room)
        for data in ({'class_id':'invented','general_feat':'fleet'},
                     {'class_id':'wizard','general_feat':'fleet','spent':0},
                     {'class_id':'wizard','general_feat':[]},
                     {'build':{'class_id':'wizard','general_feat':'fleet'}}):
            with self.assertRaises(ValueError): self.roster_command('configure_build', id=self.actor, data=data)
            self.assertEqual(self.roster.read(self.room), before)
        with self.assertRaises(ValueError): self.roster_command('update_character', id=self.actor, data={'build':{}})

    def test_spell_spends_once_and_survives_restart_and_next_encounter(self):
        self.build()
        battle = self.start(); target = battle['fighters'][2]['entity_id']
        # Guarantee a visible, nearby target independently of authored spawn positions.
        battle['fighters'][0]['position'] = [3, 3]; battle['fighters'][2]['position'] = [4, 3]
        self.save_battle(battle)
        command = {'action':'cast_spell','revision':battle['revision'],'spell_id':'force_barrage',
                   'spell_actions':3,'target_id':target}
        key = str(uuid4()); result = self.combat.execute(self.room, command, key)
        self.assertEqual(result['actions'], 0)
        self.assertEqual(result['last_spell']['damage'], 6)
        self.assertEqual(result['journal'][-1]['action'], 'cast_spell')
        self.assertEqual(self.hero()['progression']['slots_remaining'], 1)
        rolls = list(self.rolls)
        self.assertEqual(self.combat.execute(self.room, command, key), result)
        self.assertEqual(rolls, self.rolls)
        from iirpg.roster_storage import RosterStore
        self.assertEqual(RosterStore(self.store, self.roster.rules).read(self.room)['characters'][0]['build']['spent'], 1)
        result['status'] = 'finished'; self.save_battle(result)
        self.build('fighter'); self.build('wizard')
        next_battle = self.combat.execute(self.room, {'action':'start','revision':result['revision'],'character_id':self.actor}, str(uuid4()))
        self.assertEqual(next_battle['fighters'][0]['build']['spent'], 1)

    def test_unavailable_spell_wrong_cost_wall_and_empty_slots_never_roll(self):
        self.build(); battle = self.start(); target = battle['fighters'][2]['entity_id']
        battle['fighters'][0]['position'] = [3,3]; battle['fighters'][2]['position'] = [4,3]
        self.save_battle(battle)
        before = list(self.rolls)
        for spell, cost, victim in [('fireball',2,target),('force_barrage',True,target),('daze',3,target),('daze',2,self.second),('force_barrage',1,None)]:
            with self.assertRaises(ValueError): self.act(battle,'cast_spell',spell_id=spell,spell_actions=cost,target_id=victim)
            self.assertEqual(self.combat.read(self.room), battle)
            self.assertEqual(self.rolls, before)
        battle['fighters'][0]['build']['spent'] = 2
        with self.assertRaises(ValueError): prepare_spell(battle, self.scene.read(self.room), 'force_barrage', 1, target)
        from iirpg.combat_actions import available_actions
        menu=available_actions(battle,self.scene.read(self.room),self.roster.read(self.room),[4,3])
        self.assertFalse(any(a['command'].get('spell_id')=='force_barrage' for a in menu['selected']))
        self.assertTrue(any(a['command'].get('spell_id')=='daze' for a in menu['selected']))
        # A solid wall separates the two squares, despite range being sufficient.
        scene = self.scene.read(self.room); scene['map']['rows'] = ['.....','..#..','..#..','..#..','.....']
        battle['fighters'][0]['position']=[1,2]; battle['fighters'][2]['position']=[3,2]
        with self.assertRaises(ValueError): prepare_spell(battle,scene,'daze',2,target)

    def test_daze_basic_save_degrees_and_stunned_consumes_next_turn_action(self):
        self.build(); battle = self.start(); battle['mapped'] = False
        target = battle['fighters'][2]; target['will'] = 0; target['hp']=target['max_hp']=100
        for natural, expected, stunned in [(1,12,True),(10,6,False),(18,3,False),(20,0,False)]:
            trial = deepcopy(battle)
            cast_spell(trial,None,'daze',2,target['entity_id'],lambda sides:natural if sides==20 else 6)
            self.assertEqual(trial['last_spell']['damage'], expected)
            self.assertEqual(bool(trial['fighters'][2].get('stunned')), stunned)
            self.assertEqual(trial['fighters'][0]['build']['spent'], 0)
        trial = deepcopy(battle); trial['turn']=1; trial['fighters'][2]['stunned']=1
        self.combat.end_turn(trial)
        self.assertEqual(trial['turn'], 2); self.assertEqual(trial['actions'], 2)
        self.assertNotIn('stunned',trial['fighters'][2])

    def test_contextual_buttons_and_review_preserve_exact_spell_parameters(self):
        self.build(); battle = self.start()
        battle['fighters'][0]['position']=[3,3]; battle['fighters'][2]['position']=[4,3]; self.save_battle(battle)
        menu = self.combat.actions(self.room,battle['revision'],[4,3])
        spells = [a for a in menu['selected'] if a['command']['action']=='cast_spell']
        self.assertEqual(len(spells),4)
        requested = spells[0]['command']; proposed={'effect':requested['action'],**{k:v for k,v in requested.items() if k!='action'}}
        proposal = describe_proposal(battle,self.scene.read(self.room),self.roster.read(self.room),proposed)
        self.assertEqual(proposal['command']['spell_id'],'force_barrage')
        self.assertTrue(matches_requested_action(requested,proposed))
        self.assertFalse(matches_requested_action({**requested,'spell_actions':2},proposed))
        pending = self.act(battle,'intent',text='Силовой залп',review=True,requested_action=requested,target_id=requested['target_id'])
        result = self.act(pending,'execute_intent',intent_id=pending['intents'][-1]['id'],summary='Цель видна, ячейка есть.',**proposed)
        self.assertEqual(result['last_spell']['spell_id'],'force_barrage')

    def test_rogue_flanking_sneak_damage_and_agile_map(self):
        self.build('rogue'); battle = self.start(); scene = self.scene.read(self.room)
        battle['fighters'][2]['hp']=battle['fighters'][2]['max_hp']=100
        battle['fighters'][0]['position']=[3,3]; battle['fighters'][2]['position']=[4,3]; battle['fighters'][1]['position']=[5,3]
        self.assertTrue(flanking(battle,scene,0,2))
        self.combat.roll_die=lambda sides:15 if sides==20 else 2
        self.combat.strike(battle,battle['fighters'][2]['entity_id'],scene)
        self.assertEqual(battle['last_attack']['precision'],2)
        self.combat.strike(battle,battle['fighters'][2]['entity_id'],scene)
        self.assertEqual(battle['last_attack']['map'],-4)
        battle['fighters'][1]['hp']=0
        self.assertFalse(flanking(battle,scene,0,2))

    def session(self):
        class Client:
            action='cast'; objective='light'; calls=[]
            def ready(self):return True
            def complete(client,payload,timeout):
                client.calls.append(json.loads(payload['messages'][1]['content']))
                return {'content':json.dumps({'intent':'act','reply':'','steps':[{'rule':client.action, **({'ability':client.objective} if client.action=='cast' else {})}]})}
        client=Client(); config=load_config();config['session_agent']=False
        return AdventureStore(AIService(self.combat,self.scene,config,client)),client

    def say(self,session,text='Создаю свет',key=None):
        return session.execute(self.room,{'revision':session.read(self.room)['revision'],'character_id':self.actor,'text':text},key or str(uuid4()))

    def test_chat_cantrips_rest_and_checkpoint_preserve_resources(self):
        self.build(); session,client=self.session(); games=GameSessions(session)
        self.say(session)
        self.assertTrue(self.hero()['build']['light'])
        self.assertEqual(client.calls[-1]['character_build']['name'],'Маг')
        from iirpg.character_resources import save_character
        hero=self.hero();hero['build']['spent']=1
        with self.store._connection() as c:save_character(c,self.room,hero)
        games.execute({'action':'register','room_id':self.room},str(uuid4()))
        games.execute({'action':'save','room_id':self.room},str(uuid4()))
        with self.store._connection() as c:
            from iirpg.character_resources import save_character
            hero=self.hero();hero['build']['spent']=2;hero['sheet']['values']['hp_current']=10
            save_character(c,self.room,hero)
        client.action='rest';client.objective=''
        state=self.say(session,'Отдыхаю 8 часов и готовлю заклинания')
        self.assertEqual(self.hero()['progression']['slots_remaining'],2)
        self.assertEqual(self.hero()['sheet']['values']['hp_current'],12)
        self.assertFalse(self.hero()['build']['light'])
        self.assertEqual(state['elapsed_hours'],24)
        games.execute({'action':'load','room_id':self.room},str(uuid4()))
        self.assertTrue(self.hero()['build']['light'])
        self.assertEqual(self.hero()['progression']['slots_remaining'],1)
        self.assertNotIn('elapsed_hours',session.read(self.room))

    def test_rest_requires_safe_place_and_build_change_preserves_story_wounds(self):
        self.build('fighter'); session,client=self.session()
        state=session.read(self.room);state.update(character_id=self.actor,hero_hp=12,location='store')
        with self.store._connection() as c:
            c.execute('INSERT INTO adventures VALUES(?,?) ON CONFLICT(room_id) DO UPDATE SET body=excluded.body',(self.room,json.dumps(state)))
        client.action='rest';client.objective=''
        self.assertIn('небезопасно', self.say(session,'Отдыхаю')['history'][-1]['gm'])
        self.build('wizard')
        self.assertEqual(self.hero()['sheet']['values']['hp_current'],8)
        self.assertEqual(session.read(self.room)['hero_hp'],8)

    def test_npc_can_use_shared_build_rules_without_replacing_identity(self):
        npc=self.roster_command('create_character',data={'name':'Страж','kind':'npc'})['entity_id']
        self.build('fighter',actor=npc)
        character=next(a for a in self.roster.read(self.room)['characters'] if a['id']==npc)
        self.assertEqual(character['kind'],'npc')
        self.assertEqual(character['name'],'Страж')
        self.assertEqual(character['sheet']['profile'],'pc')

    def test_non_caster_cannot_get_magic_from_model(self):
        self.build('fighter'); session,client=self.session()
        self.assertIn('способности нет', self.say(session)['history'][-1]['gm'])
        self.assertFalse(self.hero()['build']['light'])
