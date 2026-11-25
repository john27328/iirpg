"""Opt-in solo lifecycle evaluation using real HTTP and a free model."""
import argparse
import hashlib
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from live_multiplayer import Playtest
from iirpg.world import location


class SoloPlaytest(Playtest):
    def control(self, action):
        state=self.state()
        status,result=self.request(self.root+'/session/adventure',{'action':action,
            'revision':state['revision'],'character_id':state['character_id']})
        self.case['turns'].append({'operation':action,'http_status':status,'result':result.get('adventure'),'error':result.get('error')})
        self.check('control '+action,status==200,result.get('error'));self.save()
        if status!=200:raise ValueError(result.get('error'))
        return result

    def play(self):
        self.case={'name':'solo evening','turns':[],'checks':[]};self.report['cases'].append(self.case)
        status,game=self.request('/api/games',{'action':'create','campaign':'solo-evening','name':'Живой сольный тест'})
        if status!=200:raise ValueError(game)
        self.room=game['room_id'];self.root='/api/rooms/'+self.room
        state=self.state();hero=state['character_id']
        self.players=[{'character_id':hero,'character_name':state['world']['entities'][hero]['name']}];self.tokens=[self.owner]
        self.case['room_id']=self.room
        self.check('creation uses no AI',self.server.ai.status()['requests_today']==0)
        self.control('start');first=self.state()['world']['adventure']
        self.turn(0,'Расскажи, что случилось. Что ты уже знаешь, а что пока только подозреваешь?')
        for stage in first['stages']:
            self.turn(0,'Иду в место «'+stage['name']+'».')
            state=self.state()
            self.check('reached intended place',state['location']==stage['id'])
            features=[v['name'] for k,v in sorted(state['world']['entities'].items()) if location(state['world'],k)==stage['id'] and v['kind'] in ('item','landmark')]
            if not features:raise ValueError('No physical evidence source generated')
            self.turn(0,'Внимательно осматриваю «'+features[0]+'». Какие следы я вижу?')
        self.check('both observations make finale ready',self.state()['world']['adventure']['status']=='ready')
        self.turn(0,'Мы уже всё нашли? Что известно по фактам? Я пока не докладываю и никуда не иду.')
        self.check('question does not complete story',self.state()['world']['adventure']['status']=='ready')
        before=self.server.ai.status()['requests_today'];self.control('finish')
        self.check('finish requires no AI',self.server.ai.status()['requests_today']==before)
        self.check('explicit ending persists',self.state()['world']['adventure']['status']=='completed')
        places=set(self.state()['world']['places'])
        self.control('start');next_state=self.state()
        self.check('new story has distinct identity',next_state['world']['adventure']['id']!=first['id'])
        self.check('new story has distinct title',next_state['world']['adventure']['title']!=first['title'])
        self.check('hero and old places persist',next_state['character_id']==hero and places<=set(next_state['world']['places']))
        self.control('abandon')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live',action='store_true',required=True)
    parser.add_argument('--model');parser.add_argument('--max-requests',type=int,default=35)
    parser.add_argument('--output',type=Path)
    args=parser.parse_args()
    if args.model and not (args.model.endswith(':free') or args.model=='openrouter/free'):parser.error('Only free models allowed')
    if not 1<=args.max_requests<=100:parser.error('Use 1..100 requests')
    output=args.output or Path('.local')/('evening-live-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'-'+uuid4().hex[:8]+'.json')
    output.parent.mkdir(parents=True,exist_ok=True)
    if output.exists() or output.with_suffix('.sqlite3').exists():parser.error('Choose a new output')
    test=SoloPlaytest(output,args.model,args.max_requests)
    test.report['source_hashes'].update({name:hashlib.sha256((Path('src/iirpg')/name).read_bytes()).hexdigest() for name in ('evening.py','rules/campaigns/solo-evening.json','rules/scenarios/evening-hub.json')})
    try:
        try:test.play()
        except (ValueError,KeyError,AssertionError) as error:
            test.case['error']=str(error);print('ERROR:',error,flush=True)
    finally:test.close()
    failures=[c for case in test.report['cases'] for c in case['checks'] if not c['passed']]
    print('Report:',output,'Requests:',test.report['requests'],'Failed checks:',len(failures),flush=True)
    if failures or any('error' in c for c in test.report['cases']):raise SystemExit(1)


if __name__=='__main__':main()
