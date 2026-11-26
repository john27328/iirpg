"""Opt-in calibration of the real free verifier on authored valid and invalid candidates."""
import argparse
from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
from uuid import uuid4

from live_world_growth import GrowthPlaytest
from iirpg.action_rules import validate_proposal
from iirpg.session_agent import SessionAgent, AgentRejected
from iirpg.session_context import context
from iirpg import story_narration


def main() -> None:
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live',action='store_true',required=True)
    args=parser.parse_args()
    output=Path('.local')/('verifier-live-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'-'+uuid4().hex[:8]+'.json')
    output.parent.mkdir(parents=True,exist_ok=True)
    test=GrowthPlaytest(output,None,20)
    test.server.ai.config['session_agent_repairs']=0
    test.report['agent']['session_agent_repairs']=0
    test.report['limitations'].append('Authored candidates test reviewer discrimination; they are not generated game turns.')
    try:
        test.start('verifier_calibration',region='river-marches')
        state=test.state()
        with test.server.service.store._connection() as c:
            actor,_=test.server.adventure.actor(c,test.room,test.players[0]['character_id'])
        def plan(text):return context(state,actor,text)
        def narration(text):
            trial=deepcopy(state)
            trial['history'].append({'player':text,'gm':'','action':'question','voice':'npc','world_request':True})
            return json.loads(story_narration.payload(trial,actor,test.server.ai.config)['messages'][1]['content'])
        site={'name':'Тихая лодочная','premise':'Маленький лодочный сарай у рыночной протоки.',
              'rumor':'Мара слышала о брошенных мешках у сарая.','question':'Что за мешки оставлены у сарая?','speaker_id':'mara'}
        cases=[
            ('valid ordinary question','plan',plan('Мара, здравствуйте.'),
             {'intent':'question','reply':'','steps':[],'voice':'npc','interlocutor':'mara','world_request':False},True),
            ('valid automatic route then inspection','plan',plan('Идём к старой дозорной башне и осматриваем её.'),
             {'intent':'act','reply':'Герои идут к башне и осматривают её.','steps':[{'rule':'move','target':'watchtower'},{'rule':'look','target':'watchtower'}]},True),
            ('unrequested purchase','plan',plan('Мара, здравствуйте.'),
             {'intent':'act','reply':'Покупаю верёвку.','steps':[{'rule':'buy','target':'travel_rope'}]},False),
            ('wrong purchase target','plan',plan('Мара, покупаю медную кружку за двадцать медных.'),
             {'intent':'act','reply':'Покупаю кружку.','steps':[{'rule':'buy','target':'travel_rope'}]},False),
            ('valid new saved seed','narration',narration('Мара, какой местный слух можно проверить?'),
             {'reply':'Мара рассказывает о Тихой лодочной у протоки: по слухам, там оставлены мешки. Можно посмотреть, что это за груз.',
              'world_additions':[site]},True),
            ('unregistered destination','narration',narration('Мара, какой местный слух можно проверить?'),
             {'reply':'Мара говорит: на севере у площади есть Забытая пристань. Сходи туда, там найдёшь тайный склад.',
              'world_additions':[]},False),
            ('speech invented for another player','narration',narration('Мара, здравствуйте.'),
             {'reply':test.players[1]['character_name']+' говорит: «Я сама пойду к башне и вскрою дверь». Мара кивает.',
              'world_additions':[]},False),
            ('invented wallet','narration',narration('Мара, здравствуйте.'),
             {'reply':'Мара улыбается. Теперь у тебя 9999 медных монет.','world_additions':[]},False),
        ]
        for index,(name,stage,data,candidate,expected) in enumerate(cases):
            request={'model':test.server.ai.config['session_model'],'messages':[
                {'role':'system','content':'Кандидат проверяется отдельно.'},
                {'role':'user','content':json.dumps(data,ensure_ascii=False)}]}
            response={'content':json.dumps(candidate,ensure_ascii=False)}
            accepted=False;error=None;legitimate_rejection=False;issues=[]
            try:
                SessionAgent(test.server.adventure,test.room,str(uuid4()),state['revision']).run(
                    stage,request,response,lambda value:validate_proposal(json.loads(value['content'])) if stage=='plan' else story_narration.reply(value))
                accepted=True
            except ValueError as failure:
                error=str(failure)
                legitimate_rejection=isinstance(failure,AgentRejected)
                if legitimate_rejection:issues=failure.issues
            test.check(name,accepted if expected else legitimate_rejection,
                       {'expected_accept':expected,'accepted':accepted,'error':error,'issues':issues})
            print(name,':',accepted,error or '',flush=True)
            test.save()
        test.check('verification never mutates the world',test.state()==state)
    finally:test.close()
    failed=[check for case in test.report['cases'] for check in case['checks'] if not check['passed']]
    print('Report:',output,'Requests:',test.report['requests'],'Failed:',len(failed),flush=True)
    if failed:raise SystemExit(1)


if __name__=='__main__':main()
