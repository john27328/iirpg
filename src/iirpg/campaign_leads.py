"""Sourced investigation leads, independent of narration, storage and HTTP.

Reading a clue records an observation, not a verdict or quest completion.
Following a lead means examining related evidence at its destination.
"""
from copy import deepcopy
from collections.abc import Callable

from .story_memory import words
from .world import accessible, location


def anchor(world: dict, place_id: str) -> str:
    seen = set()
    while place_id not in seen:
        seen.add(place_id)
        place = world['places'].get(place_id, {})
        if place.get('campaign_anchor') or not place.get('parent'):return place_id
        place_id = place['parent']
    raise ValueError('Цикл мест кампании.')


def relevant(world: dict, place_id: str, text: str = '', limit: int = 6) -> list[dict]:
    root = anchor(world, place_id); query = words(text)
    ranked = []
    for index, lead in enumerate(world.get('leads', {}).values()):
        score = (8 * int(lead['destination'] == root) + 4 * int(lead['found_at'] == place_id)
                 + len(query & words(lead['question'] + ' ' + lead['observation'])) * 2)
        ranked.append((int(lead['destination'] == root and lead['status'] != 'followed'), score, index, lead))
    return [deepcopy(row[3]) for row in sorted(ranked, key=lambda row:row[:3], reverse=True)[:limit]]


def prepare(world: dict, place_id: str, features: list[dict], discoveries: object, string: Callable[[object, int], str]) -> dict:
    """Link new local evidence only to real anchors and already known leads."""
    if not isinstance(discoveries, list) or not 1 <= len(discoveries) <= 2:
        raise ValueError('Сцене нужны одна или две исследуемые зацепки.')
    result = {}; sources = set(); root = anchor(world,place_id)
    known = world.get('leads', {})
    required = {key for key, lead in known.items() if lead['destination'] == root and lead['status'] != 'followed'}
    for index, entry in enumerate(discoveries):
        if not isinstance(entry, dict) or set(entry) != {'source','trigger','observation','destination','question','follow_up'}:
            raise ValueError('Неверные поля зацепки.')
        source = entry['source']
        if type(source) is not int or not 0 <= source < len(features) or source in sources:
            raise ValueError('Зацепке нужен отдельный существующий объект сцены.')
        sources.add(source)
        trigger = entry['trigger']
        if trigger not in ('look','open') or (trigger == 'open' and features[source]['kind'] != 'obstacle'):
            raise ValueError('Скрытая внутри зацепка требует запертого ящика или двери.')
        destination = entry['destination']
        if not isinstance(destination,str) or not world['places'].get(destination,{}).get('campaign_anchor') or destination == root:
            raise ValueError('Зацепка должна вести к другой существующей главной точке региона.')
        follow_up = entry['follow_up']
        if not isinstance(follow_up,str) or (follow_up and (follow_up not in known or known[follow_up]['destination'] != root)):
            raise ValueError('Продолжать можно только уже найденную зацепку, ведущую в эту область.')
        result[f'{place_id}:feature:{source}'] = {'id':f'{place_id}:lead:{index}', 'trigger':trigger,
            'origin':place_id,
            'observation':string(entry['observation'],300), 'destination':destination,
            'question':string(entry['question'],180), 'follow_up':follow_up}
    if required and not any(lead['follow_up'] in required for lead in result.values()):
        raise ValueError('В этой сцене нужно продолжить хотя бы одну найденную зацепку, ведущую сюда: '+', '.join(sorted(required)))
    return result


def reveal(world: dict, source_id: str, actor_id: str, rule: str) -> dict | None:
    source = world['entities'].get(source_id, {}); discovery = source.get('discovery')
    if not discovery:return None
    permitted = ((discovery['trigger'] == 'look' and rule == 'look') or
                 (discovery['trigger'] == 'open' and source.get('open') and rule in ('look','open','force_open','pick_lock','improvise')))
    if not permitted:return None
    if discovery['id'] in world.get('leads', {}):return deepcopy(world['leads'][discovery['id']])
    found_at = location(world,actor_id)
    lead = {k:deepcopy(discovery[k]) for k in ('id','observation','destination','question','follow_up')}
    lead.update(source_id=source_id, source_name=source['name'], source_origin=discovery['origin'], found_at=found_at,
                discovered_by=actor_id, discovered_at=world.get('time',0), status='open', continuations=[])
    world.setdefault('leads', {})[lead['id']] = lead
    source['discovery_revealed'] = True
    parent = world['leads'].get(lead['follow_up'])
    if parent and parent['destination'] == anchor(world,discovery['origin']):
        parent['status'] = 'followed'
        if lead['id'] not in parent['continuations']:parent['continuations'].append(lead['id'])
    return deepcopy(lead)


def visit(world: dict, place_id: str, actor_id: str) -> None:
    for lead in world.get('leads', {}).values():
        if lead['destination'] == anchor(world,place_id) and lead['status'] == 'open':
            lead.update(status='visited', visited_by=actor_id, visited_at=world.get('time',0))


def public_context(state: dict, text: str = '', actor_id: str | None = None) -> dict:
    world = state['world']; place = state['location']
    sources = [{'name':entity['name'],'trigger':entity['discovery']['trigger']}
        for key,entity in world['entities'].items() if entity.get('discovery') and not entity.get('discovery_revealed')
        and not entity.get('hidden') and location(world,key)==place and accessible(world,key,actor_id or state['character_id'])]
    progress = {'open':'Наблюдение найдено','visited':'Место по следу посещено','followed':'След продолжен новым наблюдением'}
    leads = [{'source':lead['source_name'], 'observation':lead['observation'], 'question':lead['question'],
        'found_by':world['entities'].get(lead['discovered_by'],{}).get('name','Участник группы'),
        'found_at':world['places'][lead['found_at']]['name'], 'source_place':world['places'][lead['source_origin']]['name'],
        'next_place':world['places'][lead['destination']]['name'], 'progress':progress[lead['status']]}
        for lead in relevant(world,place,text)]
    return {'leads':leads, 'unexamined':sources[:6]}
