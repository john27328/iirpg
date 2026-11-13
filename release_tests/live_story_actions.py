"""Opt-in free-model regression for help, compound travel/light and authored loot.

Runs in fresh unlisted rooms and never writes the selected source game.
PYTHONPATH=src python3 release_tests/live_story_actions.py --room UUID
"""
import argparse
from copy import deepcopy
import json
from pathlib import Path
from uuid import uuid4
from iirpg.server import ChatHTTPServer
from iirpg.application import ChatService
from iirpg.dice import DiceTool


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--room',required=True);args=parser.parse_args()
    server=ChatHTTPServer(('127.0.0.1',0),Path('.local/chat.sqlite3'))
    source=server.adventure.read(args.room)
    hero=next(a for a in server.roster.read(args.room)['characters'] if a['id']==source['character_id'])
    report={'model':server.ai.config['model'],'turns':[]}
    def fork(defeated=False):
        room=server.service.store.create_room()
        def roster(action,**extra):return server.roster.execute(room,{'action':action,'revision':server.roster.read(room)['revision'],**extra},str(uuid4()))
        actor=roster('create_character',data={'name':hero['name'],'sheet':hero['sheet'],'attributes':hero['attributes']})['entity_id']
        roster('configure_build',id=actor,data={'class_id':'wizard','general_feat':'fleet'})
        # Authoritative neutral setup, not a replay of unrelated boundary-testing prose.
        history=[{'player':'Помоги открыть сундук','gm':'Конюх не спешит соглашаться, но вы можете попытаться убедить его.','action':'narrate'}] if not defeated else [{'player':'Обыскиваю конюха','gm':'После учебного боя конюх выбыл. На нём 5 медных монет.','action':'narrate'}]
        state={'revision':1,'location':'yard','book':False,'character_id':actor,'history':history,'npc_outcomes':{'stablehand':'defeated'} if defeated else {}}
        with server.service.store._connection() as c:c.execute('INSERT INTO adventures VALUES(?,?)',(room,json.dumps(state,ensure_ascii=False)))
        return room,actor
    def say(room,actor,text):
        state=server.adventure.read(room)
        state=server.adventure.execute(room,{'revision':state['revision'],'character_id':actor,'text':text},str(uuid4()))
        report['turns'].append({'text':text,'entry':state['history'][-1]});print(json.dumps(report['turns'][-1],ensure_ascii=False),flush=True)
        return state
    def roll(room,state):
        pending=state['pending_check']
        ChatService(server.service.store,DiceTool(roll_die=lambda sides:20)).send(room,{'sender':'first','type':'tool.call','tool':'dice.roll','request_id':pending['roll_request_id'],'offer_id':pending['offer_id']})
        server.adventure.read(room)
        return server.adventure.react(room,pending['offer_id'])
    try:
        room,actor=fork()
        state=say(room,actor,'Бросок на убеждение')
        assert state['pending_check']['skill']=='diplomacy' and state['pending_check']['objective']=='help_chest'
        state=roll(room,state);assert state['npc_help']['stablehand']
        state=say(room,actor,'Иду в кладовую, заклинание свет')
        assert state['location']=='store' and state['history'][-1]['route']==['yard','hall','store']
        assert server.roster.read(room)['characters'][0]['build']['light']
        assert state['npc_locations']['stablehand']=='store'
        server.combat.roll_die=lambda sides:20
        state=say(room,actor,'Поддеваю сундук силой, конюх помогает')
        assert state['pending_check']['objective']=='book' and state['pending_check']['aid']['bonus']==2
        state=roll(room,state);assert state['book']
        room,actor=fork(True)
        state=say(room,actor,'Забираю мелочь')
        assert server.roster.read(room)['characters'][0]['coins']==5
        state=say(room,actor,'Иду в кладовую ищу чем вскрыть замок')
        assert state['location']=='store'
        assert server.adventure.read(args.room)==source,'Source game changed during test'
        report['passed']=True
    finally:
        Path('.local/story-actions-playthrough.json').write_text(json.dumps(report,ensure_ascii=False,indent=2));server.server_close()
    print('PASS free-model persuasion → NPC Aid → route with real light → book; coins → routed travel; source unchanged',flush=True)


if __name__=='__main__':main()
