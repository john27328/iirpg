"""Read-only, contextual action choices using the execution preconditions."""
from .combat_map import distance, movement_cost
from .combat_tactics import active, fighter_id, target_index, team
from .scene_rules import prepare_tip_over
from .combat_conditions import status_penalty
from .demoralize import prepare_demoralize


def strike_target(state: dict, victim_id: str | None = None) -> int:
    if state['actions'] <= 0:
        raise ValueError('Действия закончились.')
    victim = target_index(state, victim_id)
    actor, target = state['fighters'][state['turn']], state['fighters'][victim]
    if state.get('mapped') and distance(actor['position'], target['position']) != 1:
        raise ValueError('Цель вне досягаемости: подойдите на соседнюю клетку.')
    return victim


def cover_target(state: dict, scene: dict | None, object_id: str | None) -> dict:
    if not state.get('mapped') or state['actions'] <= 0:
        raise ValueError('Нельзя занять укрытие.')
    target = next((e for e in scene['entities'] if e['kind'] == 'object' and e['id'] == object_id), None)
    if not target or not target['provides_cover'] or distance(state['fighters'][state['turn']]['position'], target['position']) != 1:
        raise ValueError('Рядом должно быть подходящее укрытие.')
    return target


def available_actions(state: dict, scene: dict | None, roster: dict | None,
                      position: list[int] | None = None, victim_id: str | None = None) -> dict:
    result = {'revision': state['revision'], 'scene_revision': scene['revision'] if scene else None,
              'actor_id': None, 'position': position, 'general': [], 'selected': [], 'selection_label': None}
    if state.get('status') != 'active' or team(state, state['turn']) != 'players':
        return result
    index = state['turn'];actor = state['fighters'][index]
    result['actor_id'] = fighter_id(state, index)
    def choice(action, label, cost=1, **parameters):
        return {'label': label, 'cost': cost, 'command': {'action': action, **parameters}}
    if state['actions'] > 0 and not actor['shield']:
        result['general'].append(choice('shield', 'Поднять щит'))
    result['general'].append(choice('end_turn', 'Закончить ход', 0))
    if state.get('mapped'):
        if position is None:
            return result
        rows = scene['map']['rows']
        if (not isinstance(position, list) or len(position) != 2 or any(type(n) is not int for n in position)
                or not 0 <= position[1] < len(rows) or not 0 <= position[0] < len(rows[0])):
            raise ValueError('Выберите клетку карты [x,y].')
        names = [f['name'] for f in state['fighters'] if f.get('position') == position]
        objects = [e for e in scene['entities'] if e['kind'] == 'object' and e.get('position') == position]
        names.extend(e['name'] for e in objects)
        result['selection_label'] = f'Клетка [{position[0]},{position[1]}]' + (' · ' + ', '.join(names) if names else '')
        if state['actions'] <= 0:
            return result
        try:
            feet = movement_cost(state, scene, position)
            result['selected'].append({**choice('move', 'Переместиться', position=position), 'distance_feet': feet})
        except ValueError:
            pass
        victims = [i for i, f in enumerate(state['fighters']) if f.get('position') == position and active(f) and team(state, i) != team(state, index)]
        for obj in objects:
            try:
                cover_target(state, scene, obj['id'])
                if (actor.get('cover') or {}).get('object_id') != obj['id']:
                    result['selected'].append(choice('take_cover', 'Занять укрытие', target_id=obj['id']))
            except ValueError:
                pass
            entity = next((e for e in scene['entities'] if e['id'] == result['actor_id']), None)
            if entity and 'character_id' in actor:
                try:
                    prepare_tip_over(scene, entity, obj['id'], None, roster, status_penalty(actor))
                    result['selected'].append(choice('tip_over', 'Опрокинуть объект', target_id=obj['id']))
                except ValueError:
                    pass
    else:
        if not victim_id:
            return result
        victims = [i for i, f in enumerate(state['fighters']) if fighter_id(state, i) == victim_id]
        if victims:
            result['selection_label'] = state['fighters'][victims[0]]['name']
    for victim in victims:
        target_id = fighter_id(state, victim)
        if state.get('skill_rules'):
            try:
                prepare_demoralize(state, scene, target_id)
                result['selected'].append(choice('demoralize', 'Деморализовать', target_id=target_id))
            except ValueError:
                pass
        try:
            strike_target(state, target_id)
            result['selected'].append(choice('strike', 'Ударить', target_id=target_id))
        except ValueError:
            pass
    return result
