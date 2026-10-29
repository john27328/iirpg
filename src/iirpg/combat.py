"""Persistent team training encounters, separate from character saves."""
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
from .maps import zone_at, templates, install_map
from .combat_intents import parse_intent
from .scene_rules import perform_action, initial_scene
from .combat_actions import available_actions, strike_target, cover_target
from .combat_conditions import armor_class, status_penalty
from .demoralize import demoralize
from .gm_context import describe_proposal
from .intent_dialogue import append_message
from .combat_tactics import (active, communicate, may_surrender,
                             observe, remember_event, target_index, team)


class CombatStore:
    def __init__(self, roster: RosterStore, roll_die: Callable[[int], int]) -> None:
        self.roster, self.store, self.roll_die = roster, roster.store, roll_die
        self.rules = json.loads((Path(__file__).with_name('rules') / 'combat_pf2e.json').read_text())
        self.maps = {m['id']: m for m in templates()}
        self.scene_rules = json.loads((Path(__file__).with_name('rules') / 'scene_pf2e.json').read_text())
        self.skill_rules = json.loads((Path(__file__).with_name('rules') / 'demoralize_pf2e.json').read_text())
        self.presets = json.loads((Path(__file__).with_name('rules') / 'battle_presets.json').read_text())
        seen_presets = set()
        for preset in self.presets:
            if (set(preset) != {'id', 'name', 'description', 'map_template', 'group_id'}
                    or any(not isinstance(v, str) or not v or len(v) > 1000 for v in preset.values())
                    or preset['id'] in seen_presets or preset['map_template'] not in self.maps
                    or preset['group_id'] not in self.rules['groups']):
                raise ValueError('Некорректная заготовка боя.')
            seen_presets.add(preset['id'])
        with self.store._connection() as c:
            c.executescript('''CREATE TABLE IF NOT EXISTS encounters(room_id TEXT PRIMARY KEY REFERENCES rooms(id), body TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS encounter_requests(room_id TEXT NOT NULL REFERENCES rooms(id), request_id TEXT NOT NULL, command TEXT NOT NULL, response TEXT NOT NULL, PRIMARY KEY(room_id,request_id));''')

    def read(self, room: str) -> dict:
        with self.store._connection() as c:
            self.store._require_room(c, room)
            row = c.execute('SELECT body FROM encounters WHERE room_id=?', (room,)).fetchone()
        return json.loads(row[0]) if row else {'revision': 0, 'status': 'idle', 'rules': self.rules}

    def hydrate_skills(self, state: dict, roster: dict) -> None:
        from .character_sheet import CharacterRules
        state.setdefault('skill_rules', deepcopy(self.skill_rules))
        for key in ('targeting', 'outcomes'):
            state['skill_rules'].setdefault(key, deepcopy(self.skill_rules[key]))
        for fighter in state.get('fighters', []):
            if 'character_id' in fighter:
                character = next((a for a in roster['characters'] if a['id'] == fighter['character_id']), None)
                stats = CharacterRules(roster['character_rules']).statistics(character['sheet'], character['attributes']) if character else {}
            else:
                stats = self.rules['opponent']
            for key in ('will', 'intimidation'):
                if key in stats: fighter.setdefault(key, stats[key])

    def actions(self, room: str, revision: int, position: list[int] | None = None, target_id: str | None = None) -> dict:
        with self.store._connection() as c:
            c.execute('BEGIN')
            self.store._require_room(c, room)
            row = c.execute('SELECT body FROM encounters WHERE room_id=?', (room,)).fetchone()
            state = json.loads(row[0]) if row else {'revision': 0, 'status': 'idle'}
            if type(revision) is not int or revision != state['revision']:
                raise MessageConflict('Бой изменился. Обновите страницу.')
            scene_row = c.execute('SELECT body FROM scenes WHERE room_id=?', (room,)).fetchone() if c.execute("SELECT 1 FROM sqlite_master WHERE name='scenes'").fetchone() else None
            scene = json.loads(scene_row[0]) if scene_row else None
            roster = self.roster._load(c, room) if state['status'] == 'active' else None
            if roster: self.hydrate_skills(state, roster)
            return available_actions(state, scene, roster, position, target_id)

    def execute(self, room: str, command: dict, request_id: str) -> dict:
        try:
            UUID(request_id)
        except (ValueError, TypeError, AttributeError):
            raise ValueError('Нужен UUID Idempotency-Key.') from None
        if set(command) - {'action', 'revision', 'character_id', 'character_ids', 'actor_id', 'position', 'target_id', 'group_id', 'preset_id', 'text', 'intent_id', 'effect', 'summary', 'delivery', 'review', 'requested_action'}:
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
            input_action = action
            source_text = None
            resolved_intent = None
            events = []

            def snapshot() -> dict:
                return {key: deepcopy(state.get(key)) for key in
                        ('round', 'turn', 'actions', 'attacks', 'status', 'fighters')}

            def record(kind: str, text: str, before: dict) -> None:
                fighters = before.get('fighters') or []
                actor_index = before.get('turn')
                actor = fighters[actor_index]['name'] if fighters else None
                victim = state['last_attack']['target_index'] if kind == 'strike' else state['last_check']['target_index'] if kind == 'demoralize' else None
                event = {'action': kind, 'command': input_action, 'actor': actor,
                         'target': fighters[victim]['name'] if victim is not None else None,
                         'actor_index': actor_index if fighters else None,
                         'target_index': victim,
                         'before': before, 'after': snapshot()}
                if source_text:
                    event['source_text'] = source_text
                if resolved_intent:
                    event['intent_id'] = resolved_intent['id']
                if kind == 'strike':
                    event['roll'] = deepcopy(state['last_attack'])
                if kind == 'demoralize':
                    event['check'] = deepcopy(state['last_check'])
                events.append({'text': text, 'details': event})
                if fighters and kind not in ('intent', 'clarify_intent', 'reject_intent', 'offer_intent'):
                    remember_event(state, scene, kind, actor_index, victim)

            def report_orders() -> None:
                if state['status'] != 'active':
                    return
                before_order = snapshot()
                report = communicate(state, scene)
                if report:
                    record('communicate', report['text'], before_order)
                    events[-1]['details']['communication'] = report

            before = snapshot()
            if action == 'start':
                if state['status'] == 'active':
                    raise ValueError('Бой уже идёт.')
                preset = None
                scene_before_preset = None
                if 'preset_id' in command:
                    preset = next((p for p in self.presets if p['id'] == command['preset_id']), None)
                    if not preset:
                        raise ValueError('Выберите существующую заготовку боя.')
                    if 'group_id' in command and command['group_id'] != preset['group_id']:
                        raise ValueError('Группа противников должна соответствовать заготовке.')
                    if not c.execute("SELECT 1 FROM sqlite_master WHERE name='scenes'").fetchone():
                        raise ValueError('Для заготовки требуется подключённое хранилище сцен.')
                    scene = scene or initial_scene(room, self.scene_rules)
                    scene_before_preset = deepcopy(scene)
                    install_map(scene, self.maps[preset['map_template']])
                roster = self.roster._load(c, room)
                character_ids = command.get('character_ids', [command.get('character_id')])
                if 'character_ids' in command and 'character_id' in command:
                    raise ValueError('Передайте character_id или character_ids, не оба поля.')
                if not isinstance(character_ids, list) or not 1 <= len(character_ids) <= 4 or any(not isinstance(i, str) for i in character_ids) or len(set(character_ids)) != len(character_ids):
                    raise ValueError('Выберите от одного до четырёх разных героев.')
                from .character_sheet import CharacterRules
                heroes = []
                for character_id in character_ids:
                    actor = next((a for a in roster['characters'] if a['id'] == character_id and a['kind'] == 'pc'), None)
                    if not actor:
                        raise ValueError('Выберите игрового персонажа этой комнаты.')
                    stats = CharacterRules(roster['character_rules']).statistics(actor['sheet'], actor['attributes'])
                    if actor['sheet']['values'].get('hp_current', 0) <= 0:
                        raise ValueError('Сначала заполните положительные текущие ОЗ в листе: ' + actor['name'])
                    if 'armor_class' not in stats or 'perception' not in stats:
                        raise ValueError('Профиль не содержит КБ и Восприятие.')
                    strength = actor['attributes'].get('strength', 0)
                    owner = next((p['name'] for p in roster['players'] if p['id'] == actor.get('player_id')), None)
                    heroes.append({'name': actor['name'], 'character_id': actor['id'], 'entity_id': actor['id'],
                        'player_id': actor.get('player_id'), 'player_name': owner, 'team': 'players',
                        'hp': actor['sheet']['values']['hp_current'], 'ac': stats['armor_class'], 'perception': stats['perception'],
                        'attack': strength + actor['sheet']['values'].get('level', 1) + self.rules['player']['proficiency_offset'],
                        'damage_die': self.rules['player']['die'], 'damage_bonus': strength,
                        'speed': actor['sheet']['values'].get('speed', 25)})
                group_id = preset['group_id'] if preset else command.get('group_id', 'duel')
                if not isinstance(group_id, str):
                    raise ValueError('group_id должен быть строкой.')
                group = self.rules.get('groups', {}).get(group_id)
                if not group:
                    raise ValueError('Выберите существующую учебную группу.')
                enemies = [{**deepcopy(self.rules['opponent']), **deepcopy(member), 'team': 'enemies'} for member in group['members']]
                enemy = enemies[0]
                player = heroes[0]
                fighters = [*heroes, *enemies]
                encounter_id = str(uuid4())
                for i, member in enumerate(enemies, 1):
                    member['entity_id'] = f'training:{encounter_id}:{i}'
                    member.setdefault('languages', ['common'])
                for fighter in fighters:
                    fighter.update(max_hp=fighter['hp'], shield=False)
                    fighter['initiative_die'] = self.roll_die(20)
                    fighter['initiative'] = fighter['initiative_die'] + fighter['perception']
                # NPC wins tied initiative against PCs.
                order = sorted(range(len(fighters)), key=lambda i: (fighters[i]['initiative'], fighters[i]['team'] == 'enemies', -i), reverse=True)
                state = {'revision': state['revision'], 'id': encounter_id, 'status': 'active', 'rules': self.rules,
                         'fighters': fighters, 'turn': order[0], 'initiative_order': order, 'round': 1,
                         'actions': self.rules['actions'], 'attacks': 0, 'log': [], 'intents': [], 'journal': [], 'group_id': group_id, 'group_name': group['name']}
                self.hydrate_skills(state, roster)
                if preset:
                    state['preset_id'] = preset['id']
                    state['preset_name'] = preset['name']
                if scene and 'map' in scene:
                    scene['entities']=[e for e in scene['entities'] if e['kind']!='combatant']
                    occupied = []
                    for hero in heroes:
                        entity = next((e for e in scene['entities'] if e['id'] == hero['entity_id']), None)
                        position = list(entity['position']) if entity and 'position' in entity else list(scene['map']['spawn'])
                        if position in occupied:
                            possible = []
                            for y, row_text in enumerate(scene['map']['rows']):
                                for x, tile in enumerate(row_text):
                                    if tile != '#' and [x, y] not in occupied:
                                        path = route(scene, heroes[0]['position'], [x, y])
                                        if path: possible.append((len(path), [x, y]))
                            if not possible: raise ValueError('На карте недостаточно места для героев.')
                            position = min(possible)[1]
                        hero.update(position=position, cover=deepcopy(entity.get('cover')) if entity and entity.get('position') == position else None)
                        occupied.append(position)
                    position = player['position']
                    for member in enemies:
                        member.update(speed=25, cover=None)
                        possible=[]
                        for y,row_text in enumerate(scene['map']['rows']):
                            for x,tile in enumerate(row_text):
                                if tile=='#' or [x,y] in occupied:continue
                                path=route(scene,position,[x,y])
                                if path:possible.append((abs(len(path)-4),len(path),[x,y]))
                        if not possible:raise ValueError('На карте недостаточно свободных клеток для группы.')
                        member['position']=min(possible)[2]
                        occupied.append(member['position'])
                    state['mapped']=True
                    state['scene_id']=scene['id']
                observe(state, scene)
                record('start', f"Начат учебный бой: {', '.join(f['name'] for f in heroes)} против {', '.join(f['name'] for f in enemies)}. Инициатива " + ' / '.join(str(f['initiative']) for f in fighters) + '.', before)
                events[-1]['details'].update(actor=player['name'], target=enemy['name'], actor_index=0, target_index=len(heroes))
                if preset:
                    events[-1]['details'].update(preset_id=preset['id'], scene_before=scene_before_preset, scene_after=deepcopy(scene))
                report_orders()
            else:
                if state['status'] != 'active':
                    raise ValueError('Нет активного боя.')
                rules = state['rules']
                roster = self.roster._load(c, room)
                self.hydrate_skills(state, roster)
                turn = state['turn']
                actor_id = state['fighters'][turn].get('entity_id', f'fighter:{turn}')
                if 'actor_id' in command and command['actor_id'] != actor_id:
                    raise ValueError('Сейчас ход другого участника. Обновите состояние боя.')
                if state.get('mapped') and (not scene or 'map' not in scene):
                    raise ValueError('Карта боя недоступна.')
                if action in ('intent', 'clarify_intent', 'resolve_intent', 'offer_intent', 'accept_intent') and team(state, turn) != 'players':
                    raise ValueError('Текстовое действие сейчас может заявить только активный герой.')
                if action == 'intent':
                    source_text = command.get('text')
                    parsed = parse_intent(source_text, state, scene, command.get('target_id'))
                    if 'review' in command and type(command['review']) is not bool:
                        raise ValueError('review должен быть логическим значением.')
                    if parsed and not command.get('review'):
                        command = parsed
                        action = parsed['action']
                elif action in ('clarify_intent', 'resolve_intent', 'offer_intent', 'accept_intent'):
                    resolved_intent = next((i for i in state.get('intents', []) if i['id'] == command.get('intent_id') and i['status'] == 'pending'), None)
                    if not resolved_intent or resolved_intent['actor_id'] != actor_id:
                        raise ValueError('Намерение должно ожидать решения и принадлежать активному герою.')
                    if action == 'accept_intent':
                        proposal = resolved_intent.get('analysis')
                        if not proposal or proposal['effect'] in ('manual','need_target'):
                            raise ValueError('Сначала получите предложение реализуемого действия.')
                        offered = {'effect': proposal['effect'], **proposal['command'], 'summary': proposal['summary']}
                        current = describe_proposal(state, scene, roster, offered)
                        def mechanical_check(value):
                            return {k:v for k,v in (value or {}).items() if k not in ('targeting','outcomes')}
                        if mechanical_check(current.get('check')) != mechanical_check(proposal.get('check')):
                            raise ValueError('Условия проверки изменились. Разберите намерение заново.')
                        command = offered
                        action = proposal['effect']
                    elif action == 'resolve_intent':
                        action = command.get('effect')
                    if action not in ('clarify_intent', 'offer_intent', 'strike', 'shield', 'move', 'take_cover', 'tip_over', 'end_turn', 'reject_intent', 'demoralize'):
                        raise ValueError('Выберите реализуемое действие или отклоните намерение.')
                    source_text = resolved_intent['text']
                snapshot_before = snapshot()
                if action == 'intent':
                    intents = state.setdefault('intents', [])
                    if len(intents) >= 200:
                        raise ValueError('Достигнут лимит намерений этого боя.')
                    intent = {'id': str(uuid4()), 'actor_id': actor_id, 'actor_name': state['fighters'][turn]['name'],
                              'text': source_text.strip(), 'status': 'pending', 'round': state['round'],
                              'selected_target_id': command.get('target_id'), 'selected_position': command.get('position')}
                    intent['messages'] = [{'role': 'user', 'text': source_text.strip()}]
                    if command.get('requested_action') is not None:
                        requested = command['requested_action']
                        if not isinstance(requested, dict) or set(requested) - {'action','target_id','position','delivery'} or requested.get('action') not in ('strike','move','shield','take_cover','tip_over','demoralize','end_turn'):
                            raise ValueError('Нужна поддерживаемая команда действия.')
                        describe_proposal(state, scene, roster, {'effect': requested['action'], **{k:v for k,v in requested.items() if k!='action'}})
                        intent['requested_action'] = deepcopy(requested)
                    intents.append(intent)
                    record('intent', intent['actor_name'] + ': «' + intent['text'] + '» — ожидает решения ведущего; действия не потрачены.', before)
                    events[-1]['details']['intent_id'] = intent['id']
                elif action == 'clarify_intent':
                    text = command.get('text')
                    append_message(resolved_intent, 'user', text)
                    resolved_intent.pop('analysis', None)
                    resolved_intent.pop('requested_action', None)
                    if 'target_id' in command:resolved_intent['selected_target_id'] = command['target_id']
                    if 'position' in command:resolved_intent['selected_position'] = command['position']
                    record('clarify_intent', resolved_intent['actor_name'] + ' уточняет: «' + text.strip() + '» — предложение нужно разобрать заново; действия не потрачены.', before)
                elif action == 'offer_intent':
                    if not isinstance(command.get('summary'), str) or len(command['summary']) > 400:
                        raise ValueError('Объяснение предложения должно быть строкой до 400 символов.')
                    proposal = describe_proposal(state, scene, roster, command)
                    append_message(resolved_intent, 'assistant', proposal['summary'], proposal)
                    resolved_intent['analysis'] = proposal
                    check = proposal.get('check')
                    detail = (f"Запугивание {check['modifier']:+d} против КС Воли {check['dc']}, цель {check['target_name']}, одно действие; бросок ожидает подтверждения."
                              if check else 'Предложение ожидает решения игрока.' if proposal['effect'] not in ('manual','need_target') else 'Уточните замысел или цель.')
                    record('offer_intent', 'ИИ-ведущий: ' + proposal['summary'] + ' ' + detail, before)
                elif action == 'demoralize':
                    record('demoralize', demoralize(state, scene, command.get('target_id'), command.get('delivery','speech'), self.roll_die), before)
                elif action == 'reject_intent':
                    if not resolved_intent:
                        raise ValueError('Отклоняйте намерение через resolve_intent.')
                    record('reject_intent', 'Намерение отклонено: «' + source_text + '».', before)
                elif action == 'strike':
                    record('strike', self.strike(state, command.get('target_id')), snapshot_before)
                elif action == 'move':
                    if not state.get('mapped'):raise ValueError('Бой не связан с картой.')
                    record('move', move_fighter(state,scene,command.get('position')), before)
                elif action == 'take_cover':
                    target = cover_target(state, scene, command.get('target_id'))
                    fighter=state['fighters'][turn]
                    fighter['cover']={'object_id':target['id'],'bonus':scene['rules']['cover_bonus']}
                    state['actions']-=1
                    record('take_cover',fighter['name']+' занимает укрытие.',before)
                elif action == 'tip_over':
                    fighter = state['fighters'][turn]
                    if not state.get('mapped') or state['actions'] <= 0 or 'character_id' not in fighter:
                        raise ValueError('Для опрокидывания нужны действие и герой на карте.')
                    entity = next(e for e in scene['entities'] if e['id'] == actor_id)
                    result = perform_action(scene, entity, 'tip_over', command.get('target_id'), None,
                                            roster, self.roll_die, check_penalty=status_penalty(fighter))
                    fighter['cover'] = None
                    state['actions'] -= 1
                    record('tip_over', result['text'], before)
                    events[-1]['details']['check'] = result
                elif action == 'shield':
                    if state['actions'] <= 0:
                        raise ValueError('Действия закончились.')
                    state['actions'] -= 1
                    state['fighters'][turn]['shield'] = True
                    record('shield', state['fighters'][turn]['name'] + ' поднимает учебный щит.', before)
                elif action == 'end_turn':
                    self.end_turn(state)
                    record('end_turn', 'Конец хода. Ход: ' + state['fighters'][state['turn']]['name'], before)
                    report_orders()
                elif action == 'surrender':
                    if team(state, turn) != 'enemies' or not may_surrender(state, turn):
                        raise ValueError('Участник пока не готов сдаться.')
                    state['fighters'][turn]['surrendered'] = True
                    text = state['fighters'][turn]['name'] + ' сдаётся и выходит из учебного боя.'
                    self.finish_if_won(state)
                    if state['status'] == 'finished':
                        text += ' Учебный бой окончен. Победитель: ' + state['winner'] + '.'
                    record('surrender', text, before)
                    if state['status'] == 'active':
                        before_turn = snapshot()
                        self.end_turn(state)
                        record('end_turn', 'Ход: ' + state['fighters'][state['turn']]['name'], before_turn)
                        report_orders()
                elif action == 'enemy_turn' and team(state, turn) == 'enemies':
                    while state['actions'] and state['status'] == 'active':
                        snapshot_before = snapshot()
                        victim = target_index(state)
                        if state.get('mapped') and distance(state['fighters'][turn]['position'],state['fighters'][victim]['position'])!=1:
                            destination=approach(state,scene,victim)
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
                        report_orders()
                else:
                    raise ValueError('Недопустимое действие.')
                if resolved_intent and action not in ('offer_intent', 'clarify_intent'):
                    resolved_intent.update(status='rejected' if action == 'reject_intent' else 'resolved', effect=action)
                    if action == 'demoralize':resolved_intent['check_result'] = deepcopy(state['last_check'])
            if state.get('mapped'):
                for fighter in state['fighters']:
                    entity=next((e for e in scene['entities'] if e['id']==fighter['entity_id']),None)
                    if entity is None:
                        entity={'id':fighter['entity_id'],'kind':'actor' if 'character_id' in fighter else 'combatant','name':fighter['name']}
                        if 'character_id' in fighter:entity['character_id']=fighter['character_id']
                        scene['entities'].append(entity)
                    entity.update(position=fighter['position'],zone_id=zone_at(scene['map'],fighter['position']),cover=fighter.get('cover'),training_hp=fighter['hp'], surrendered=fighter.get('surrendered', False))
                scene['revision']+=1
                c.execute('INSERT INTO scenes VALUES(?,?) ON CONFLICT(room_id) DO UPDATE SET body=excluded.body',(room,json.dumps(scene,ensure_ascii=False)))
            state['revision'] += 1
            for index, event in enumerate(events):
                text, details = event['text'], event['details']
                details.update(request_id=request_id, step=index, round=state['round'] if details['action'] == 'start' else details['before']['round'])
                state['log'].append(text)
                timestamp = datetime.now(timezone.utc).isoformat()
                state.setdefault('journal', []).append({'id': f'{request_id}:{index}', 'text': text, 'action': details['action'],
                    'actor': details['actor'], 'target': details['target'], 'round': details['round'], 'created_at': timestamp,
                    'source_text': details.get('source_text'), 'intent_id': details.get('intent_id')})
                envelope = {'schema_version': 1, 'type': 'combat.action', 'sender': 'second', 'text': text, 'visibility': {'mode': 'all', 'chat_ids': []}, 'choices': [], 'metadata': {'encounter_id': state['id'], 'combat_revision': state['revision']}, 'created_at': timestamp, 'combat': details}
                c.execute('INSERT INTO queue_events(room_id,request_id,envelope) VALUES(?,?,?)', (room, str(uuid4()), json.dumps(envelope, ensure_ascii=False)))
            response = json.dumps(state, ensure_ascii=False)
            c.execute('INSERT INTO encounters VALUES(?,?) ON CONFLICT(room_id) DO UPDATE SET body=excluded.body', (room, response))
            c.execute('INSERT INTO encounter_requests VALUES(?,?,?,?)', (room, request_id, encoded, response))
            return state

    def end_turn(self, state: dict) -> None:
        previous = state['fighters'][state['turn']]
        if previous.get('frightened', 0):previous['frightened'] -= 1
        order = state.get('initiative_order', list(range(len(state['fighters']))))
        current = order.index(state['turn'])
        for step in range(1, len(order)+1):
            following = (current + step) % len(order)
            if active(state['fighters'][order[following]]):
                state['turn'] = order[following]
                if current + step >= len(order):
                    state['round'] += 1
                break
        state['turns_elapsed'] = state.get('turns_elapsed', 0) + 1
        state['actions'], state['attacks'] = state['rules']['actions'], 0
        state['fighters'][state['turn']]['shield'] = False

    def strike(self, state: dict, victim_id: str | None = None) -> str:
        victim = strike_target(state, victim_id)
        attacker, target = state['fighters'][state['turn']], state['fighters'][victim]
        attacker['cover']=None
        penalty = state['rules']['map'][min(state['attacks'], 2)]
        natural = self.roll_die(20)
        total = natural + attacker['attack'] + penalty - status_penalty(attacker)
        dc = armor_class(state, target)
        outcome = degree(natural, total, dc)
        damage_roll = self.roll_die(attacker['damage_die']) if outcome >= 2 else 0
        damage = max(0, damage_roll + attacker['damage_bonus']) * (2 if outcome == 3 else 1) if outcome >= 2 else 0
        target['hp'] = max(0, target['hp'] - damage)
        state['actions'] -= 1
        state['attacks'] += 1
        state['last_attack'] = {'natural': natural, 'total': total, 'dc': dc, 'map': penalty, 'degree': outcome, 'damage_die': damage_roll, 'damage': damage, 'target_index': victim}
        label = ['критический провал', 'промах', 'попадание', 'критическое попадание'][outcome]
        text = f"{attacker['name']} — удар мечом: d20={natural}, итог {total} против КБ {dc} — {label}; урон {damage}. ОЗ цели {target['hp']}/{target['max_hp']}."
        self.finish_if_won(state)
        if state['status'] == 'finished':
            text += f" Учебный бой окончен. Победитель: {state['winner']}."
        return text

    def finish_if_won(self, state: dict) -> None:
        sides = {team(state, i) for i, f in enumerate(state['fighters']) if active(f)}
        if len(sides) == 1:
            state['status'] = 'finished'
            state['winner_team'] = next(iter(sides))
            winners = [f['name'] for i, f in enumerate(state['fighters']) if team(state, i) == state['winner_team'] and active(f)]
            state['winner'] = ', '.join(winners)
