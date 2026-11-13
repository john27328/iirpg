"""Named games and room-scoped checkpoints, independent of AI providers."""
import json
import time
from datetime import datetime, timezone
from uuid import UUID, uuid4
from .storage import MessageConflict
from .roster import InventoryRules
from .character_sheet import CharacterRules
from .hero_library import HeroLibrary

SCENARIOS = [{'id':'missing-book','name':'Пропавшая дорожная книга','description':'Тестовое приключение: постоялый двор, разговоры, проверки и возможная драка.'}]
EXCLUDED = {'game_sessions','game_checkpoints','game_operations','ai_requests'}

class GameSessions:
    def __init__(self, adventure):
        self.adventure=adventure;self.store=adventure.store
        self.heroes=HeroLibrary(adventure.ai)
        with self.store._connection() as c:
            c.executescript('CREATE TABLE IF NOT EXISTS game_sessions(room_id TEXT PRIMARY KEY REFERENCES rooms(id),name TEXT NOT NULL,scenario TEXT NOT NULL,saved_at TEXT); CREATE TABLE IF NOT EXISTS game_checkpoints(room_id TEXT PRIMARY KEY REFERENCES rooms(id),body TEXT NOT NULL); CREATE TABLE IF NOT EXISTS game_operations(id TEXT PRIMARY KEY,command TEXT NOT NULL,response TEXT NOT NULL);')

    def list(self):
        with self.store._connection() as c:
            games=[dict(r) for r in c.execute('SELECT s.*,r.created_at,EXISTS(SELECT 1 FROM game_checkpoints p WHERE p.room_id=s.room_id) AS has_save FROM game_sessions s JOIN rooms r ON r.id=s.room_id ORDER BY r.created_at DESC,s.rowid DESC')]
        return {'games':games,'scenarios':SCENARIOS}

    def tables(self,c):
        names=[r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'") if r[0] not in EXCLUDED and not r[0].startswith('sqlite_')]
        names=[n for n in names if any(r['name']=='room_id' for r in c.execute(f'PRAGMA table_info("{n}")'))]
        # Insert parent records before children, delete in reverse order.
        result=[]
        while names:
            ready=[n for n in names if not any(r['table'] in names for r in c.execute(f'PRAGMA foreign_key_list("{n}")'))]
            if not ready:raise ValueError('Не удалось определить порядок сохранения.')
            result.extend(ready);names=[n for n in names if n not in ready]
        return result

    def busy(self,c,room):
        if any(json.loads(r[0]).get('status') in ('running','chosen') for r in c.execute('SELECT body FROM ai_requests WHERE room_id=? AND started>?',(room,time.time()-120))):
            raise MessageConflict('Дождитесь ответа мастера перед сохранением, загрузкой или удалением.')

    def checkpoint(self,c,room):
        data={n:[dict(r) for r in c.execute(f'SELECT * FROM "{n}" WHERE room_id=?',(room,))] for n in self.tables(c)}
        c.execute('INSERT INTO game_checkpoints VALUES(?,?) ON CONFLICT(room_id) DO UPDATE SET body=excluded.body',(room,json.dumps(data,ensure_ascii=False)))
        c.execute('UPDATE game_sessions SET saved_at=? WHERE room_id=?',(datetime.now(timezone.utc).isoformat(),room))

    def execute(self,command,key):
        try:UUID(key)
        except (ValueError,TypeError,AttributeError):raise ValueError('Нужен UUID операции.') from None
        if set(command)-{'action','room_id','name','scenario','hero_id'}:raise ValueError('Неизвестные поля сеанса.')
        encoded=json.dumps(command,sort_keys=True)
        with self.store._connection() as c:
            c.execute('BEGIN IMMEDIATE')
            prior=c.execute('SELECT command,response FROM game_operations WHERE id=?',(key,)).fetchone()
            if prior:
                if prior['command']!=encoded:raise MessageConflict('UUID уже использован.')
                return json.loads(prior['response'])
            action=command.get('action');room=command.get('room_id')
            if action=='create':
                if command.get('scenario')!='missing-book':raise ValueError('Выберите доступный сценарий.')
                title=command.get('name','Новая игра')
                if not isinstance(title,str) or not title.strip() or len(title)>100:raise ValueError('Название: от 1 до 100 символов.')
                hero_id=command.get('hero_id')
                if 'hero_id' in command and not isinstance(hero_id,str):raise ValueError('Выберите готового героя.')
                template=c.execute('SELECT body FROM hero_templates WHERE id=?',(hero_id,)).fetchone() if hero_id is not None else c.execute('SELECT body FROM hero_templates ORDER BY rowid LIMIT 1').fetchone()
                if not template:raise ValueError('Выберите готового героя или создайте нового с помощником.')
                prepared=json.loads(template['body'])
                room=str(uuid4());c.execute('INSERT INTO rooms(id) VALUES(?)',(room,))
                c.execute('INSERT INTO game_sessions VALUES(?,?,?,NULL)',(room,title.strip(),'missing-book'))
                roster=self.adventure.ai.combat.roster._load(c,room)
                rules=InventoryRules(roster['rules'])
                character=rules.character({k:v for k,v in prepared.items() if k!='items'},[],CharacterRules(roster['character_rules']))
                actor=str(uuid4());body=character
                c.execute('INSERT INTO characters VALUES(?,?,?,?)',(actor,room,None,json.dumps(body,ensure_ascii=False)))
                for item in prepared.get('items',[]):
                    c.execute('INSERT INTO inventory_items VALUES(?,?,?,?)',(str(uuid4()),room,actor,json.dumps(rules.item(item),ensure_ascii=False)))
                c.execute('UPDATE room_rosters SET revision=1 WHERE room_id=?',(room,))
                state={'revision':1,'location':'hall','book':False,'character_id':actor,'history':[{'player':'','gm':'Вы входите в постоялый двор. Хозяйка просит помочь найти пропавшую дорожную книгу. У лестницы видны грязные следы. Из зала можно пройти в кладовую или во двор. Что вы делаете?','action':'start'}]}
                c.execute('INSERT INTO adventures VALUES(?,?)',(room,json.dumps(state,ensure_ascii=False)));self.adventure.publish(c,room,state)
                self.checkpoint(c,room)
            elif action in ('register','save','load','delete'):
                self.store._require_room(c,room)
                if action=='register':c.execute('INSERT OR IGNORE INTO game_sessions VALUES(?,?,?,NULL)',(room,'Моё приключение','missing-book'))
                else:
                    if not c.execute('SELECT 1 FROM game_sessions WHERE room_id=?',(room,)).fetchone():raise ValueError('Сеанс не найден.')
                    self.busy(c,room)
                    if action=='save':
                        self.adventure.sync_combat(c,room,self.adventure._load(c,room))
                        self.checkpoint(c,room)
                    else:
                        tables=self.tables(c)
                        snapshot=None
                        if action=='load':
                            row=c.execute('SELECT body FROM game_checkpoints WHERE room_id=?',(room,)).fetchone()
                            if not row:raise ValueError('Сохранения пока нет.')
                            snapshot=json.loads(row[0])
                        revisions={}
                        for table in ('adventures','scenes','encounters'):
                            row=c.execute(f'SELECT body FROM {table} WHERE room_id=?',(room,)).fetchone()
                            revisions[table]=json.loads(row[0]).get('revision',0) if row else 0
                        roster=c.execute('SELECT revision FROM room_rosters WHERE room_id=?',(room,)).fetchone();revisions['room_rosters']=roster[0] if roster else 0
                        for table in reversed(tables):c.execute(f'DELETE FROM "{table}" WHERE room_id=?',(room,))
                        if action=='delete':
                            for usage in c.execute("SELECT date(started,'unixepoch') AS day,COUNT(*) AS total FROM ai_requests WHERE room_id=? GROUP BY day",(room,)).fetchall():
                                c.execute('INSERT INTO ai_deleted_usage VALUES(?,?) ON CONFLICT(day) DO UPDATE SET count=count+excluded.count',(usage['day'],usage['total']))
                            c.execute('DELETE FROM ai_requests WHERE room_id=?',(room,))
                            c.execute('DELETE FROM game_checkpoints WHERE room_id=?',(room,));c.execute('DELETE FROM game_sessions WHERE room_id=?',(room,));c.execute('DELETE FROM rooms WHERE id=?',(room,))
                        else:
                            for table in tables:
                                for row in snapshot.get(table,[]):
                                    if table in ('adventures','scenes','encounters'):
                                        body=json.loads(row['body']);body['revision']=max(body.get('revision',0),revisions[table])+1
                                        # Chat publication IDs depend on history positions, not optimistic concurrency revisions.
                                        if table=='adventures':
                                            body['timeline_id']=str(uuid4())
                                            start=json.loads(row['body'])['revision']-len(body['history'])
                                            for i,h in enumerate(body['history'],start+1):h.setdefault('chat_index',i)
                                        row['body']=json.dumps(body,ensure_ascii=False)
                                    if table=='room_rosters':row['revision']=max(row['revision'],revisions[table])+1
                                    keys=list(row);columns=','.join('"'+k+'"' for k in keys)
                                    c.execute(f'INSERT INTO "{table}" ({columns}) VALUES ({",".join("?" for _ in keys)})',[row[k] for k in keys])
            else:raise ValueError('Неизвестная операция сеанса.')
            result={'room_id':room,'action':action}
            c.execute('INSERT INTO game_operations VALUES(?,?,?)',(key,encoded,json.dumps(result)))
            return result
