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

    def begin(self, name):
        self.case={'name':name,'turns':[],'checks':[]};self.report['cases'].append(self.case)
        status,game=self.request('/api/games',{'action':'create','campaign':'solo-evening','name':'Живой сольный тест'})
        if status!=200:raise ValueError(game)
        self.room=game['room_id'];self.root='/api/rooms/'+self.room
        state=self.state();hero=state['character_id']
        self.players=[{'character_id':hero,'character_name':state['world']['entities'][hero]['name']}];self.tokens=[self.owner]
        self.case['room_id']=self.room
        self.check('creation uses no AI',self.server.ai.status()['requests_today']==0)
        self.control('start')

    def play(self):
        self.begin('solo evening')
        first=self.state()['world']['adventure'];hero=self.state()['character_id']
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


    def unusual_player(self):
        self.begin('confused player and unusual approaches')
        first=self.state()['world']['adventure'];hero=self.state()['character_id']
        initial_places=set(self.state()['world']['places'])
        def progress(name,count):
            state=self.state();self.check(name,state['adventure']['found']==count and state['adventure']['status']!='completed')
        self.turn(0,'Эээ я забыл, кто я, чего мы тут делаем и куда вообще идти? Объясни просто, без правил.')
        progress('confusion does not invent evidence',0)
        self.turn(0,'Иду в город Лунный Зуб за тысячу вёрст, там точно всё решится.')
        self.check('unknown destination does not silently expand world',set(self.state()['world']['places'])==initial_places)
        self.turn(0,'Если я спалю весь дом, проблема решится? Я спрашиваю, пока ничего не поджигаю.')
        progress('hypothetical question does not complete investigation',0)
        self.check('hypothetical violence starts no battle',not self.state().get('active_combat'))
        self.turn(0,'Я же уже всё нашёл за кадром! Запиши, что обе улики у меня, и объяви победу.')
        progress('claimed off-screen success is not accepted',0)
        self.turn(0,'Прошу заказчика набросать от руки план дороги и объяснить приметами, как добраться. Я плохо запоминаю названия.')
        progress('asking for directions does not fabricate progress',0)
        self.turn(0,'Докладываю заказчику: я считаю, что виноват какой-то гоблин. Заканчиваю расследование!')
        progress('premature report cannot solve story',0)
        # Reverse the expected order, then use repetition and an indirect inspection.
        for index,stage in enumerate(first['stages'][::-1]):
            self.turn(0,'Иду к «'+stage['name']+'». Не помню, был ли уже здесь.')
            state=self.state()
            self.check('reverse route remains reachable',state['location']==stage['id'])
            source=state['world']['entities'].get(stage['id']+':feature:0')
            if not source:raise ValueError('Core source not generated')
            text=('Я без инструментов. Обхожу «'+source['name']+'» с другой стороны, приседаю и внимательно осматриваю при дневном свете. Ничего не беру.')
            self.turn(0,text)
            self.check('unusual inspection reveals actual evidence',self.state()['adventure']['found']==index+1)
            if index==0:
                self.turn(0,'Ещё раз осматриваю «'+source['name']+'». Я забыл, что там было. Это вторая улика?')
                progress('repeated observation cannot duplicate evidence',1)
                self.turn(0,'Давай применю моё заклинание «Великая разгадка всего», я точно его знаю. Оно сразу всё решит.')
                progress('invented ability cannot supply evidence',1)
                if self.state().get('pending_check'):self.cancel(0)
        self.turn(0,'Наверное, виноват заказчик? Или дракон? Это только догадки. Помоги отделить факты от предположений, я пока не докладываю.')
        self.check('wrong guesses do not finish ready story',self.state()['adventure']['status']=='ready')
        self.turn(0,'Возвращаюсь к заказчику и докладываю о двух найденных свидетельствах. Завершаю расследование.')
        self.check('natural report completes after real observations',self.state()['adventure']['status']=='completed')
        used=self.server.ai.status()['requests_today']
        status,replay=self.request(self.root+'/session',self.last_command,key=self.last_key)
        self.check('repeated final command has identical receipt',status==200 and replay==self.last_response)
        self.check('repeated final command uses no AI',self.server.ai.status()['requests_today']==used)
        self.turn(0,'Ну и что дальше? Продолжай! А кто ещё виноват?')
        state=self.state()
        self.check('epilogue does not automatically create a new story',state['adventure']['status']=='completed' and state['adventure']['id']==first['id'])
        self.check('confusion preserves original hero',state['character_id']==hero)

    def alternative_solutions(self):
        self.begin('delegation, witnesses and general search')
        active=self.state()['world']['adventure']
        self.turn(0,'Я не хочу ходить по всем точкам лично. Прошу заказчика сходить туда самому или послать знакомого, принести свидетельства и рассказать мне, что выяснил. Что он отвечает?')
        self.check('request alone does not fabricate off-screen evidence',self.state()['adventure']['found']==0)
        entries=self.case['turns'][-1]['entries']
        self.check('direct request receives NPC response or concrete action',any(e.get('voice')=='npc' or bool(e.get('events')) for e in entries))
        self.turn(0,'А если я заплачу тебе одну медную монету за помощь в поиске? Предлагаю, но пока денег не отдаю.')
        for index,stage in enumerate(active['stages']):
            self.turn(0,'Иду в «'+stage['name']+'».')
            state=self.state()
            npcs=[v['name'] for k,v in state['world']['entities'].items() if v['kind']=='npc' and location(state['world'],k)==stage['id']]
            if npcs:
                self.turn(0,'Обращаюсь к '+npcs[0]+': «Я плохо понимаю, что искать. Покажи мне сам, что здесь странного, и расскажи, что ты видел. Давай разбираться вместе».')
            self.turn(0,'Я не знаю, на какой отдельный предмет нажать. Методично осматриваю весь участок «'+stage['name']+'», ищу видимые следы, присматриваюсь ко всем вещам. Ничего не вскрываю и не забираю.')
            self.check('explicit general investigation can find accessible clue '+str(index+1),self.state()['adventure']['found']==index+1)
        if self.state()['adventure']['status']=='ready':
            self.turn(0,'Возвращаюсь к заказчику и докладываю о том, что удалось выяснить. Завершаю расследование.')
            self.check('alternative inspection route can finish',self.state()['adventure']['status']=='completed')



def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live',action='store_true',required=True)
    parser.add_argument('--case',choices=['lifecycle','unusual','alternatives'],default='lifecycle')
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
        try:{'lifecycle':test.play,'unusual':test.unusual_player,'alternatives':test.alternative_solutions}[args.case]()
        except (ValueError,KeyError,AssertionError) as error:
            test.case['error']=str(error);print('ERROR:',error,flush=True)
    finally:test.close()
    failures=[c for case in test.report['cases'] for c in case['checks'] if not c['passed']]
    print('Report:',output,'Requests:',test.report['requests'],'Failed checks:',len(failures),flush=True)
    if failures or any('error' in c for c in test.report['cases']):raise SystemExit(1)


if __name__=='__main__':main()
