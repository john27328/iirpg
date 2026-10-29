"""Validated, code-free grid map documents suitable for generated content."""
from copy import deepcopy
from collections import deque
import json
from pathlib import Path
import re
from .roster import integer, name

MAP_ROOT = Path(__file__).with_name('rules') / 'maps'
TILES = {'#', '.', '~'}


def validate_map(document: object) -> dict:
    if not isinstance(document, dict) or set(document) != {'schema_version', 'id', 'name', 'description', 'rows', 'zones', 'objects', 'spawn'}:
        raise ValueError('Карта: неверный набор полей.')
    if type(document['schema_version']) is not int or document['schema_version'] != 1:
        raise ValueError('Неподдерживаемая версия карты.')
    def identifier(value):
        if not isinstance(value, str) or not re.fullmatch(r'[a-z][a-z0-9_-]{0,39}', value):
            raise ValueError('ID карты, зоны или объекта должен быть коротким латинским ключом.')
        return value
    identifier(document['id']); name(document['name']); name(document['description'], 'Описание', 2000)
    rows = document['rows']
    if not isinstance(rows, list) or not 3 <= len(rows) <= 30 or not all(isinstance(r, str) for r in rows):
        raise ValueError('Карта должна содержать от 3 до 30 строк.')
    width = len(rows[0])
    if not 3 <= width <= 40 or any(len(r) != width or set(r) - TILES for r in rows):
        raise ValueError('Строки должны иметь одинаковую ширину 3–40; допустимы # . ~.')
    floor = {(x,y) for y,row in enumerate(rows) for x,tile in enumerate(row) if tile != '#'}
    def point(value):
        if not isinstance(value, list) or len(value) != 2 or any(type(n) is not int for n in value) or tuple(value) not in floor:
            raise ValueError('Координаты должны указывать на проходимую клетку [x,y].')
        return tuple(value)
    spawn = point(document['spawn'])
    zones = document['zones']
    if not isinstance(zones, list) or not 1 <= len(zones) <= 30:
        raise ValueError('Нужно от 1 до 30 зон.')
    occupied, ids = set(), set()
    for zone in zones:
        if not isinstance(zone, dict) or set(zone) != {'id','name','cells'}:
            raise ValueError('Неверные поля зоны.')
        key=identifier(zone['id']);name(zone['name'])
        if key in ids or not isinstance(zone['cells'],list) or not zone['cells'] or len(zone['cells']) > 1200:
            raise ValueError('Повтор зоны или неверные клетки.')
        ids.add(key)
        cells=[point(p) for p in zone['cells']]
        if len(set(cells)) != len(cells) or occupied.intersection(cells):
            raise ValueError('Клетки зон не должны повторяться или пересекаться.')
        occupied.update(cells)
    if occupied != floor:
        raise ValueError('Каждая проходимая клетка должна принадлежать одной зоне.')
    objects=document['objects']
    if not isinstance(objects,list) or len(objects)>100:
        raise ValueError('Слишком много объектов.')
    positions=set(); object_ids=set()
    for obj in objects:
        if not isinstance(obj,dict) or set(obj) != {'id','name','position','posture','provides_cover','blocks_movement'}:
            raise ValueError('Неверные поля объекта.')
        key=identifier(obj['id']);name(obj['name']);pos=point(obj['position'])
        if key in object_ids or pos in positions or pos == spawn:
            raise ValueError('Повтор объекта или занятая точка появления.')
        object_ids.add(key);positions.add(pos)
        if obj['posture'] not in ('upright','fixed','overturned') or any(type(obj[k]) is not bool for k in ('provides_cover','blocks_movement')):
            raise ValueError('Неверное состояние объекта.')
    blocked={tuple(o['position']) for o in objects if o['blocks_movement']}
    reached={spawn};queue=deque([spawn])
    while queue:
        x,y=queue.popleft()
        for p in ((x-1,y),(x+1,y),(x,y-1),(x,y+1)):
            if p in floor-blocked-reached:reached.add(p);queue.append(p)
    if reached != floor-blocked:
        raise ValueError('Все свободные клетки должны быть достижимы от точки появления.')
    return deepcopy(document)


def templates() -> list[dict]:
    return [validate_map(json.loads(path.read_text())) for path in sorted(MAP_ROOT.glob('*.json'))]


def zone_at(document: dict, position: list[int]) -> str:
    return next(z['id'] for z in document['zones'] if position in z['cells'])


def install_map(state: dict, document: dict) -> None:
    document=validate_map(document)
    actors=[e for e in state['entities'] if e['kind']=='actor']
    if {a['id'] for a in actors}.intersection(o['id'] for o in document['objects']):
        raise ValueError('ID объекта конфликтует с персонажем.')
    state['map']=document;state['name']=document['name']
    zones=[{'id':z['id'],'name':z['name'],'neighbors':[]} for z in document['zones']]
    cells={tuple(p):z['id'] for z in document['zones'] for p in z['cells']}
    for (x,y),key in cells.items():
        for neighbor in ((x+1,y),(x-1,y),(x,y+1),(x,y-1)):
            other=cells.get(neighbor)
            zone=next(z for z in zones if z['id']==key)
            if other and other!=key and other not in zone['neighbors']:zone['neighbors'].append(other)
    state['zones']=zones
    state['entities']=[{**o,'kind':'object','zone_id':zone_at(document,o['position'])} for o in document['objects']]
    for actor in actors:
        actor.update(position=list(document['spawn']),zone_id=zone_at(document,document['spawn']),cover=None)
    state['entities'].extend(actors)
    for proposal in state['proposals']:
        if proposal['status']=='pending':proposal.update(status='cancelled',reason='Карта заменена')


def move_on_map(state: dict, actor: dict, destination: list[int], speed: int) -> int:
    document=state['map'];rows=document['rows']
    if not isinstance(destination,list) or len(destination)!=2 or any(type(n) is not int for n in destination):
        raise ValueError('Укажите клетку [x,y].')
    target=tuple(destination);start=tuple(actor['position'])
    if target==start:raise ValueError('Персонаж уже в этой клетке.')
    floor={(x,y) for y,row in enumerate(rows) for x,t in enumerate(row) if t!='#'}
    blocked={tuple(e['position']) for e in state['entities'] if e['kind']=='object' and e.get('blocks_movement')}
    if target not in floor-blocked:raise ValueError('Клетка занята препятствием.')
    # Dijkstra: difficult terrain costs twice as much. Orthogonal movement only.
    import heapq
    costs={start:0};queue=[(0,start)]
    while queue:
        cost,p=heapq.heappop(queue)
        if cost!=costs[p]:continue
        if p==target:break
        x,y=p
        for nxt in ((x-1,y),(x+1,y),(x,y-1),(x,y+1)):
            if nxt not in floor-blocked:continue
            candidate=cost+(10 if rows[nxt[1]][nxt[0]]=='~' else 5)
            if candidate<costs.get(nxt,100000):costs[nxt]=candidate;heapq.heappush(queue,(candidate,nxt))
    distance=costs.get(target)
    if distance is None or distance>speed:raise ValueError('Путь недоступен за одно перемещение с текущей Скоростью.')
    actor.update(position=list(destination),zone_id=zone_at(document,destination),cover=None)
    return distance
