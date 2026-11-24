"""Opt-in real free-model player journeys, in a separate persistent evaluation database.

PYTHONPATH=src python3 release_tests/live_action_plans.py --live --case all
No user game is read or changed. Each HTTP/model request is metered in the eval DB.
"""
import argparse
from pathlib import Path
import json
import time
from uuid import uuid4
from iirpg.server import ChatHTTPServer
from iirpg.application import ChatService
from iirpg.dice import DiceTool


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--live',action='store_true',required=True)
    parser.add_argument('--model',help='Explicit free session model override')
    parser.add_argument('--reasoning',action='store_true')
    parser.add_argument('--reasoning-tokens',type=int)
    parser.add_argument('--case',choices=['book','gate','social','boundaries','open','all'],default='all')
    args=parser.parse_args()
    if args.model and not (args.model.endswith(':free') or args.model=='openrouter/free'):
        parser.error('Only free models are allowed.')
    if args.reasoning_tokens is not None and not 128<=args.reasoning_tokens<=2048:parser.error('Reasoning budget must be 128–2048.')
    path=Path('.local') / ('action-eval-'+str(int(time.time()))+'.sqlite3')
    server=ChatHTTPServer(('127.0.0.1',0),path)
    if args.model:server.ai.config['session_model']=args.model
    if args.reasoning or args.reasoning_tokens is not None:server.ai.config['session_reasoning']=True
    if args.reasoning_tokens is not None:server.ai.config['session_reasoning_tokens']=args.reasoning_tokens
    report={'model':server.ai.config.get('session_model',server.ai.config['model']),'database':str(path),'cases':[]}
    def game(identifier):
        hero=server.games.heroes.list()['heroes'][0]['id']
        room=server.games.execute({'action':'create','scenario':identifier,'hero_id':hero,'name':'Проверка игроком'},str(uuid4()))['room_id']
        return room,server.adventure.read(room)['character_id']
    def turn(room,actor,text):
        state=server.adventure.read(room)
        state=server.adventure.execute(room,{'revision':state['revision'],'character_id':actor,'text':text},str(uuid4()))
        row={'player':text,'entry':state['history'][-1],'plan':state.get('plan'),'pending':state.get('pending_check')}
        report['cases'][-1]['turns'].append(row)
        print(json.dumps({'player':text,'gm':row['entry']['gm'],'plan_status':state.get('plan',{}).get('status')},ensure_ascii=False),flush=True)
        return state
    def roll(room,state,natural=20):
        pending=state['pending_check']
        ChatService(server.service.store,DiceTool(roll_die=lambda sides:natural)).send(room,{'sender':'first','type':'tool.call','tool':'dice.roll','request_id':pending['roll_request_id'],'offer_id':pending['offer_id']})
        result=server.adventure.read(room)
        report['cases'][-1]['turns'].append({'dice':natural,'entry':result['history'][-1]})
        print(result['history'][-1]['gm'],flush=True)
        return result
    def run(name,fn):
        report['cases'].append({'name':name,'turns':[]})
        try:fn();report['cases'][-1]['passed']=True
        except (AssertionError,ValueError,KeyError) as error:
            report['cases'][-1].update(passed=False,error=str(error));print('FAIL '+name+': '+str(error),flush=True)
        Path('.local/action-plan-playthrough.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
        path.with_suffix('.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
    def book():
        room,actor=game('missing-book')
        state=turn(room,actor,'Где искать книгу? Пока никуда не иду.')
        assert state['location']=='hall' and 'pending_check' not in state
        state=turn(room,actor,'Убираю оружие и щит, иду в кладовую, беру лом и держу его в руке.')
        assert state['location']=='store',state['history'][-1]['gm']
        state=turn(room,actor,'Вскрываю сундук ломом и забираю книгу.')
        assert state.get('pending_check',{}).get('skill')=='athletics',state['history'][-1]['gm']
        state=roll(room,state);assert state['book'],state['history'][-1]['gm']
        state=turn(room,actor,'Возвращаюсь к хозяйке в зал и отдаю ей найденную книгу.')
        assert state['completed'],state['history'][-1]['gm']
        items=server.roster.read(room)['characters'][0]['items']
        assert not any(i['name']=='Дорожная книга' for i in items)
    def gate():
        room,actor=game('watch-post')
        state=turn(room,actor,'Как можно открыть калитку? Это вопрос, пока ничего не делаю.')
        assert state['location']=='road' and not state['world']['entities']['gate']['open']
        state=turn(room,actor,'Убираю оружие и щит. Беру железный рычаг в руку, выламываю калитку, иду в башню и беру сигнальный рожок.')
        assert 'pending_check' in state,state['history'][-1]['gm']
        state=roll(room,state)
        assert state['world']['entities']['horn']['owner']==actor,state['history'][-1]['gm']
        state=turn(room,actor,'Возвращаюсь к сторожу на дорогу и отдаю ему рожок.')
        assert state['world']['quests']['signal'].get('completed'),state['history'][-1]['gm']
    def social():
        room,actor=game('missing-book')
        state=turn(room,actor,'Иду во двор. Спокойно знакомлюсь с конюхом, хочу расположить его к себе.')
        assert state.get('pending_check',{}).get('prepared_action',{}).get('rule')=='make_impression',state['history'][-1]['gm']
        state=roll(room,state)
        state=turn(room,actor,'Прошу помочь мне открыть сундук в кладовой.')
        assert state.get('pending_check',{}).get('prepared_action',{}).get('rule')=='request',state['history'][-1]['gm']
        state=roll(room,state)
        state=turn(room,actor,'Идём вместе к сундуку в кладовую. Пусть он подготовится помогать мне поддеть крышку, а я пробую открыть её силой.')
        assert state.get('pending_check',{}).get('aid'),state['history'][-1]['gm']
        pending=state['pending_check']
        state=turn(room,actor,'Подожди, это моя Атлетика или конюха? Пока не бросаю.')
        assert state['pending_check']['offer_id']==pending['offer_id']
        state=server.adventure.cancel(room,pending['offer_id'])
        assert 'pending_check' not in state
    def boundaries():
        room,actor=game('missing-book')
        state=turn(room,actor,'Можно ли открыть сундук силой? Пока это только вопрос, я остаюсь в зале.')
        assert state['location']=='hall' and 'pending_check' not in state
        state=turn(room,actor,'Пробую телепортироваться в кладовую заклинанием Телепортация.')
        assert state['location']=='hall' and not state.get('active_combat') and 'pending_check' not in state
        assert state.get('plan',{}).get('status')=='blocked' or state['history'][-1]['action'] in ('question','clarify')
        state=turn(room,actor,'Иду во двор и угрожаю конюху: помоги открыть сундук, иначе пожалеешь. Хочу принудить его.')
        if 'pending_check' not in state and state['history'][-1]['action'] in ('question','clarify'):
            state=turn(room,actor,'Да. Пешком перехожу во двор к нему и целую минуту угрожаю, добиваясь помощи с сундуком в кладовой. Требую только физической помощи, а не опасного поступка.')
        assert state.get('pending_check',{}).get('prepared_action',{}).get('rule')=='coerce',state['history'][-1]['gm']
        state=roll(room,state,1)
        npc=state['world']['entities']['stablehand']
        assert actor not in npc.get('agreements',{})
        state=turn(room,actor,'Ладно, больше не угрожаю. Просто спрашиваю, что ты видел и откуда грязные следы у лестницы?')
        assert 'pending_check' not in state and state['location']=='yard'
        assert state['history'][-1]['action']=='question',state['history'][-1]['gm']
        state=turn(room,actor,'Извиняюсь, пытаюсь наладить отношения: спокойно знакомлюсь и говорю, что хочу помочь хозяйке.')
        if 'pending_check' not in state and state['history'][-1]['action'] in ('question','clarify'):
            # A real player can resolve an ambiguous third-person reference once, in the same chat.
            state=turn(room,actor,'Продолжаю говорить с тобой, моим нынешним собеседником. Извиняюсь именно перед тобой, спокойно объясняю свои намерения и пытаюсь расположить тебя к себе.')
        assert state.get('pending_check',{}).get('prepared_action',{}).get('rule')=='make_impression',state['history'][-1]['gm']
        assert state['pending_check']['prepared_action']['target']=='stablehand'
        state=roll(room,state,20)
    def open_requests():
        room,actor=game('missing-book')
        state=turn(room,actor,'Хозяйка, прошу не называть моего имени другим постояльцам. Мне нужна твоя осторожность, пока я ищу сестру.')
        assert state.get('pending_check',{}).get('prepared_action',{}).get('commitment'),state['history'][-1]['gm']
        assert not state['world']['entities']['hostess'].get('services')
        state=roll(room,state)
        state=turn(room,actor,'Ещё одна просьба: встреться со мной вечером во дворе, чтобы спокойно поговорить без посторонних.')
        assert state.get('pending_check',{}).get('prepared_action',{}).get('commitment'),state['history'][-1]['gm']
        state=roll(room,state)
        assert len(state['world']['entities']['hostess']['commitments'])==2
        state=turn(room,actor,'Убираю оружие и щит, иду в кладовую и пробую открыть сундук плечом, упираясь под край крышки.')
        assert state.get('pending_check',{}).get('skill')=='athletics',state['history'][-1]['gm']
        assert state['pending_check']['dc']==state['world']['entities']['chest']['force_dc']
        state=roll(room,state)
        assert state['world']['entities']['chest']['open'] and not state['book']
        state=turn(room,actor,'Возвращаюсь к хозяйке в зал.')
        assert state['location']=='hall',state['history'][-1]['gm']
        state=turn(room,actor,'Ты помнишь две наши договорённости? Сейчас только разговариваю.')
        assert state['location']=='hall' and 'pending_check' not in state,state['history'][-1]['gm']
        assert len(state['world']['entities']['hostess']['commitments'])==2
    try:
        for name,fn in [('book',book),('gate',gate),('social',social),('boundaries',boundaries),('open',open_requests)]:
            if args.case in ('all',name):run(name,fn)
    finally:server.server_close()
    print(json.dumps({'passed':all(c['passed'] for c in report['cases']),'cases':[(c['name'],c['passed']) for c in report['cases']]},ensure_ascii=False),flush=True)
    if not all(c['passed'] for c in report['cases']):raise SystemExit(1)

if __name__=='__main__':main()
