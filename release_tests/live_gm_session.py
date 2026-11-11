"""Opt-in real free-model playthroughs, isolated rooms and controlled test dice."""
import json
import argparse
from pathlib import Path
from uuid import uuid4
from iirpg.server import ChatHTTPServer
from iirpg.application import ChatService
from iirpg.dice import DiceTool

SCENARIOS = {
 'conversation': [('Спрашиваю хозяйку, что случилось с книгой.',10),('Иду во двор к конюху.',10),('Привет, я хочу помочь. Как тебя зовут?',10),('Спокойно спрашиваю, что он видел вчера вечером.',10),('Возвращаюсь в зал.',10),('Иду обратно во двор.',10),('Напоминаю конюху наш разговор. Что ещё он вспомнил?',10)],
 'quest': [('Иду в кладовую.',10),('Поддеваю сундук дубиной как рычагом, чтобы достать книгу.',20),('Быстро изучаю найденную книгу.',10),('Возвращаюсь с книгой в зал, но пока держу её при себе.',10),('Возвращаю дорожную книгу хозяйке.',10),('Спрашиваю хозяйку, всё ли теперь в порядке с возвращённой книгой.',10)],
 'aggression': [('Иду во двор.',10),('Угрожаю конюху: говори про книгу, иначе пожалеешь!',3),('Бью конюха кулаком в живот.',20),('Ухожу в зал.',10),('Возвращаюсь во двор к конюху.',10),('Ну что, теперь расскажешь?',10)],
 'boundaries': [('Телепортируюсь на луну силой мысли.',10),('Осматриваю зал. Какие есть выходы?',10)]}

def main():
 parser=argparse.ArgumentParser();parser.add_argument('--scenario',choices=SCENARIOS);parser.add_argument('--report',default='.local/gm-playthroughs.json');args=parser.parse_args()
 server=ChatHTTPServer(('127.0.0.1',0),Path('.local/chat.sqlite3'))
 reports=[]
 try:
  for name,turns in SCENARIOS.items():
   if args.scenario and name!=args.scenario:continue
   room=server.service.store.create_room()
   actor=server.roster.execute(room,{'action':'create_character','revision':0,'data':{'name':'Испытатель '+name,'attributes':{'strength':4,'charisma':3}}},str(uuid4()))['entity_id']
   server.roster.execute(room,{'action':'add_item','revision':server.roster.read(room)['revision'],'character_id':actor,'data':{'name':'Дубина','category':'weapon'}},str(uuid4()))
   report={'scenario':name,'room':room,'turns':[]};reports.append(report)
   for text,natural in turns:
    try:
     before=server.adventure.read(room);state=server.adventure.execute(room,{'revision':before['revision'],'character_id':actor,'text':text},str(uuid4()))
     entry={'input':text,'reply':state['history'][-1],'location':state['location'],'book':state['book'],'completed':state.get('completed',False)}
     if state.get('pending_check'):
      pending=state['pending_check'];service=ChatService(server.service.store,DiceTool(roll_die=lambda sides:natural))
      payload={'sender':'first','type':'tool.call','tool':'dice.roll','request_id':pending['roll_request_id'],'offer_id':pending['offer_id']}
      service.send(room,payload);state=server.adventure.read(room);entry['roll']=state['history'][-1]
      stable=json.dumps(state,sort_keys=True);service.send(room,payload)
      assert json.dumps(server.adventure.read(room),sort_keys=True)==stable,'Repeated dice changed state'
      state=server.adventure.react(room,pending['offer_id']);entry['reaction']=state['history'][-1]
     entry.update(location=state['location'],book=state['book'],completed=state.get('completed',False))
     expected={'conversation':['hall','yard','yard','yard','hall','yard','yard'],'quest':['store','store','store','hall','hall','hall'],'aggression':['yard']*3+['hall','yard','yard'],'boundaries':['hall','hall']}[name][len(report['turns'])]
     assert state['location']==expected,f"Expected {expected}, got {state['location']}"
     if name=='quest' and len(report['turns'])==3:assert not state.get('completed'),'Book returned without consent'
     if name=='quest' and len(report['turns'])==4:assert state.get('completed'),'Book not returned'
     report['turns'].append(entry);print(json.dumps({'scenario':name,**entry},ensure_ascii=False),flush=True)
    except Exception as error:
     report['error']=str(error);print(name,'ERROR',str(error),flush=True);break
 finally:
  Path(args.report).write_text(json.dumps(reports,ensure_ascii=False,indent=2));server.server_close()
 if any('error' in report for report in reports):raise SystemExit('Playthrough failures recorded in '+args.report)

if __name__=='__main__':main()
