"""Opt-in free-model journey: dialogue -> saved rumor -> generated scene -> evidence."""
import argparse
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from uuid import uuid4

from live_multiplayer import Playtest
from iirpg.world import route


class GrowthPlaytest(Playtest):
    def __init__(self, output: Path, model: str | None, budget: int) -> None:
        super().__init__(output,model,budget)
        self.report['source_hashes'].update({name:hashlib.sha256((Path('src/iirpg')/name).read_bytes()).hexdigest()
                                           for name in ('world_growth.py','session_agent.py','evening.py','rules/ai.json')})
        self.report['agent']={key:self.server.ai.config.get(key) for key in ('session_agent','session_agent_repairs')}

    def save(self) -> None:
        super().save()
        with self.server.service.store._connection() as c:
            traces=[json.loads(r[0]) for r in c.execute('SELECT body FROM ai_requests')]
        rejected=0
        for trace in traces:
            if trace.get('role','').startswith('session_verify'):
                try:rejected+=json.loads(trace.get('response',{}).get('content','{}')).get('ok') is False
                except (ValueError,TypeError):pass
        self.report['verification_rejections']=rejected
        self.report['repair_requests']=sum(t.get('role','').startswith('session_repair') for t in traces)
        self.report['reported_cost']=sum((t.get('response',{}).get('usage') or {}).get('cost',0) or 0 for t in traces)
        self.output.write_text(json.dumps(self.report,ensure_ascii=False,indent=2))

    def play(self) -> None:
        self.start('automatic_world_growth',region='river-marches')
        self.turn(0,'Мара, у меня свободный день. Помимо известных парома и моста, есть здесь неподалёку небольшое необычное место, где можно что-нибудь исследовать? Расскажи один местный слух. Пока никуда не иду.')
        hooks=list(self.state()['world'].get('story_hooks',{}).values())
        self.check('ordinary dialogue creates a saved local hook',bool(hooks))
        if not hooks:return
        hook=deepcopy(hooks[0]);site=hook['destination'];title=self.state()['world']['places'][site]['name']
        self.check('hook destination is reachable and still unexplored',self.state()['world']['places'][site]['unexplored']
                   and bool(route(self.state()['world'],'market',site)))
        self.check('rumor has a source but no verdict or evidence',hook['speaker_id']=='mara' and hook['status']=='unverified' and not hook['evidence'])
        self.request('/api/games',{'action':'save','room_id':self.room})
        self.turn(1,'Напомни как ведущий, что именно мы услышали про «'+title+'» и от кого? Пока только вспоминаю, ничего не делаю.')
        self.check('remembering does not duplicate the hook',list(self.state()['world']['story_hooks'])==[h['id'] for h in hooks])
        self.turn(0,'Мы вместе идём к «'+title+'», о котором только что рассказала Мара. Хочу осмотреть место своими глазами.')
        self.check('named improvised destination becomes an actual scene',self.state()['location']==site and not self.state()['world']['places'][site]['unexplored'])
        if self.state()['location']!=site:return
        self.check('arrival is not proof of the rumor',self.state()['world']['story_hooks'][hook['id']]['status']=='visited')
        lead=self.examine_discovery(0)
        self.check('investigation records an observation for the exact hook',bool(lead)
                   and lead['id'] in self.state()['world']['story_hooks'][hook['id']]['evidence'])
        if not lead:return
        stable=deepcopy(self.state()['world']['places'][site])
        people={k:v['name'] for k,v in self.state()['world']['entities'].items() if v.get('place')==site and v['kind']=='npc'}
        def generations():
            with self.server.service.store._connection() as c:
                rows=c.execute("SELECT body FROM ai_requests WHERE room_id=? AND json_extract(body,'$.role')='campaign_generation'",(self.room,)).fetchall()
            return sum(json.loads(json.loads(row[0])['request']['messages'][1]['content'])['seed']['name']==title for row in rows)
        count=generations()
        self.turn(1,'Возвращаемся на рыночную площадь, ничего с собой не забираем.')
        self.turn(0,'Снова идём к «'+title+'», тем же путём. Только возвращаемся, не создаём новое место.')
        self.check('return reuses the stored scene and people',self.state()['world']['places'][site]==stable
                   and all(self.state()['world']['entities'][k]['name']==name for k,name in people.items()) and generations()==count)
        self.check('exploration grants no unearned reward',[self.hero(i)['coins'] for i in range(2)]==[300,300])
        self.request('/api/games',{'action':'load','room_id':self.room})
        state=self.state()
        self.check('checkpoint restores the rumor before investigation',state['world']['places'][site]['unexplored']
                   and state['world']['story_hooks'][hook['id']]['status']=='unverified'
                   and not state['world']['story_hooks'][hook['id']]['evidence'])

    def local(self) -> None:
        self.start('unmapped_local_site',region='river-marches')
        places=set(self.state()['world']['places'])
        self.turn(0,'Ведущий, пока не иду к главным точкам региона. Что за небольшое неизвестное место я мог бы заметить прямо в окрестностях рыночной площади? Хочу выбрать что-нибудь ещё не названное — маленькое местное ответвление для исследования. Пока только присматриваюсь, никуда не перемещаюсь.')
        state=self.state();created=[key for key in state['world']['places'] if key not in places]
        self.check('ordinary world request expands geography',bool(created))
        if not created:return
        site=created[0];title=state['world']['places'][site]['name']
        self.check('new site stays within the current area',state['world']['places'][site]['parent']=='market' and state['location']=='market')
        self.turn(1,'Теперь идём вместе к «'+title+'» и осматриваем его. Не забираем ничего автоматически.')
        state=self.state()
        self.check('unmapped place becomes a persisted generated scene',state['location']==site and not state['world']['places'][site]['unexplored'])
        if state['location']!=site:return
        lead=self.examine_discovery(1)
        self.check('new scene has actionable investigation',bool(lead))
        self.check('new area grants no automatic reward',[self.hero(i)['coins'] for i in range(2)]==[300,300])


def main() -> None:
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live',action='store_true',required=True)
    parser.add_argument('--model')
    parser.add_argument('--case',choices=['all','rumor','local'],default='all')
    parser.add_argument('--max-requests',type=int,default=70)
    args=parser.parse_args()
    if args.model and not (args.model.endswith(':free') or args.model=='openrouter/free'):parser.error('Only free models allowed')
    if not 1<=args.max_requests<=100:parser.error('Use 1..100 requests')
    output=Path('.local')/('world-growth-live-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'-'+uuid4().hex[:8]+'.json')
    output.parent.mkdir(parents=True,exist_ok=True)
    test=GrowthPlaytest(output,args.model,args.max_requests)
    try:
        try:
            if args.case in ('all','rumor'):test.play()
            if args.case in ('all','local'):test.local()
        except (ValueError,KeyError,AssertionError) as error:
            test.case['error']=str(error);print('ERROR:',error,flush=True)
    finally:test.close()
    failures=[check for case in test.report['cases'] for check in case['checks'] if not check['passed']]
    print('Report:',output,'Requests:',test.report['requests'],'Failed checks:',len(failures),flush=True)
    if failures or any('error' in case for case in test.report['cases']):raise SystemExit(1)


if __name__=='__main__':main()
