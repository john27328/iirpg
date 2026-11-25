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

    def begin_preference(self,name,choice=None):
        self.case={'name':name,'turns':[],'checks':[]};self.report['cases'].append(self.case)
        status,new=self.request('/api/characters',{'action':'new_draft'})
        if status!=200:raise ValueError(new)
        self.draft=new['draft_id']
        if choice:
            status,state=self.request('/api/characters',{'action':'update_draft','id':self.draft,'revision':0,'proposal':choice})
            if status!=200:raise ValueError(state)

    def preferences(self):
        self.begin_preference('honest rogue, no premature choice')
        state=self.ask('Хочу плута по имени Соня. Она честная спасательница, не ворует и никого не убивает. Это не типичный вор! Сохрани этот образ в описании. Перк пока НЕ выбирай: сначала объясни варианты.')
        self.check('non-stereotypical rogue keeps requested class and name',state['proposal']['class_id']=='rogue' and state['proposal']['name']=='Соня')
        self.check('asking for explanation does not select feat',state['proposal']['general_feat']=='')
        self.check('rescue concept appears in card',any(word in state['proposal']['description'].lower() for word in ('спас','помога')))
        state=self.ask('Выбираю Невероятную инициативу. Принцип: помогать попавшим в беду. Табу: убивать людей. Имя, класс и описание оставь.')
        self.check('requested feat selected',state['proposal']['general_feat']=='incredible_initiative')
        before=state['proposal'].copy()
        state=self.ask('Объясни простыми словами, чем я полезна группе. Пока ничего в карточке не меняй.')
        self.check('advice preserves all personal choices',state['proposal']==before)
        state=self.ask('Поменяй только табу на «выдавать чужие тайны». Остальное оставь дословно.')
        self.check('single belief correction keeps other fields',all(state['proposal'].get(k)==v for k,v in before.items() if k!='anathema'))
        self.check('new taboo is stored', 'тайн' in state['proposal'].get('anathema','').lower())
        self.check('personal choices give no invented bonuses',state['preview']['attributes']==RULES['classes']['rogue']['attributes'])

        self.begin_preference('unsupported concept requires consent')
        state=self.ask('Хочу эльфа-друида, который превращается в сову и лечит друзей. Это уже доступно? Если нет, объясни ограничения. НЕ заменяй его человеком или магом без моего согласия, ничего пока не выбирай.')
        self.check('unsupported request does not silently choose substitute',not state['proposal']['class_id'] and not state['proposal']['general_feat'] and state['preview'] is None)
        state=self.ask('Тогда согласен на человека-мага по имени Север с Быстроногим. Он мирный исследователь леса, избегает насилия и любит животных. Честно объясни, каких способностей друида у него не будет.')
        self.check('explicit fallback creates supported mage',state['proposal']['class_id']=='wizard' and state['proposal']['name']=='Север' and state['proposal']['general_feat']=='fleet')
        self.check('fallback keeps nature concept',any(word in state['proposal']['description'].lower() for word in ('лес','животн','природ')))
        self.check('fallback grants no druid magic',set(state['preview']['progression']['spells'])==set(RULES['classes']['wizard']['spells']))

        choice={'name':'Лев','class_id':'fighter','general_feat':'fleet','description':'Учёный дипломат, который умеет защищаться.',
            'edicts':'Сначала договориться.','anathema':'Унижать слабых.'}
        self.begin_preference('intellectual fighter and rule limits',choice)
        state=self.ask('Хочу оставить класс воина, но сделать интеллект и харизму высокими, а силу низкой. И начать с посохом вместо меча и щита. Такое уже можно настроить? Пока ничего в карточке не меняй, объясни честно.')
        self.check('discussing alternative build leaves manual card intact',state['proposal']==choice)
        self.check('no invented redistribution of stats',state['preview']['attributes']==RULES['classes']['fighter']['attributes'])
        state=self.ask('Ладно, оставляем поддержанную сборку. Поменяй только описание: «Я дипломат, который защищает друзей и решает проблемы словами; боевой опыт скрывает». Принципы и табу не трогай.')
        self.check('flavor edit keeps mechanical and identity choices',all(state['proposal'].get(k)==v for k,v in choice.items() if k!='description'))
        self.check('new personal concept is stored','дипломат' in state['proposal']['description'].lower() and 'скрыва' in state['proposal']['description'].lower())
        self.check('all preference cases use supported first-level rules',state['preview']['sheet']['values']['level']==1)
        final_choice=state['proposal'].copy()
        status,saved=self.request('/api/characters',{'action':'save','id':self.draft,'revision':state['revision']})
        if status!=200:raise ValueError(saved)
        status,game=self.request('/api/games',{'action':'create','campaign':'solo-evening','name':'Образ в игре','hero_id':saved['hero_id']})
        if status!=200:raise ValueError(game)
        self.room=game['room_id'];self.root='/api/rooms/'+self.room
        state=self.state();hero=state['character_id']
        self.players=[{'character_id':hero,'character_name':final_choice['name']}];self.tokens=[self.owner]
        self.turn(0,'Напомни, кто мой герой по характеру, что ему важно и как он обычно решает проблемы. Карточку и мир не меняй.')
        from iirpg.session_context import context
        with self.server.service.store._connection() as c:actor,_=self.server.adventure.actor(c,self.room,hero)
        identity=context(self.state(),actor,'Кто я?')['hero_identity']
        self.check('personal concept reaches game GM',all(identity[k]==final_choice[k] for k in ('description','edicts','anathema')))
        reply=self.state()['history'][-1]['gm'].lower()
        self.check('GM recalls diplomatic concept without prompt reminder','дипломат' in reply)
        self.check('roleplay preferences do not grant bonus statistics',actor['attributes']==RULES['classes']['fighter']['attributes'])




def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live',action='store_true',required=True)
    parser.add_argument('--case',choices=['basic','preferences'],default='basic')
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
        try:(test.preferences if args.case=='preferences' else test.play)()
        except (ValueError,KeyError,AssertionError) as error:test.case['error']=str(error);print('ERROR:',error,flush=True)
    finally:test.close()
    failures=[c for case in test.report['cases'] for c in case['checks'] if not c['passed']]
    print('Report:',output,'Requests:',test.report['requests'],'Failed:',len(failures),flush=True)
    if failures or any('error' in case for case in test.report['cases']):raise SystemExit(1)


if __name__=='__main__':main()
