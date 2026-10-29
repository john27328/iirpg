"""Scene action effects without persistence or HTTP dependencies."""
from typing import Callable
from copy import deepcopy
from .character_sheet import CharacterRules
from .checks import degree
from .roster import integer
from .maps import move_on_map


def initial_scene(room: str, rules: dict) -> dict:
    return {'revision': 0, 'id': room, 'name': 'Придорожная таверна', 'rules': deepcopy(rules),
            'zones': deepcopy(rules['zones']), 'entities': [dict(obj, kind='object') for obj in rules['objects']], 'proposals': []}


def prepare_tip_over(state: dict, actor: dict, target_id: str, dc: int | None, roster: dict, check_penalty: int = 0) -> tuple[dict, int, int]:
    target = next((e for e in state['entities'] if e['id'] == target_id and e['kind'] == 'object'), None)
    if not target or target['zone_id'] != actor['zone_id']:
        raise ValueError('Объект должен находиться в той же зоне.')
    if 'map' in state and sum(abs(a-b) for a,b in zip(actor['position'], target['position'])) > 1:
        raise ValueError('Подойдите вплотную к объекту.')
    if target['posture'] != 'upright':
        raise ValueError('Объект нельзя опрокинуть этим действием.')
    character = next((a for a in roster['characters'] if a['id'] == actor['character_id']), None)
    if not character:
        raise ValueError('Персонаж отсутствует в реестре.')
    stats = CharacterRules(roster['character_rules']).statistics(character['sheet'], character['attributes'])
    if 'athletics' not in stats:
        raise ValueError('В профиле отсутствует Атлетика.')
    dc = integer(dc if dc is not None else state['rules']['tip_over_dc'], 'КС', 0, 100)
    return target, stats['athletics'] - check_penalty, dc

def perform_action(state: dict, actor: dict, effect: str, target_id: str, dc: int | None, roster: dict, roll_die: Callable[[int], int], position: list[int] | None = None, check_penalty: int = 0) -> dict:
    if effect == 'move_cell':
        if 'map' not in state:
            raise ValueError('Сначала загрузите карту.')
        character = next(a for a in roster['characters'] if a['id'] == actor['character_id'])
        distance = move_on_map(state, actor, position, character['sheet']['values'].get('speed', 0))
        return {'text': f"{actor['name']} перемещается в клетку {position}: {distance} футов.", 'distance_feet': distance}
    if effect == 'move':
        if 'map' in state:
            raise ValueError('На карте выберите конкретную клетку.')
        zone = next((z for z in state['zones'] if z['id'] == actor['zone_id']), None)
        if target_id not in zone['neighbors']:
            raise ValueError('Зона должна быть соседней.')
        actor.update(zone_id=target_id, cover=None)
        target = next(z for z in state['zones'] if z['id'] == target_id)
        return {'text': actor['name'] + ' перемещается: ' + target['name']}
    target = next((e for e in state['entities'] if e['id'] == target_id and e['kind'] == 'object'), None)
    if not target or target['zone_id'] != actor['zone_id']:
        raise ValueError('Объект должен находиться в той же зоне.')
    if 'map' in state and sum(abs(a-b) for a,b in zip(actor['position'], target['position'])) > 1:
        raise ValueError('Подойдите вплотную к объекту.')
    if effect == 'take_cover':
        if not target['provides_cover']:
            raise ValueError('Этот объект пока не предоставляет укрытие.')
        actor['cover'] = {'object_id': target_id, 'bonus': state['rules']['cover_bonus']}
        return {'text': actor['name'] + ' занимает укрытие: ' + target['name']}
    if effect != 'tip_over' or target['posture'] != 'upright':
        raise ValueError('Объект нельзя опрокинуть этим действием.')
    target, modifier, dc = prepare_tip_over(state, actor, target_id, dc, roster, check_penalty)
    actor['cover'] = None
    natural = roll_die(20)
    total = natural + modifier
    outcome = degree(natural, total, dc)
    if outcome >= 2:
        target.update(posture='overturned', provides_cover=True)
    return {'text': f"{actor['name']} опрокидывает {target['name']}: d20={natural}, итог {total} против КС {dc} — {'успех' if outcome >= 2 else 'неудача'}.",
            'effect': effect, 'natural': natural, 'modifier': modifier, 'total': total, 'dc': dc, 'degree': outcome}
