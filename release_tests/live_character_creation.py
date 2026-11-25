"""Opt-in free-model guidance for a manually filled card, using an isolated HTTP server."""
import argparse
from datetime import datetime, timezone
import hashlib
from pathlib import Path
from uuid import uuid4

from live_multiplayer import Playtest
from iirpg.progression import RULES


class CreationPlaytest(Playtest):
    def ask(self,text):
        state=self.server.games.heroes.read(self.draft)
        command={'revision':state['revision'],'text':text};key=str(uuid4())
        status,result=self.request('/api/characters/drafts/'+self.draft,command,key=key)
        self.case['turns'].append({'text':text,'http_status':status,'error':result.get('error'),
            'proposal':result.get('proposal'),'reply':result.get('conversation',[{}])[-1].get('text')})
        self.check('assistant accepts question',status==200,result.get('error'));self.save()
        if status!=200:raise ValueError(result.get('error'))
        used=self.server.ai.status()['requests_today']
        replay_status,replay=self.request('/api/characters/drafts/'+self.draft,command,key=key)
        self.check('reply replay spends no extra request',replay_status==200 and replay==result and self.server.ai.status()['requests_today']==used)
        print(text,'=>',self.case['turns'][-1]['reply'],flush=True)
        return result

    def play(self):
        self.case={'name':'manual card with free AI guidance','turns':[],'checks':[]};self.report['cases'].append(self.case)
        status,new=self.request('/api/characters',{'action':'new_draft'})
        if status!=200:raise ValueError(new)
        self.draft=new['draft_id']
        choice={'name':'Нора','class_id':'wizard','general_feat':'fleet','description':'Исследовательница древних рукописей.',
            'edicts':'Не бросать друзей в опасности.','anathema':'Предавать доверие.'}
        status,state=self.request('/api/characters',{'action':'update_draft','id':self.draft,'revision':0,'proposal':choice})
        self.check('manual card works without model request',status==200 and self.server.ai.status()['requests_today']==0)
        self.check('wizard statistics are computed by engine',state['preview']['attributes']==RULES['classes']['wizard']['attributes'])
        state=self.ask('Я заполнил карточку, но правил не знаю. Объясни, что даёт выбранный перк и чем будет полезен этот герой. Мои выборы пока не меняй.')
        self.check('advice preserves manual choices',all(state['proposal'].get(k)==v for k,v in choice.items()))
        state=self.ask('Я туплю с характеристиками. Можно просто выставить силу 99? Объясни простыми словами. Не меняй имя, класс, перк и убеждения.')
        self.check('asking for cheated stats cannot change rules',state['preview']['attributes']==RULES['classes']['wizard']['attributes'])
        state=self.ask('Теперь хочу играть плутом. Поменяй только класс, а имя, перк, описание, принципы и табу сохрани.')
        self.check('class correction keeps remaining fields',state['proposal']['class_id']=='rogue' and all(state['proposal'].get(k)==v for k,v in choice.items() if k!='class_id'))
        self.check('rogue preview is recomputed',state['preview']['attributes']==RULES['classes']['rogue']['attributes'])
        state=self.ask('Поменяй только имя на Ольга. Всё остальное оставь как есть.')
        self.check('rename preserves class feat and beliefs',state['proposal']['name']=='Ольга' and state['proposal']['class_id']=='rogue' and state['proposal']['general_feat']=='fleet' and state['proposal']['edicts']==choice['edicts'] and state['proposal']['anathema']==choice['anathema'])
        status,saved=self.request('/api/characters',{'action':'save','id':self.draft,'revision':state['revision']})
        self.check('ready card saves',status==200)
        status,game=self.request('/api/games',{'action':'create','campaign':'solo-evening','name':'Созданный герой','hero_id':saved['hero_id']})
        self.check('saved hero starts a game',status==200)
        self.room=game['room_id'];self.root='/api/rooms/'+self.room
        before=self.server.roster.read(self.room);hero=before['characters'][0]
        status,_=self.request(self.root+'/roster',{'action':'update_character','revision':before['revision'],'id':hero['id'],'data':{'attributes':{'strength':9}}})
        self.check('even owner cannot turn stats in game',status==403 and self.server.roster.read(self.room)==before)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live',action='store_true',required=True)
    parser.add_argument('--model');parser.add_argument('--max-requests',type=int,default=15)
    parser.add_argument('--output',type=Path);args=parser.parse_args()
    if args.model and not(args.model.endswith(':free') or args.model=='openrouter/free'):parser.error('Only free models allowed')
    if not 1<=args.max_requests<=100:parser.error('Use 1..100 requests')
    output=args.output or Path('.local')/('creator-live-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'-'+uuid4().hex[:8]+'.json')
    output.parent.mkdir(parents=True,exist_ok=True)
    if output.exists() or output.with_suffix('.sqlite3').exists():parser.error('Choose a new output')
    test=CreationPlaytest(output,args.model,args.max_requests)
    if args.model:test.server.ai.config['model']=args.model
    test.report['model']=test.server.ai.config['model']
    test.report['source_hashes'].update({name:hashlib.sha256((Path('src/iirpg')/name).read_bytes()).hexdigest() for name in ('hero_library.py','roster_storage.py','web/hero-menu.js','web/character-sheet.js')})
    try:
        try:test.play()
        except (ValueError,KeyError,AssertionError) as error:test.case['error']=str(error);print('ERROR:',error,flush=True)
    finally:test.close()
    failures=[c for case in test.report['cases'] for c in case['checks'] if not c['passed']]
    print('Report:',output,'Requests:',test.report['requests'],'Failed:',len(failures),flush=True)
    if failures or any('error' in case for case in test.report['cases']):raise SystemExit(1)


if __name__=='__main__':main()
