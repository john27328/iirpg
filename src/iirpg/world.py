"""Scenario data and persistent entities. No HTTP, database or natural-language commands."""
from copy import deepcopy
import json
from pathlib import Path

SCENARIOS = Path(__file__).with_name('rules') / 'scenarios'


def scenario(identifier: str = 'missing-book') -> dict:
    if not isinstance(identifier, str) or identifier not in {p.stem for p in SCENARIOS.glob('*.json')}:
        raise ValueError('Неизвестный сценарий.')
    value = json.loads((SCENARIOS / (identifier + '.json')).read_text())
    validate(value['world'])
    return value


def scenarios() -> list[dict]:
    return [{k: scenario(p.stem)[k] for k in ('id', 'name', 'description')}
            for p in sorted(SCENARIOS.glob('*.json'))]


def validate(world: dict) -> None:
    places, entities = world['places'], world['entities']
    if not places or set(places) & set(entities):
        raise ValueError('Места и сущности должны иметь уникальные ID.')
    for place in places.values():
        for edge in place.get('exits', []):
            if edge['to'] not in places or edge.get('door', '') not in ('', *entities):
                raise ValueError('Проход ссылается на неизвестное место или дверь.')
    for key, entity in entities.items():
        owner = entity.get('owner', entity.get('place'))
        if owner not in (*places, *entities, '$hero', None):
            raise ValueError('Неизвестный владелец сущности.')
        seen = {key}
        while owner in entities:
            if owner in seen:
                raise ValueError('Цикл владения предметами.')
            seen.add(owner)
            owner = entities[owner].get('owner', entities[owner].get('place'))


def location(world: dict, target: str) -> str | None:
    seen = set()
    while target in world['entities']:
        if target in seen:
            raise ValueError('Цикл владения.')
        seen.add(target)
        entity = world['entities'][target]
        target = entity.get('owner', entity.get('place'))
    return target if target in world['places'] else None


def route(world: dict, start: str, destination: str) -> list[str]:
    if destination not in world['places']:
        raise ValueError('Уточните, куда вы хотите попасть.')
    pending = [[start]]
    visited = {start}
    for path in pending:
        if path[-1] == destination:
            return path
        for edge in world['places'][path[-1]].get('exits', []):
            door = world['entities'].get(edge.get('door'), {})
            if door and not door.get('open', False):
                continue
            if edge['to'] not in visited:
                visited.add(edge['to'])
                pending.append(path + [edge['to']])
    raise ValueError('Открытого пути пока нет. Сначала откройте проход или выберите другой маршрут.')


def accessible(world: dict, key: str, actor: str) -> bool:
    entity = world['entities'][key]
    owner = entity.get('owner')
    while owner in world['entities']:
        if owner == actor:
            return True
        container = world['entities'][owner]
        if container['kind'] == 'pc':
            return False
        if container['kind'] == 'npc':
            return container.get('status') == 'defeated'
        if container['kind'] == 'container' and not container.get('open'):
            return False
        owner = container.get('owner')
    return True


def public_view(world: dict, actor: str, place: str) -> dict:
    """Observations are distinct from the GM's private facts and NPC knowledge."""
    entities = {}
    for key, value in world['entities'].items():
        if key == actor or location(world, key) != place or value.get('hidden'):
            continue
        if value.get('owner') in world['entities'] and not accessible(world, key, actor):
            continue
        entities[key] = {k: deepcopy(v) for k, v in value.items()
                         if k in ('name', 'kind', 'description', 'owner', 'open', 'locked', 'status')}
    return {'place_id': place, 'place': deepcopy(world['places'][place]), 'entities': entities}
