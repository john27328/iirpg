"""Room-scoped invitation credentials and persistent player/character bindings."""
from copy import deepcopy
import hashlib
import json
import secrets
from uuid import UUID, uuid4
from urllib.parse import urlencode
from .party import SETTINGS
from .storage import MessageConflict
from .roster import InventoryRules
from .character_sheet import CharacterRules
from .session_world import initialize


class Participants:
    def __init__(self, adventure):
        self.adventure = adventure
        self.store = adventure.store
        with self.store._connection() as c:
            c.executescript('''
                CREATE TABLE IF NOT EXISTS session_participants(
                    room_id TEXT NOT NULL REFERENCES rooms(id), player_id TEXT NOT NULL,
                    character_id TEXT NOT NULL, token TEXT NOT NULL, token_hash TEXT NOT NULL UNIQUE,
                    initial_character TEXT NOT NULL, initial_items TEXT NOT NULL,
                    PRIMARY KEY(room_id,player_id));
                CREATE TABLE IF NOT EXISTS participant_operations(
                    room_id TEXT NOT NULL REFERENCES rooms(id), request_id TEXT NOT NULL,
                    command TEXT NOT NULL, response TEXT NOT NULL, PRIMARY KEY(room_id,request_id));
            ''')

    def authenticate(self, token: str) -> dict | None:
        digest = hashlib.sha256(token.encode()).hexdigest()
        with self.store._connection() as c:
            row = c.execute('''SELECT a.room_id,a.player_id,a.character_id,p.name AS player_name,c.body
                FROM session_participants a JOIN characters c ON c.room_id=a.room_id AND c.id=a.character_id
                JOIN players p ON p.room_id=a.room_id AND p.id=a.player_id
                WHERE a.token_hash=? AND c.player_id=a.player_id''', (digest,)).fetchone()
        if not row: return None
        return {'role': 'player', **{k: row[k] for k in ('room_id','player_id','character_id','player_name')},
                'character_name': json.loads(row['body'])['name']}

    def list(self, room: str, connection=None) -> dict:
        if connection is None:
            with self.store._connection() as c: return self.list(room, c)
        self.store._require_room(connection, room)
        result = []
        for row in connection.execute('''SELECT a.*,p.name,c.body FROM session_participants a
                JOIN players p ON p.room_id=a.room_id AND p.id=a.player_id
                JOIN characters c ON c.room_id=a.room_id AND c.id=a.character_id
                WHERE a.room_id=? ORDER BY a.rowid''', (room,)):
            result.append({'player_id': row['player_id'], 'name': row['name'], 'character_id': row['character_id'],
                           'character_name': json.loads(row['body'])['name'],
                           'fragment': urlencode({'room': room, 'access': row['token']})})
        return {'players': result, 'max_players': SETTINGS['max_players'], 'travel_mode': SETTINGS['travel_mode']}

    def add(self, c, room: str, name: str, hero_id: str | None = None, *, character_id: str | None = None) -> dict:
        if not isinstance(name, str) or not name.strip() or len(name) > 80:
            raise ValueError('Имя игрока: от 1 до 80 символов.')
        existing = c.execute('SELECT character_id FROM session_participants WHERE room_id=? ORDER BY rowid', (room,)).fetchall()
        if len(existing) >= SETTINGS['max_players']: raise ValueError('Достигнут настроенный лимит игроков.')
        roster = self.adventure.ai.combat.roster._load(c, room)
        if character_id:
            if any(row['character_id'] == character_id for row in existing): raise ValueError('Герой уже закреплён за игроком.')
            actor = next((a for a in roster['characters'] if a['id']==character_id and a['kind']=='pc'), None)
            if not actor: raise ValueError('Выберите героя этого сеанса.')
        else:
            row = c.execute('SELECT body FROM hero_templates WHERE id=?', (hero_id,)).fetchone() if hero_id else c.execute('SELECT body FROM hero_templates ORDER BY rowid LIMIT 1').fetchone()
            if not row: raise ValueError('Выберите готового героя.')
            template = json.loads(row['body']); rules = InventoryRules(roster['rules'])
            body = rules.character({k:v for k,v in template.items() if k!='items'}, [], CharacterRules(roster['character_rules']))
            used={a['name'] for a in roster['characters']}
            original=body['name'];suffix=len(existing)+1
            while body['name'] in used:
                body['name']=original[:80]+' · '+str(suffix);suffix+=1
            character_id = str(uuid4())
            c.execute('INSERT INTO characters VALUES(?,?,?,?)', (character_id,room,None,json.dumps(body,ensure_ascii=False)))
            items = [{'id':str(uuid4()), **rules.item(item)} for item in template.get('items',[])]
            for item in items:
                c.execute('INSERT INTO inventory_items VALUES(?,?,?,?)',(item['id'],room,character_id,json.dumps({k:v for k,v in item.items() if k!='id'},ensure_ascii=False)))
            actor = {'id':character_id, **body, 'items':items}
        player = str(uuid4()); token = 'player_' + secrets.token_urlsafe(32)
        c.execute('INSERT INTO players VALUES(?,?,?)',(player,room,name.strip()))
        body = {k:v for k,v in actor.items() if k not in ('id','items','summary','sheet_statistics','progression')}
        body['player_id'] = player
        c.execute('UPDATE characters SET player_id=?,body=? WHERE room_id=? AND id=?',(player,json.dumps(body,ensure_ascii=False),room,character_id))
        initial={**body,'_invitation_player_name':name.strip()}
        c.execute('INSERT INTO session_participants VALUES(?,?,?,?,?,?,?)', (room,player,character_id,token,
                  hashlib.sha256(token.encode()).hexdigest(),json.dumps(initial,ensure_ascii=False),json.dumps(actor['items'],ensure_ascii=False)))
        c.execute('UPDATE room_rosters SET revision=revision+1 WHERE room_id=?',(room,))
        state = self.adventure._load(c,room)
        state['party_ids'] = [r['character_id'] for r in existing] + [character_id]
        initialize(state)
        self.adventure.sync_inventory(c,room,state)
        state['revision'] += 1
        self.adventure.save(c,room,state)
        return {'player_id':player,'character_id':character_id}

    def reconcile(self, c, room: str) -> None:
        """Membership survives checkpoint rewinds; newly admitted heroes return with starter gear."""
        rows = c.execute('SELECT * FROM session_participants WHERE room_id=? ORDER BY rowid',(room,)).fetchall()
        if not rows: return
        for row in rows:
            body = json.loads(row['initial_character'])
            name=body.pop('_invitation_player_name','Игрок '+str(rows.index(row)+1))
            c.execute('INSERT OR IGNORE INTO players VALUES(?,?,?)',(row['player_id'],room,name))
            if not c.execute('SELECT 1 FROM characters WHERE room_id=? AND id=?',(room,row['character_id'])).fetchone():
                c.execute('INSERT INTO characters VALUES(?,?,?,?)',(row['character_id'],room,row['player_id'],json.dumps(body,ensure_ascii=False)))
                for item in json.loads(row['initial_items']):
                    c.execute('INSERT INTO inventory_items VALUES(?,?,?,?)',(item['id'],room,row['character_id'],json.dumps({k:v for k,v in item.items() if k!='id'},ensure_ascii=False)))
            current=json.loads(c.execute('SELECT body FROM characters WHERE room_id=? AND id=?',(room,row['character_id'])).fetchone()[0])
            current['player_id']=row['player_id']
            c.execute('UPDATE characters SET player_id=?,body=? WHERE room_id=? AND id=?',(row['player_id'],json.dumps(current,ensure_ascii=False),room,row['character_id']))
        state = self.adventure._load(c,room)
        state['party_ids'] = [row['character_id'] for row in rows]
        if state.get('character_id') not in state['party_ids']: state['character_id'] = state['party_ids'][0]
        self.adventure.sync_inventory(c,room,state)
        self.adventure.save(c,room,state)

    def execute(self, room: str, command: dict, key: str) -> dict:
        try: UUID(key)
        except (ValueError,TypeError,AttributeError): raise ValueError('Нужен UUID операции.') from None
        if set(command)-{'action','name','hero_id','player_id'}: raise ValueError('Неизвестные поля приглашения.')
        encoded = json.dumps(command,sort_keys=True)
        with self.store._connection() as c:
            c.execute('BEGIN IMMEDIATE');self.store._require_room(c,room)
            prior = c.execute('SELECT command,response FROM participant_operations WHERE room_id=? AND request_id=?',(room,key)).fetchone()
            if prior:
                if prior['command']!=encoded: raise MessageConflict('UUID уже использован.')
                return json.loads(prior['response'])
            state=self.adventure._load(c,room)
            if state.get('active_combat') or state.get('pending_check'): raise MessageConflict('Закончите бой или проверку перед изменением группы.')
            import time
            if any(json.loads(r[0]).get('status') in ('running','chosen') for r in c.execute('SELECT body FROM ai_requests WHERE room_id=? AND started>?',(room,time.time()-120))):
                raise MessageConflict('Дождитесь ответа мастера перед изменением группы.')
            action=command.get('action')
            if action=='enable':
                if c.execute('SELECT 1 FROM session_participants WHERE room_id=?',(room,)).fetchone(): raise ValueError('Совместная игра уже включена.')
                if not state.get('character_id'): raise ValueError('Сначала создайте игровой сеанс с героем.')
                self.add(c,room,'Игрок 1',character_id=state['character_id'])
                self.add(c,room,'Игрок 2',command.get('hero_id'))
            elif action=='add':
                if not c.execute('SELECT 1 FROM session_participants WHERE room_id=?',(room,)).fetchone(): raise ValueError('Сначала включите совместную игру.')
                self.add(c,room,command.get('name'),command.get('hero_id'))
            elif action=='rotate':
                row=c.execute('SELECT * FROM session_participants WHERE room_id=? AND player_id=?',(room,command.get('player_id'))).fetchone()
                if not row: raise ValueError('Игрок не найден.')
                token='player_'+secrets.token_urlsafe(32)
                c.execute('UPDATE session_participants SET token=?,token_hash=? WHERE room_id=? AND player_id=?',(token,hashlib.sha256(token.encode()).hexdigest(),room,row['player_id']))
            else: raise ValueError('Неизвестное действие приглашения.')
            result=self.list(room,c)
            c.execute('INSERT INTO participant_operations VALUES(?,?,?,?)',(room,key,encoded,json.dumps(result,ensure_ascii=False)))
            return result
