"""Teams, local observations and audible orders for the spatial prototype.

Vision, hearing ranges and morale are authored simulation settings, not a full
implementation of Pathfinder detection or encounter difficulty rules.
"""
from collections import deque
from copy import deepcopy

from .combat_map import distance


def team(state: dict, index: int) -> str:
    return state['fighters'][index].get('team', 'players' if index == 0 else 'enemies')


def fighter_id(state: dict, index: int) -> str:
    return state['fighters'][index].get('entity_id', f'fighter:{index}')


def active(fighter: dict) -> bool:
    return fighter['hp'] > 0 and not fighter.get('surrendered', False)


def opponents(state: dict, index: int) -> list[int]:
    return [i for i, f in enumerate(state['fighters']) if team(state, i) != team(state, index) and active(f)]


def target_index(state: dict, target_id: str | None = None) -> int:
    choices = opponents(state, state['turn'])
    if target_id is None and choices:
        return choices[0]
    for i in choices:
        if fighter_id(state, i) == target_id:
            return i
    raise ValueError('Выберите активного противника, а не союзника или выбывшего бойца.')


def settings(state: dict) -> dict:
    return state['rules'].get('coordination', {'vision_feet': 60, 'hearing_feet': 30, 'order_ttl_turns': 4})


def visible_position(state: dict, scene: dict | None, observer: int, position: list[int]) -> bool:
    if not state.get('mapped'):
        return True
    start = state['fighters'][observer]['position']
    if distance(start, position) * 5 > settings(state)['vision_feet']:
        return False
    rows = scene['map']['rows']
    x, y = start
    dx, dy = position[0] - x, position[1] - y
    nx, ny = abs(dx), abs(dy)
    sx, sy = (1 if dx > 0 else -1), (1 if dy > 0 else -1)
    ix = iy = 0
    while ix < nx or iy < ny:
        decision = (1 + 2 * ix) * ny - (1 + 2 * iy) * nx
        if decision == 0:
            # Conservative supercover: no seeing diagonally through a wall corner.
            if rows[y][x + sx] == '#' or rows[y + sy][x] == '#':
                return False
            x += sx; y += sy; ix += 1; iy += 1
        elif decision < 0:
            x += sx; ix += 1
        else:
            y += sy; iy += 1
        if rows[y][x] == '#':
            return False
    return True


def can_see(state: dict, scene: dict | None, observer: int, target: int) -> bool:
    return not state.get('mapped') or visible_position(state, scene, observer, state['fighters'][target]['position'])


def can_hear(state: dict, scene: dict | None, observer: int, speaker: int, require_language: bool = True) -> bool:
    if not active(state['fighters'][observer]) or not active(state['fighters'][speaker]):
        return False
    if require_language and not set(state['fighters'][observer].get('languages', ['common'])) & set(state['fighters'][speaker].get('languages', ['common'])):
        return False
    if not state.get('mapped'):
        return True
    rows = scene['map']['rows']
    start = tuple(state['fighters'][speaker]['position'])
    goal = tuple(state['fighters'][observer]['position'])
    limit = settings(state)['hearing_feet'] // 5
    queue = deque([(start, 0)])
    visited = {start}
    while queue:
        (x, y), steps = queue.popleft()
        if (x, y) == goal:
            return True
        if steps >= limit:
            continue
        for nx, ny in ((x-1, y), (x+1, y), (x, y-1), (x, y+1)):
            if 0 <= ny < len(rows) and 0 <= nx < len(rows[0]) and rows[ny][nx] != '#' and (nx, ny) not in visited:
                visited.add((nx, ny)); queue.append(((nx, ny), steps+1))
    return False


def health(fighter: dict) -> str:
    if fighter.get('surrendered'):
        return 'surrendered'
    if fighter['hp'] == 0:
        return 'down'
    ratio = fighter['hp'] / fighter['max_hp']
    return 'badly_wounded' if ratio <= .35 else 'wounded' if ratio < .75 else 'fit'


def observe(state: dict, scene: dict | None) -> None:
    memories = state.setdefault('awareness', {})
    for observer, fighter in enumerate(state['fighters']):
        if not active(fighter):
            continue
        memory = memories.setdefault(fighter_id(state, observer), {'contacts': {}, 'events': [], 'order': None})
        if state.get('mapped'):
            known_objects = memory.setdefault('objects', {e['id']: deepcopy(e) for e in scene['entities'] if e['kind'] == 'object'})
            for entity in scene['entities']:
                if entity['kind'] == 'object' and visible_position(state, scene, observer, entity['position']):
                    known_objects[entity['id']] = deepcopy(entity)
        visible_ids = set()
        for target, other in enumerate(state['fighters']):
            if target == observer or not can_see(state, scene, observer, target):
                continue
            visible_ids.add(fighter_id(state, target))
            memory['contacts'][fighter_id(state, target)] = {
                'id': fighter_id(state, target), 'name': other['name'], 'team': team(state, target),
                'condition': health(other), 'shield': other['shield'], 'cover': bool(other.get('cover')),
                'position': deepcopy(other.get('position')), 'seen_round': state['round'],
                'observed_turn': state.get('turns_elapsed', 0), 'source': 'seen',
                'observed_revision': state['revision'],
                'shaken': other.get('frightened', 0) > 0,
            }
        for contact in memory['contacts'].values():
            if state.get('mapped') and contact['id'] not in visible_ids and contact.get('position') and visible_position(state, scene, observer, contact['position']):
                contact['last_position_checked_empty'] = True


def remember_event(state: dict, scene: dict | None, action: str, actor: int, target: int | None = None) -> None:
    observe(state, scene)
    for observer, fighter in enumerate(state['fighters']):
        if not active(fighter) or not (observer == actor or observer == target or can_see(state, scene, observer, actor)):
            continue
        memory = state['awareness'][fighter_id(state, observer)]
        event = {'action': action, 'actor': fighter_id(state, actor), 'round': state['round'],
                 'turn': state.get('turns_elapsed', 0)}
        if state.get('mapped') and (observer == actor or can_see(state, scene, observer, actor)):
            event['actor_position'] = deepcopy(state['fighters'][actor]['position'])
        if target is not None and (target == observer or can_see(state, scene, observer, target)):
            event['target'] = fighter_id(state, target)
        if action == 'strike' and (observer == actor or observer == target):
            event['degree'] = state['last_attack']['degree']
            if observer == actor:
                event['attack_penalty'] = state['last_attack']['map']
            if observer == target:
                event['damage_taken'] = state['last_attack']['damage']
        memory['events'] = (memory['events'] + [event])[-8:]


def communicate(state: dict, scene: dict | None) -> dict | None:
    """One brief report on the speaker's turn; recipients hear the same staleable facts."""
    speaker = state['turn']
    allies = [i for i, f in enumerate(state['fighters']) if i != speaker and team(state, i) == team(state, speaker) and active(f)]
    if team(state, speaker) != 'enemies' or not allies:
        return None
    observe(state, scene)
    sender_id = fighter_id(state, speaker)
    memory = state['awareness'][sender_id]
    heard_by = [i for i in range(len(state['fighters'])) if i != speaker and can_hear(state, scene, i, speaker)]
    recipients = [i for i in allies if i in heard_by]
    own = state['fighters'][speaker]
    contacts = list(memory['contacts'].values()) + [{
        'id': sender_id, 'name': own['name'], 'team': team(state, speaker), 'condition': health(own),
        'shield': own['shield'], 'cover': bool(own.get('cover')), 'position': deepcopy(own.get('position')),
        'seen_round': state['round'], 'observed_turn': state.get('turns_elapsed', 0),
        'observed_revision': state['revision'], 'source': 'self_report',
    }]
    known_enemies = [c for c in contacts if c['team'] != team(state, speaker) and c['condition'] not in ('down', 'surrendered')]
    known_losses = [c for c in contacts if c['team'] == team(state, speaker) and c['condition'] in ('down', 'surrendered')]
    leaders = [i for i, f in enumerate(state['fighters']) if team(state, i) == 'enemies' and active(f) and f.get('role') == 'leader']
    is_leader = speaker in leaders
    order = None
    if is_leader:
        mode = 'withdraw' if health(own) == 'badly_wounded' or known_losses else 'advance'
        order = {'issuer': sender_id, 'mode': mode, 'issued_turn': state.get('turns_elapsed', 0),
                 'round': state['round'], 'target': deepcopy(known_enemies[0]) if known_enemies else None,
                 'text': 'Отходим, сохраняем людей!' if mode == 'withdraw' else 'Держимся вместе; передний сближается, второй прикрывает, резерв не теснит своих.'}
        memory['order'] = deepcopy(order)
    for receiver in recipients:
        other_memory = state['awareness'][fighter_id(state, receiver)]
        # Do not overwrite a more recent direct observation with an old report.
        for contact in contacts:
            old = other_memory['contacts'].get(contact['id'])
            if old is None or old.get('observed_revision', -1) < contact.get('observed_revision', -1):
                other_memory['contacts'][contact['id']] = {**deepcopy(contact), 'source': 'heard', 'reported_by': sender_id}
        if order:
            other_memory['order'] = deepcopy(order)
    if not recipients:
        return None
    text = own['name'] + ': ' + (order['text'] if order else 'Докладываю видимое положение и состояние участников.')
    if state.get('mapped'):
        text += f" Моя позиция {own['position']}, состояние: {health(own)}."
    return {'speaker': sender_id, 'recipients': [fighter_id(state, i) for i in recipients],
            'heard_by': [fighter_id(state, i) for i in heard_by], 'order': order, 'text': text}


def may_surrender(state: dict, index: int) -> bool:
    own = state['fighters'][index]
    memory = state.get('awareness', {}).get(fighter_id(state, index), {})
    order = memory.get('order') or {}
    fresh = state.get('turns_elapsed', 0) - order.get('issued_turn', -100) <= settings(state)['order_ttl_turns']
    losses = any(c['team'] == team(state, index) and c['condition'] in ('down', 'surrendered')
                 for c in memory.get('contacts', {}).values())
    return health(own) == 'badly_wounded' or losses or (fresh and order.get('mode') == 'withdraw')
