"""Opt-in real free-model character conversation; removes only its own draft afterwards."""
import json
from pathlib import Path
from uuid import uuid4
from iirpg.server import ChatHTTPServer


def main():
    server=ChatHTTPServer(('127.0.0.1',0),Path('.local/chat.sqlite3'))
    library=server.games.heroes
    room=library.execute({'action':'new_draft'},str(uuid4()))['draft_id']
    report={'model':server.ai.config['model'],'turns':[]}
    try:
        for text in ['Я совсем не умею играть. Хочу мага по имени Мира. Выбери подходящий новичку перк и покажи готовый вариант.',
                     'Оставь класс мага и выбранный перк. Поменяй только имя: Нора.']:
            state=library.respond(room,{'revision':library.read(room)['revision'],'text':text},str(uuid4()))
            report['turns'].append({'text':text,'proposal':state['proposal'],'reply':state['conversation'][-1]['text']})
            print(json.dumps(report['turns'][-1],ensure_ascii=False),flush=True)
            assert state['proposal']['class_id']=='wizard'
            assert state['proposal']['general_feat'] in ('fleet','incredible_initiative')
        assert state['proposal']['name']=='Нора'
        report['passed']=True
    finally:
        with server.service.store._connection() as c:
            report['traces']=[json.loads(r[0]) for r in c.execute('SELECT body FROM ai_requests WHERE room_id=?',(room,))]
            for usage in c.execute("SELECT date(started,'unixepoch') AS day,COUNT(*) AS total FROM ai_requests WHERE room_id=? GROUP BY day",(room,)).fetchall():
                c.execute('INSERT INTO ai_deleted_usage VALUES(?,?) ON CONFLICT(day) DO UPDATE SET count=count+excluded.count',(usage['day'],usage['total']))
            for table in ('queue_events','ai_requests','hero_drafts'):c.execute(f'DELETE FROM {table} WHERE room_id=?',(room,))
            c.execute('DELETE FROM rooms WHERE id=?',(room,))
        Path('.local/character-creation-playthrough.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
        server.server_close()
    print('PASS real free-model guided build and subsequent correction; test draft cleaned',flush=True)


if __name__=='__main__':main()
