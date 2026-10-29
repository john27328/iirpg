"""Persistent two-participant training encounters, separate from character saves."""
from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
from uuid import UUID, uuid4
from typing import Callable
from .roster_storage import RosterStore
from .storage import MessageConflict


def degree(natural: int, total: int, dc: int) -> int:
    result = 3 if total >= dc + 10 else 2 if total >= dc else 0 if total <= dc - 10 else 1
    return max(0, min(3, result + (1 if natural == 20 else -1 if natural == 1 else 0)))


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
        if set(command) - {'action', 'revision', 'character_id'}:
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
            action = command.get('action')
            events = []
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
                events.append(f"Начат учебный бой: {player['name']} против {enemy['name']}. Инициатива {player['initiative']} / {enemy['initiative']}.")
            else:
                if state['status'] != 'active':
                    raise ValueError('Нет активного боя.')
                rules = state['rules']
                turn = state['turn']
                if action == 'strike':
                    events.append(self.strike(state))
                elif action == 'shield':
                    if state['actions'] <= 0:
                        raise ValueError('Действия закончились.')
                    state['actions'] -= 1
                    state['fighters'][turn]['shield'] = True
                    events.append(state['fighters'][turn]['name'] + ' поднимает учебный щит.')
                elif action == 'end_turn':
                    self.end_turn(state)
                    events.append('Ход: ' + state['fighters'][state['turn']]['name'])
                elif action == 'enemy_turn' and turn == 1:
                    while state['actions'] and state['status'] == 'active':
                        events.append(self.strike(state))
                    if state['status'] == 'active':
                        self.end_turn(state)
                else:
                    raise ValueError('Недопустимое действие.')
            state['revision'] += 1
            for text in events:
                state['log'].append(text)
                envelope = {'schema_version': 1, 'type': 'chat.message', 'sender': 'second', 'text': text, 'visibility': {'mode': 'all', 'chat_ids': []}, 'choices': [], 'metadata': {'encounter_id': state['id'], 'combat_revision': state['revision']}, 'created_at': datetime.now(timezone.utc).isoformat(), 'combat': {'action': action, 'round': state['round']}}
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
        penalty = state['rules']['map'][min(state['attacks'], 2)]
        natural = self.roll_die(20)
        total = natural + attacker['attack'] + penalty
        dc = target['ac'] + (state['rules']['shield_bonus'] if target['shield'] else 0)
        outcome = degree(natural, total, dc)
        damage_roll = self.roll_die(attacker['damage_die']) if outcome >= 2 else 0
        damage = max(0, damage_roll + attacker['damage_bonus']) * (2 if outcome == 3 else 1) if outcome >= 2 else 0
        target['hp'] = max(0, target['hp'] - damage)
        state['actions'] -= 1
        state['attacks'] += 1
        state['last_attack'] = {'natural': natural, 'total': total, 'dc': dc, 'map': penalty, 'degree': outcome, 'damage_die': damage_roll, 'damage': damage}
        label = ['критический провал', 'промах', 'попадание', 'критическое попадание'][outcome]
        text = f"{attacker['name']}: d20={natural}, итог {total} против КБ {dc} — {label}; урон {damage}. ОЗ цели {target['hp']}/{target['max_hp']}."
        if target['hp'] == 0:
            state['status'], state['winner'] = 'finished', attacker['name']
            text += f" Учебный бой окончен. Победитель: {attacker['name']}."
        return text
