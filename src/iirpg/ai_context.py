"""NPC-local observations, executable choices and their immediate consequences."""
from copy import deepcopy

from .combat_map import approach, distance, move_fighter, route
from .combat_tactics import (active, can_see, fighter_id, health, may_surrender,
                             observe, settings, team)


def npc_context(state: dict, scene: dict, profile: dict) -> tuple[dict, dict[str, dict]]:
    state = deepcopy(state)
    observe(state, scene)
    index = state['turn']
    actor = state['fighters'][index]
    actor_id = fighter_id(state, index)
    memory = state['awareness'][actor_id]
    if state.get('mapped'):
        scene = deepcopy(scene)
        scene['entities'] = [e for e in scene['entities'] if e['kind'] != 'object'] + list(memory['objects'].values())
    contacts = []
    planning = deepcopy(state)
    for i, other in enumerate(state['fighters']):
        if i == index:
            continue
        contact = memory['contacts'].get(fighter_id(state, i))
        if not contact:
            planning['fighters'][i].update(hp=0, position=[-100, -100])
            continue
        visible = can_see(state, scene, index, i)
        contacts.append({**deepcopy(contact), 'visible_now': visible})
        # Menus must not expose an unseen creature's current location or health.
        planning['fighters'][i].update(hp=0 if contact['condition'] in ('down', 'surrendered') or contact.get('last_position_checked_empty') else 1,
                                       position=deepcopy(contact['position']), surrendered=contact['condition'] == 'surrendered')
    foes = [c for c in contacts if c['team'] != team(state, index) and c['condition'] not in ('down', 'surrendered')]
    threats = [c for c in foes if not c.get('last_position_checked_empty')]
    allies = [c for c in contacts if c['team'] == team(state, index)]
    by_id = {fighter_id(state, i): i for i in range(len(state['fighters']))}
    commands = {'end_turn': {'action': 'end_turn'}}
    consequences = {'end_turn': {'cost': 'remaining_actions', 'effect': 'Передать ход следующему активному участнику.'}}
    moves = {}
    penalty = state['rules']['map'][min(state['attacks'], 2)]
    own_ac = actor['ac'] + max(state['rules']['shield_bonus'] if actor['shield'] else 0,
                              (actor.get('cover') or {}).get('bonus', 0))
    if state['actions'] > 0:
        for contact in foes:
            if contact['visible_now'] and (not state.get('mapped') or distance(actor['position'], contact['position']) == 1):
                key = 'strike' if 'strike' not in commands else 'strike_' + str(by_id[contact['id']])
                commands[key] = {'action': 'strike', 'target_id': contact['id']}
                consequences[key] = {'cost': 1, 'target': contact['id'], 'attack_modifier': actor['attack'] + penalty,
                                     'repeat_penalty': penalty, 'loses_cover': bool(actor.get('cover')),
                                     'target_defence': 'unknown', 'target_condition': contact['condition']}
        if not actor['shield']:
            commands['shield'] = {'action': 'shield'}
            consequences['shield'] = {'cost': 1, 'effective_ac_after': actor['ac'] + max(state['rules']['shield_bonus'], (actor.get('cover') or {}).get('bonus', 0)),
                                      'ac_gain': max(0, actor['ac'] + state['rules']['shield_bonus'] - own_ac),
                                      'expires': 'start_of_own_next_turn'}
        if may_surrender(state, index):
            commands['surrender'] = {'action': 'surrender'}
            consequences['surrender'] = {'effect': 'Сдаться, прекратить участие в учебном бою; исходные ОЗ листа сохраняются.'}
        if state.get('mapped'):
            destinations = []
            for contact in threats:
                position = approach(planning, scene, by_id[contact['id']])
                if position:
                    destinations.append((position, 'close_distance' if contact['visible_now'] else 'search_last_seen'))
            x, y = actor['position']
            neighbours = [[x-1, y], [x+1, y], [x, y-1], [x, y+1]]
            # Reach cover with one stride, rather than shuffling one square per action.
            cover_positions = []
            for entity in scene['entities']:
                if entity['kind'] != 'object' or not entity['provides_cover']:
                    continue
                if distance(actor['position'], entity['position']) == 1:
                    if (actor.get('cover') or {}).get('object_id') != entity['id']:
                        key = 'cover_' + entity['id']
                        commands[key] = {'action': 'take_cover', 'target_id': entity['id']}
                        bonus = scene['rules']['cover_bonus']
                        consequences[key] = {'cost': 1, 'ac_gain': max(0, actor['ac'] + bonus - own_ac),
                                             'effective_ac_after': actor['ac'] + max(bonus, state['rules']['shield_bonus'] if actor['shield'] else 0),
                                             'ends_on': ['strike', 'move']}
                cx, cy = entity['position']
                cover_positions.extend([[cx-1, cy], [cx+1, cy], [cx, cy-1], [cx, cy+1]])
            nearby_cover = sorted(cover_positions, key=lambda p: distance(actor['position'], p))[:8]
            destinations.extend((p, 'reach_cover') for p in nearby_cover)
            destinations.extend((p, 'local_step') for p in neighbours)
            # A bounded set of retreat destinations, checked by the same movement rule.
            if threats:
                rows = scene['map']['rows']
                floor = [[cx, cy] for cy, row in enumerate(rows) for cx, tile in enumerate(row)
                         if tile != '#' and 0 < distance(actor['position'], [cx, cy]) * 5 <= actor['speed']]
                away = sorted(floor, key=lambda p: (-min(distance(p, c['position']) for c in threats), distance(p, actor['position'])))
                found = 0
                for position in away:
                    candidate = deepcopy(planning)
                    try:
                        move_fighter(candidate, scene, position)
                    except ValueError:
                        continue
                    destinations.insert(min(1 + found, len(destinations)), (position, 'disengage'))
                    found += 1
                    if found == 2:
                        break
            occupied = [f['position'] for i, f in enumerate(planning['fighters']) if i != index and active(f)]
            for position, purpose in destinations:
                if len(moves) >= 10:
                    break
                candidate = deepcopy(planning)
                try:
                    move_fighter(candidate, scene, position)
                except ValueError:
                    continue
                key = f'move_{position[0]}_{position[1]}'
                if key in moves:
                    continue
                path = route(scene, actor['position'], position, occupied)
                feet = sum(10 if scene['map']['rows'][p[1]][p[0]] == '~' else 5 for p in path[1:])
                closest = min((distance(position, c['position']) for c in threats), default=None)
                commands[key] = {'action': 'move', 'position': position}
                moves[key] = {'cost': 1, 'purpose': purpose, 'feet_travelled': feet,
                              'actions_left_after': state['actions'] - 1,
                              'can_strike_after_move': any(c['visible_now'] and distance(position, c['position']) == 1 for c in foes),
                              'distance_to_nearest_known_opponent_feet': closest * 5 if closest is not None else None,
                              'near_cover': any(p == position for p in cover_positions), 'loses_cover': bool(actor.get('cover'))}
                consequences[key] = moves[key]
    order = deepcopy(memory.get('order'))
    if order:
        order['age_turns'] = state.get('turns_elapsed', 0) - order['issued_turn']
        order['stale'] = order['age_turns'] > settings(state)['order_ttl_turns']
    intent = 'engage'
    if health(actor) == 'badly_wounded':
        intent = 'protect_self'
    elif order and not order['stale'] and order['mode'] == 'withdraw':
        intent = 'withdraw_together'
    elif any(c['condition'] in ('down', 'surrendered') for c in allies):
        intent = 'regroup_or_withdraw_after_loss'
    elif any(c['visible_now'] and c['condition'] == 'badly_wounded' for c in allies):
        intent = 'cover_wounded_ally'
    elif actor.get('role') == 'reserve':
        intent = 'cover_allies_without_blocking_their_path'
    elif actor.get('role') == 'guard':
        intent = 'support_visible_leader_and_use_a_free_approach'
    if intent == 'engage' and not any(c['visible_now'] for c in foes):
        intent = 'search_last_seen_or_regroup' if threats else 'observe_and_protect_self'
    own_fields = ('name', 'hp', 'max_hp', 'ac', 'attack', 'damage_die', 'damage_bonus', 'position', 'speed', 'shield', 'cover', 'role')
    own_events = [e for e in memory['events'] if e['actor'] == actor_id and e['action'] == 'strike'
                  and e['turn'] == state.get('turns_elapsed', 0)]
    own_actions = [e for e in memory['events'] if e['actor'] == actor_id and e['turn'] == state.get('turns_elapsed', 0)]
    priorities = []
    defensive = intent in ('protect_self', 'withdraw_together', 'regroup_or_withdraw_after_loss')
    if defensive:
        if state.get('mapped') and threats:
            current_distance = min(distance(actor['position'], c['position']) * 5 for c in threats)
            safer = [k for k, effect in moves.items() if effect['purpose'] == 'disengage'
                     and effect['distance_to_nearest_known_opponent_feet'] > current_distance]
            priorities.extend({'choice': k, 'reason': 'Увеличивает дистанцию; не возвращайся сразу в ближний бой после отхода.'} for k in safer[:1])
        if 'surrender' in commands:
            priorities.append({'choice': 'surrender', 'reason': 'Можно прекратить бой ради самосохранения, особенно если отход не помогает.'})
    else:
        if penalty == 0:
            priorities.extend({'choice': k, 'reason': 'Противник досягаем; первый удар без штрафа.'}
                              for k, cmd in commands.items() if cmd['action'] == 'strike')
        if intent not in ('cover_allies_without_blocking_their_path', 'observe_and_protect_self'):
            priorities.extend({'choice': k, 'reason': 'Одно сближение оставляет действия и позволяет ударить вместо серии коротких шагов.'}
                              for k, effect in moves.items() if effect['purpose'] == 'close_distance'
                              and effect['can_strike_after_move'] and effect['actions_left_after'] > 0)
    if defensive or penalty < 0 or state['actions'] == 1 or actor.get('role') == 'reserve':
        priorities.extend({'choice': k, 'reason': 'Полезное усиление защиты с учётом уже действующих бонусов.'}
                          for k, effect in consequences.items() if effect.get('ac_gain', 0) > 0)
    if not priorities:
        priorities.append({'choice': 'end_turn', 'reason': 'Можно сохранить позицию и передать ход; не обязательно расходовать всё.'})
    context = {
        'schema_version': 2, 'role': 'npc', 'mode': 'local_observations', 'actor_id': actor_id,
        'personality': profile['personality'], 'goal': profile['goal'], 'current_intent': intent,
        'self': {key: deepcopy(actor[key]) for key in own_fields if key in actor},
        'self_effective_ac': own_ac, 'self_condition': health(actor),
        'opponents': foes, 'allies': allies, 'received_order': order,
        'round': state['round'], 'actions_left': state['actions'], 'attack_penalty': penalty,
        'own_actions_this_turn': own_actions,
        'priority_candidates': priorities[:4],
        'decision_brief': 'Сначала оцени приоритетные варианты для current_intent. Это подсказки по известной обстановке, а не приказ: разрешено выбрать другое доступное действие. Сохраняй последовательность действий внутри хода.',
        'own_attacks_this_turn': own_events, 'recent_events': deepcopy(memory['events'][-6:]),
        'rules': 'Три действия за ход. Удар, щит, укрытие и движение стоят одно действие. '
                 'Штраф повторных атак: 0/-5/-10. Щит и укрытие не складываются. '
                 'Удар и движение снимают укрытие. Реакции и механический бонус окружения пока отсутствуют.',
        'decision_guidance': [
            'Выбери действие для current_intent по текущим наблюдениям. Приказ — ориентир, его можно пересмотреть при опасности.',
            'Если цель — сблизиться и атаковать, сравни полное сближение с коротким шагом: оба стоят действие.',
            'Обычно не трать третью атаку со штрафом -10, если можно получить полезную защиту или сменить позицию.',
            'Щит/укрытие без прироста КБ не улучшают защиту. Если собираешься атаковать, не занимай снимаемое атакой укрытие заранее.',
            'Союзники занимают клетки. Не закрывай им проход. Резерв прикрывает, а не обязательно бежит вплотную к цели.',
            'Тяжёлое ранение, замеченная потеря товарища или приказ об отходе — повод отступить, прикрыться или сдаться.',
            'У невидимых сейчас контактов известна только последняя позиция. Не считай её текущей; чужие ОЗ и КБ неизвестны.',
        ],
        'available_actions': commands, 'action_consequences': consequences, 'movement_outcomes': moves,
        'knowledge_limits': {'vision_feet': settings(state)['vision_feet'], 'hearing_feet': settings(state)['hearing_feet'],
                             'communications': 'Короткий отчёт в начале своего хода слышат только участники в пределах слышимости, понимающие язык; нет автоматической ретрансляции.'},
    }
    if state.get('mapped'):
        context['map'] = {'rows': scene['map']['rows'],
                          'legend': '# стена и преграда зрению, . пол, ~ трудная местность; клетка 5 футов; ортогональное движение. Геометрия карты заранее известна.',
                          'objects': [{key: deepcopy(e[key]) for key in ('id', 'position', 'provides_cover', 'blocks_movement')}
                                      for e in scene['entities'] if e['kind'] == 'object']}
    return context, commands
