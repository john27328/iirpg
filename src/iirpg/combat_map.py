"""Pure movement and reach rules for mapped training encounters."""
from copy import deepcopy
import heapq
from .maps import move_on_map


def distance(a: list[int], b: list[int]) -> int:
    return sum(abs(x-y) for x,y in zip(a,b))


def route(state: dict, start: list[int], goal: list[int], occupied: list[list[int]] = ()) -> list[list[int]]:
    rows=state['map']['rows']
    floor={(x,y) for y,row in enumerate(rows) for x,t in enumerate(row) if t!='#'}
    blocked={tuple(e['position']) for e in state['entities'] if e['kind']=='object' and e.get('blocks_movement')}
    blocked.update(tuple(p) for p in occupied if p!=start)
    target=tuple(goal);origin=tuple(start);costs={origin:0};previous={};queue=[(0,origin)]
    while queue:
        cost,p=heapq.heappop(queue)
        if cost!=costs[p]:continue
        if p==target:
            path=[p]
            while p!=origin:p=previous[p];path.append(p)
            return [list(p) for p in reversed(path)]
        x,y=p
        for nxt in ((x-1,y),(x+1,y),(x,y-1),(x,y+1)):
            if nxt not in floor-blocked:continue
            new=cost+(10 if rows[nxt[1]][nxt[0]]=='~' else 5)
            if new<costs.get(nxt,100000):costs[nxt]=new;previous[nxt]=p;heapq.heappush(queue,(new,nxt))
    return []


def move_fighter(state: dict, scene: dict, position: list[int]) -> str:
    if state['actions']<=0:raise ValueError('Действия закончились.')
    actor=state['fighters'][state['turn']]
    occupied=[f['position'] for i,f in enumerate(state['fighters']) if i!=state['turn'] and f['hp']>0 and not f.get('surrendered')]
    if position in occupied:raise ValueError('Клетка занята участником боя.')
    temporary=deepcopy(scene)
    temporary['entities'].extend({'kind':'object','position':p,'blocks_movement':True} for p in occupied)
    moved={'position':list(actor['position'])}
    feet=move_on_map(temporary,moved,position,actor['speed'])
    actor.update(position=list(position),cover=None)
    state['actions']-=1
    return f"{actor['name']} перемещается в {position}: {feet} футов; осталось действий {state['actions']}."


def approach(state: dict, scene: dict, target_index: int | None = None) -> list[int] | None:
    if target_index is None:
        own_team=state['fighters'][state['turn']].get('team','players' if state['turn']==0 else 'enemies')
        choices=[i for i,f in enumerate(state['fighters']) if f.get('team','players' if i==0 else 'enemies')!=own_team and f['hp']>0 and not f.get('surrendered')]
        if not choices:return None
        target_index=min(choices,key=lambda i:distance(state['fighters'][state['turn']]['position'],state['fighters'][i]['position']))
    actor=state['fighters'][state['turn']];target=state['fighters'][target_index]
    occupied=[f['position'] for i,f in enumerate(state['fighters']) if i!=state['turn'] and f['hp']>0 and not f.get('surrendered')]
    x,y=target['position'];candidates=[]
    for goal in ([x-1,y],[x+1,y],[x,y-1],[x,y+1]):
        path=route(scene,actor['position'],goal,occupied)
        if path:candidates.append(path)
    if not candidates:return None
    def cost(path):return sum(10 if scene['map']['rows'][y][x]=='~' else 5 for x,y in path[1:])
    path=min(candidates,key=cost);travelled=0;destination=None
    for p in path[1:]:
        travelled+=10 if scene['map']['rows'][p[1]][p[0]]=='~' else 5
        if travelled>actor['speed']:break
        destination=p
    return destination
