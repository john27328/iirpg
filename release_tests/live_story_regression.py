"""Opt-in replay of a saved story in isolated rooms; source game is read-only.

PYTHONPATH=src python3 release_tests/live_story_regression.py --room UUID
"""
from copy import deepcopy
import argparse
import json
from pathlib import Path
import re
from uuid import uuid4
from iirpg.server import ChatHTTPServer
from iirpg.application import ChatService
from iirpg.dice import DiceTool


def similarity(a,b):
    a=set(re.findall(r'\w+',a.lower()));b=set(re.findall(r'\w+',b.lower()))
    return len(a & b)/max(1,len(a | b))


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--room',required=True);args=parser.parse_args()
    server=ChatHTTPServer(('127.0.0.1',0),Path('.local/chat.sqlite3'))
    source=server.adventure.read(args.room);source_before=deepcopy(source)
    hero=next(a for a in server.roster.read(args.room)['characters'] if a['id']==source['character_id'])
    report={'model':server.ai.config['model'],'turns':[],'rooms':[]}
    def fork(history):
        room=server.service.store.create_room();report['rooms'].append(room)
        def roster(action,**extra):return server.roster.execute(room,{'revision':server.roster.read(room)['revision'],'action':action,**extra},str(uuid4()))
        actor=roster('create_character',data={k:deepcopy(hero[k]) for k in ('name','kind','attributes','sheet')})['entity_id']
        if hero.get('build'):roster('configure_build',id=actor,data={k:hero['build'][k] for k in ('class_id','general_feat')})
        for item in hero['items']:roster('add_item',character_id=actor,data={k:v for k,v in item.items() if k!='id'})
        state=deepcopy(source);state.update(character_id=actor,history=deepcopy(history),revision=len(history),location='yard')
        for key in ('pending_check','active_combat','last_combat','place','places','location_id'):state.pop(key,None)
        with server.service.store._connection() as c:c.execute('INSERT INTO adventures VALUES(?,?)',(room,json.dumps(state,ensure_ascii=False)))
        return room,actor
    def say(room,actor,text):
        before=server.adventure.read(room)
        state=server.adventure.execute(room,{'revision':before['revision'],'character_id':actor,'text':text},str(uuid4()))
        entry=state['history'][-1];report['turns'].append({'room':room,'text':text,'entry':entry})
        print(json.dumps(report['turns'][-1],ensure_ascii=False),flush=True)
        return state
    try:
        # Replay the ignored check at its original point in the conversation.
        history=source['history'];index=next(i for i,h in enumerate(history) if 'нимательност' in h.get('player','').lower())
        room,actor=fork(history[:index])
        state=say(room,actor,'хочу проверить нимательность')
        assert state['pending_check']['skill']=='perception'
        pending=state['pending_check']
        ChatService(server.service.store,DiceTool(roll_die=lambda sides:11)).send(room,{'sender':'first','type':'tool.call','tool':'dice.roll','request_id':pending['roll_request_id'],'offer_id':pending['offer_id']})
        server.adventure.read(room);server.adventure.react(room,pending['offer_id'])
        # Keep even the previous bad GM repetitions to test recovery on the next turn.
        room,actor=fork(history)
        replies=[]
        for text in ['Что ты скрываешь, почему напрягся?','Я просто хочу расспросить про книгу.','Где книга? Скажи по делу, я больше не угрожаю.']:
            state=say(room,actor,text);reply=state['history'][-1]['gm']
            assert state['history'][-1]['action']=='narrate'
            assert not any(similarity(reply,other)>0.75 for other in replies),'GM repeated a previous reply'
            replies.append(reply)
        assert any(w in replies[-1].lower() for w in ('кладов','сундук','книг')),'No substantive response about the book'
        state=say(room,actor,'кидаю в него фаербол')
        assert 'недоступно' in state['history'][-1]['gm'] and not state.get('active_combat')
        assert server.roster.read(room)['characters'][0]['build']['spent']==0
        assert server.adventure.read(args.room)==source_before,'Source game changed during test'
        report['passed']=True
    finally:
        Path('.local/story-regression.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
        server.server_close()
    print('PASS free-model perception request, post-failure dialogue recovery and unavailable spell; source unchanged',flush=True)


if __name__=='__main__':main()
