"""Persistent two-participant training encounters, separate from character saves."""
from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
from uuid import UUID, uuid4
from typing import Callable
from .roster_storage import RosterStore
from .storage import MessageConflict


from .checks import degree
from .combat_map import distance, move_fighter, approach, route
from .maps import zone_at


class CombatStore:
    def __init__(self, roster: RosterStore, roll_die: Callable[[int], int]) -> None:
        self.roster, self.store, self.roll_die = roster, roster.store, roll_die
        self.rules = json.loads((Path(__file__).with_name('rules') / 'combat_pf2e.json').read_text())
        with self.store._connection() as c:
            c.executescript('''CREATE TABLE IF NOT EXISTS encounters(room_id TEXT PRIMARY KEY REFERENCES rooms(id), body TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS encounter_requests(room_id TEXT NOT NULL REFERENCES rooms(id), request_id TEXT NOT NULL, command TEXT NOT NULL, response TEXT NOT NULL, PRIMARY KEY(room_id,request_id));''')

    def read(self, room: str) -> dict:
        with self.store._connection() as c:
            self.store._require_room(c, room)
            row = c.execute('SELECT body FROM encounters WHERE room_id=?', (room,)).fetchone()
        return json.loads(row[0]) if row else {'revision': 0, 'status': 'idle', 'rules': self.rules}

    def execute(self, room: str, command: dict, request_id: str) -> dict:
        try:
            UUID(request_id)
        except (ValueError, TypeError, AttributeError):
            raise ValueError('Нужен UUID Idempotency-Key.') from None
        if set(command) - {'action', 'revision', 'character_id', 'position', 'target_id'}:
            raise ValueError('Неизвестные поля боя.')
        encoded = json.dumps(command, sort_keys=True, allow_nan=False)
        with self.store._connection() as c:
            c.execute('BEGIN IMMEDIATE')
            self.store._require_room(c, room)
            prior = c.execute('SELECT command,response FROM encounter_requests WHERE room_id=? AND request_id=?', (room, request_id)).fetchone()
            if prior:
                if prior[0] != encoded:
                    raise MessageConflict('Идентификатор уже использован.')
                return json.loads(prior[1])
            row = c.execute('SELECT body FROM encounters WHERE room_id=?', (room,)).fetchone()
            state = json.loads(row[0]) if row else {'revision': 0, 'status': 'idle'}
            if type(command.get('revision')) is not int or command['revision'] != state['revision']:
                raise MessageConflict('Бой изменился. Обновите страницу.')
            scene_row = c.execute("SELECT body FROM scenes WHERE room_id=?", (room,)).fetchone() if c.execute("SELECT 1 FROM sqlite_master WHERE name='scenes'").fetchone() else None
            scene = json.loads(scene_row[0]) if scene_row else None
            action = command.get('action')
            events = []

            def snapshot() -> dict:
                return {key: deepcopy(state.get(key)) for key in
                        ('round', 'turn', 'actions', 'attacks', 'status', 'fighters')}

            def record(kind: str, text: str, before: dict) -> None:
                fighters = before.get('fighters') or []
                actor_index = before.get('turn')
                actor = fighters[actor_index]['name'] if fighters else None
                event = {'action': kind, 'command': action, 'actor': actor,
                         'target': fighters[1 - actor_index]['name'] if kind == 'strike' else None,
                         'actor_index': actor_index if fighters else None,
                         'target_index': 1 - actor_index if fighters and kind == 'strike' else None,
                         'before': before, 'after': snapshot()}
                if kind == 'strike':
                    event['roll'] = deepcopy(state['last_attack'])
                events.append({'text': text, 'details': event})

            before = snapshot()
            if action == 'start':
                if state['status'] == 'active':
                    raise ValueError('Бой уже идёт.')
                roster = self.roster._load(c, room)
                actor = next((a for a in roster['characters'] if a['id'] == command.get('character_id') and a['kind'] == 'pc'), None)
                if not actor:
                    raise ValueError('Выберите игрового персонажа этой комнаты.')
                from .character_sheet import CharacterRules
                stats = CharacterRules(roster['character_rules']).statistics(actor['sheet'], actor['attributes'])
                if actor['sheet']['values'].get('hp_current', 0) <= 0:
                    raise ValueError('Сначала заполните положительные текущие ОЗ в листе.')
                if 'armor_class' not in stats or 'perception' not in stats:
                    raise ValueError('Профиль не содержит КБ и Восприятие.')
                strength = actor['attributes'].get('strength', 0)
                enemy = deepcopy(self.rules['opponent'])
                player = {'name': actor['name'], 'character_id': actor['id'], 'hp': actor['sheet']['values']['hp_current'], 'ac': stats['armor_class'], 'perception': stats['perception'], 'attack': strength + actor['sheet']['values'].get('level', 1) + self.rules['player']['proficiency_offset'], 'damage_die': self.rules['player']['die'], 'damage_bonus': strength}
                fighters = [player, enemy]
                for fighter in fighters:
                    fighter.update(max_hp=fighter['hp'], shield=False)
                    fighter['initiative_die'] = self.roll_die(20)
                    fighter['initiative'] = fighter['initiative_die'] + fighter['perception']
                # NPC wins tied initiative against PCs.
                turn = 0 if player['initiative'] > enemy['initiative'] else 1
                state = {'revision': state['revision'], 'id': str(uuid4()), 'status': 'active', 'rules': self.rules, 'fighters': fighters, 'turn': turn, 'round': 1, 'actions': self.rules['actions'], 'attacks': 0, 'log': []}
                if scene and 'map' in scene:
                    scene['entities']=[e for e in scene['entities'] if e['kind']!='combatant']
                    entity=next((e for e in scene['entities'] if e['id']==actor['id']),None)
                    position=list(entity['position']) if entity and 'position' in entity else list(scene['map']['spawn'])
                    player.update(position=position, speed=actor['sheet']['values'].get('speed',25), cover=deepcopy(entity.get('cover')) if entity else None, entity_id=actor['id'])
                    enemy.update(speed=25,cover=None,entity_id='training:'+state['id'])
                    possible=[]
                    for y,row_text in enumerate(scene['map']['rows']):
                        for x,tile in enumerate(row_text):
                            if tile=='#' or [x,y]==position:continue
                            path=route(scene,position,[x,y])
                            if path:possible.append((abs(len(path)-4),len(path),[x,y]))
                    if not possible:raise ValueError('На карте нет места для второго участника.')
                    enemy['position']=min(possible)[2]
                    state['mapped']=True
                    state['scene_id']=scene['id']
                record('start', f"Начат учебный бой: {player['name']} против {enemy['name']}. Инициатива {player['initiative']} / {enemy['initiative']}.", before)
                events[-1]['details'].update(actor=player['name'], target=enemy['name'], actor_index=0, target_index=1)
            else:
                if state['status'] != 'active':
                    raise ValueError('Нет активного боя.')
                rules = state['rules']
                turn = state['turn']
                if state.get('mapped') and (not scene or 'map' not in scene):
                    raise ValueError('Карта боя недоступна.')
                snapshot_before = snapshot()
                if action == 'strike':
                    record('strike', self.strike(state), snapshot_before)
                elif action == 'move':
                    if not state.get('mapped'):raise ValueError('Бой не связан с картой.')
                    record('move', move_fighter(state,scene,command.get('position')), before)
                elif action == 'take_cover':
                    if not state.get('mapped') or state['actions']<=0:raise ValueError('Нельзя занять укрытие.')
                    target=next((e for e in scene['entities'] if e['kind']=='object' and e['id']==command.get('target_id')),None)
                    fighter=state['fighters'][turn]
                    if not target or not target['provides_cover'] or distance(fighter['position'],target['position'])!=1:
                        raise ValueError('Рядом должно быть подходящее укрытие.')
                    fighter['cover']={'object_id':target['id'],'bonus':scene['rules']['cover_bonus']}
                    state['actions']-=1
                    record('take_cover',fighter['name']+' занимает укрытие.',before)
                elif action == 'shield':
                    if state['actions'] <= 0:
                        raise ValueError('Действия закончились.')
                    state['actions'] -= 1
                    state['fighters'][turn]['shield'] = True
                    record('shield', state['fighters'][turn]['name'] + ' поднимает учебный щит.', before)
                elif action == 'end_turn':
                    self.end_turn(state)
                    record('end_turn', 'Конец хода. Ход: ' + state['fighters'][state['turn']]['name'], before)
                elif action == 'enemy_turn' and turn == 1:
                    while state['actions'] and state['status'] == 'active':
                        snapshot_before = snapshot()
                        if state.get('mapped') and distance(state['fighters'][1]['position'],state['fighters'][0]['position'])!=1:
                            destination=approach(state,scene)
                            if destination is None:
                                events_text='Противник не может добраться до цели и заканчивает ход.'
                                state['actions']=0
                                record('wait',events_text,snapshot_before)
                            else:record('move',move_fighter(state,scene,destination),snapshot_before)
                        else:record('strike', self.strike(state), snapshot_before)
                    if state['status'] == 'active':
                        before_turn = snapshot()
                        self.end_turn(state)
                        record('end_turn', 'Конец хода противника. Ход: ' + state['fighters'][state['turn']]['name'], before_turn)
                else:
                    raise ValueError('Недопустимое действие.')
            if state.get('mapped'):
                for fighter in state['fighters']:
                    entity=next((e for e in scene['entities'] if e['id']==fighter['entity_id']),None)
                    if entity is None:
                        entity={'id':fighter['entity_id'],'kind':'actor' if 'character_id' in fighter else 'combatant','name':fighter['name']}
                        if 'character_id' in fighter:entity['character_id']=fighter['character_id']
                        scene['entities'].append(entity)
                    entity.update(position=fighter['position'],zone_id=zone_at(scene['map'],fighter['position']),cover=fighter.get('cover'),training_hp=fighter['hp'])
                scene['revision']+=1
                c.execute('INSERT INTO scenes VALUES(?,?) ON CONFLICT(room_id) DO UPDATE SET body=excluded.body',(room,json.dumps(scene,ensure_ascii=False)))
            state['revision'] += 1
            for index, event in enumerate(events):
                text, details = event['text'], event['details']
                details.update(request_id=request_id, step=index, round=state['round'] if details['action'] == 'start' else details['before']['round'])
                state['log'].append(text)
                envelope = {'schema_version': 1, 'type': 'combat.action', 'sender': 'second', 'text': text, 'visibility': {'mode': 'all', 'chat_ids': []}, 'choices': [], 'metadata': {'encounter_id': state['id'], 'combat_revision': state['revision']}, 'created_at': datetime.now(timezone.utc).isoformat(), 'combat': details}
                c.execute('INSERT INTO queue_events(room_id,request_id,envelope) VALUES(?,?,?)', (room, str(uuid4()), json.dumps(envelope, ensure_ascii=False)))
            response = json.dumps(state, ensure_ascii=False)
            c.execute('INSERT INTO encounters VALUES(?,?) ON CONFLICT(room_id) DO UPDATE SET body=excluded.body', (room, response))
            c.execute('INSERT INTO encounter_requests VALUES(?,?,?,?)', (room, request_id, encoded, response))
            return state

    def end_turn(self, state: dict) -> None:
        state['turn'] = 1 - state['turn']
        state['turns_elapsed'] = state.get('turns_elapsed', 0) + 1
        state['round'] = 1 + state['turns_elapsed'] // 2
        state['actions'], state['attacks'] = state['rules']['actions'], 0
        state['fighters'][state['turn']]['shield'] = False

    def strike(self, state: dict) -> str:
        if state['actions'] <= 0:
            raise ValueError('Действия закончились.')
        attacker, target = state['fighters'][state['turn']], state['fighters'][1 - state['turn']]
        if state.get('mapped') and distance(attacker['position'],target['position'])!=1:
            raise ValueError('Цель вне досягаемости: подойдите на соседнюю клетку.')
        attacker['cover']=None
        penalty = state['rules']['map'][min(state['attacks'], 2)]
        natural = self.roll_die(20)
        total = natural + attacker['attack'] + penalty
        dc = target['ac'] + max(state['rules']['shield_bonus'] if target['shield'] else 0, (target.get('cover') or {}).get('bonus',0))
        outcome = degree(natural, total, dc)
        damage_roll = self.roll_die(attacker['damage_die']) if outcome >= 2 else 0
        damage = max(0, damage_roll + attacker['damage_bonus']) * (2 if outcome == 3 else 1) if outcome >= 2 else 0
        target['hp'] = max(0, target['hp'] - damage)
        state['actions'] -= 1
        state['attacks'] += 1
        state['last_attack'] = {'natural': natural, 'total': total, 'dc': dc, 'map': penalty, 'degree': outcome, 'damage_die': damage_roll, 'damage': damage}
        label = ['критический провал', 'промах', 'попадание', 'критическое попадание'][outcome]
        text = f"{attacker['name']} — удар мечом: d20={natural}, итог {total} против КБ {dc} — {label}; урон {damage}. ОЗ цели {target['hp']}/{target['max_hp']}."
        if target['hp'] == 0:
            state['status'], state['winner'] = 'finished', attacker['name']
            text += f" Учебный бой окончен. Победитель: {attacker['name']}."
        return text
