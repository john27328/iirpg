"""Room scenes, stable entity references and adjudicated player intentions."""
from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
from uuid import UUID, uuid4
from typing import Callable
import sqlite3
from .maps import install_map, templates, zone_at
from .roster import name
from .scene_rules import perform_action
from .roster_storage import RosterStore
from .storage import MessageConflict


class SceneStore:
    def __init__(self, roster: RosterStore, roll_die: Callable[[int], int]) -> None:
        self.roster, self.store, self.roll_die = roster, roster.store, roll_die
        self.maps = templates()
        self.rules = json.loads((Path(__file__).with_name('rules') / 'scene_pf2e.json').read_text())
        with self.store._connection() as c:
            c.executescript('''CREATE TABLE IF NOT EXISTS scenes(room_id TEXT PRIMARY KEY REFERENCES rooms(id), body TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS scene_requests(room_id TEXT NOT NULL REFERENCES rooms(id), request_id TEXT NOT NULL, command TEXT NOT NULL, response TEXT NOT NULL, PRIMARY KEY(room_id,request_id));''')

    def _load(self, c: sqlite3.Connection, room: str) -> dict:
        self.store._require_room(c, room)
        row = c.execute('SELECT body FROM scenes WHERE room_id=?', (room,)).fetchone()
        return json.loads(row[0]) if row else {'revision': 0, 'id': room, 'name': 'Придорожная таверна', 'rules': self.rules,
            'zones': deepcopy(self.rules['zones']), 'entities': [dict(obj, kind='object') for obj in self.rules['objects']], 'proposals': []}

    def read(self, room: str) -> dict:
        with self.store._connection() as c:
            return self._load(c, room)

    def execute(self, room: str, command: dict, request_id: str) -> dict:
        try:
            UUID(request_id)
        except (ValueError, TypeError, AttributeError):
            raise ValueError('Нужен UUID Idempotency-Key.') from None
        if set(command) - {'action', 'revision', 'actor_id', 'target_id', 'parameters', 'text', 'proposal_id'}:
            raise ValueError('Неизвестные поля действия сцены.')
        encoded = json.dumps(command, sort_keys=True, allow_nan=False)
        with self.store._connection() as c:
            c.execute('BEGIN IMMEDIATE')
            state = self._load(c, room)
            prior = c.execute('SELECT command,response FROM scene_requests WHERE room_id=? AND request_id=?', (room, request_id)).fetchone()
            if prior:
                if prior[0] != encoded:
                    raise MessageConflict('Идентификатор действия уже использован.')
                return json.loads(prior[1])
            if type(command.get('revision')) is not int or command['revision'] != state['revision']:
                raise MessageConflict('Сцена изменилась. Обновите данные.')
            if c.execute("SELECT 1 FROM sqlite_master WHERE name='encounters'").fetchone():
                battle=c.execute('SELECT body FROM encounters WHERE room_id=?',(room,)).fetchone()
                if battle and json.loads(battle[0]).get('status')=='active' and json.loads(battle[0]).get('mapped') and command.get('action')!='propose':
                    raise ValueError('Идёт бой на карте: используйте боевые действия, смена сцены временно недоступна.')
            before = deepcopy(state)
            action = command.get('action')
            roster = self.roster._load(c, room)
            if action == 'load_map':
                parameters = command.get('parameters', {})
                if not isinstance(parameters, dict) or set(parameters) - {'template_id', 'map'} or ('template_id' in parameters) == ('map' in parameters):
                    raise ValueError('Укажите либо template_id, либо map.')
                document = parameters.get('map')
                if 'template_id' in parameters:
                    document = next((m for m in self.maps if m['id'] == parameters['template_id']), None)
                install_map(state, document)
                result = {'text': 'Загружена карта: ' + state['name']}
            elif action == 'join':
                actor = next((a for a in roster['characters'] if a['id'] == command.get('actor_id')), None)
                if not actor:
                    raise ValueError('Персонаж не найден в комнате.')
                if any(e['id'] == actor['id'] for e in state['entities']):
                    raise ValueError('Персонаж уже находится в сцене.')
                state['entities'].append({'id': actor['id'], 'character_id': actor['id'], 'kind': 'actor', 'name': actor['name'], 'zone_id': state['zones'][0]['id'], 'cover': None})
                if 'map' in state:
                    state['entities'][-1].update(position=list(state['map']['spawn']), zone_id=zone_at(state['map'], state['map']['spawn']))
                result = {'text': actor['name'] + ' входит в сцену.'}
            else:
                actor = next((e for e in state['entities'] if e['id'] == command.get('actor_id') and e['kind'] == 'actor'), None)
                if action == 'resolve':
                    proposal = next((p for p in state['proposals'] if p['id'] == command.get('proposal_id') and p['status'] == 'pending'), None)
                    if not proposal:
                        raise ValueError('Ожидающее намерение не найдено.')
                    actor = next((e for e in state['entities'] if e['id'] == proposal['actor_id']), None)
                if not actor:
                    raise ValueError('Сначала добавьте персонажа в сцену.')
                if not any(a['id'] == actor['character_id'] for a in roster['characters']):
                    raise ValueError('Персонаж сцены удалён из реестра.')
                if action == 'propose':
                    if len(state['proposals']) >= 200:
                        raise ValueError('Достигнут лимит намерений сцены.')
                    text = name(command.get('text'), 'Намерение', 1000)
                    proposal = {'id': str(uuid4()), 'actor_id': actor['id'], 'text': text, 'status': 'pending'}
                    state['proposals'].append(proposal)
                    result = {'text': actor['name'] + ' предлагает: ' + text, 'proposal_id': proposal['id']}
                else:
                    parameters = command.get('parameters', {})
                    if not isinstance(parameters, dict):
                        raise ValueError('parameters должен быть объектом.')
                    allowed = {'effect', 'dc', 'position'} if action == 'resolve' else {'position'} if action == 'move_cell' else set()
                    if set(parameters) - allowed:
                        raise ValueError('Неизвестные параметры действия.')
                    effect = parameters.get('effect') if action == 'resolve' else action
                    if effect == 'reject' and action == 'resolve':
                        result = {'text': 'Ведущий отклонил намерение: ' + proposal['text']}
                    else:
                        result = perform_action(state, actor, effect, command.get('target_id'), parameters.get('dc'), roster, self.roll_die, parameters.get('position'))
                    if action == 'resolve':
                        proposal.update(status='resolved', resolution=deepcopy(result))
            state['revision'] += 1
            envelope = {'schema_version': 1, 'type': 'scene.action', 'sender': 'second', 'text': result['text'],
                'visibility': {'mode': 'all', 'chat_ids': []}, 'choices': [], 'metadata': {'scene_id': state['id']},
                'created_at': datetime.now(timezone.utc).isoformat(), 'scene': {'command': command, 'result': result, 'before': before, 'after': deepcopy(state)}}
            response = json.dumps(state, ensure_ascii=False, allow_nan=False)
            c.execute('INSERT INTO scenes VALUES(?,?) ON CONFLICT(room_id) DO UPDATE SET body=excluded.body', (room, response))
            c.execute('INSERT INTO queue_events(room_id,request_id,envelope) VALUES(?,?,?)', (room, request_id, json.dumps(envelope, ensure_ascii=False)))
            c.execute('INSERT INTO scene_requests VALUES(?,?,?,?)', (room, request_id, encoded, response))
            return state
