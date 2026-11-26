"""Sourced world seeds; detailed scenes materialize on first visit."""
from copy import deepcopy
import hashlib
import unicodedata

from . import evening
from .story_memory import words
from .world import location, validate


def enabled(state: dict) -> bool:
    if not state.get('campaign') or evening.enabled(state) or state.get('pending_check'):
        return False
    return not state['world']['places'][state['location']].get('unexplored')


def capacity(state: dict) -> int:
    if not enabled(state):return 0
    here = state['location']
    place = state['world']['places'][here]
    if place.get('depth', 0) >= 3 or place.get('unexplored'):
        return 0
    count = sum(p.get('parent') == here for p in state['world']['places'].values())
    return min(2, max(0, 24 - count))


def schema(state: dict) -> dict:
    speakers = ['', *[key for key, npc in state['world']['entities'].items()
                     if npc['kind'] == 'npc' and location(state['world'], key) == state['location']
                     and npc.get('status', 'standing') in ('standing', 'surrendered')]]
    fields = {key: {'type': 'string', 'maxLength': limit, 'minLength': 1}
              for key, limit in (('name', 80), ('premise', 240), ('rumor', 300), ('question', 180))}
    fields['speaker_id'] = {'type': 'string', 'enum': speakers}
    fields['premise']['minLength'] = 0
    return {'type': 'array', 'maxItems': 2 if enabled(state) else 0, 'items': {
        'type': 'object', 'properties': fields, 'required': list(fields), 'additionalProperties': False}}


def relevant(state: dict, actor_id: str, text: str = '', limit: int = 6) -> list[dict]:
    query = words(text)
    ranked = []
    for index, hook in enumerate(state['world'].get('story_hooks', {}).values()):
        if actor_id not in hook['heard_by']:
            continue
        destination = state['world']['places'][hook['destination']]
        score = (8 * int(hook['destination'] == state['location'])
                 + 4 * int(destination.get('parent') == state['location'])
                 + 3 * len(query & words(destination['name'] + ' ' + hook['question'])))
        ranked.append((score, index, hook))
    return [{**deepcopy(row[2]),'destination_name':state['world']['places'][row[2]['destination']]['name']}
            for row in sorted(ranked, key=lambda row: row[:2], reverse=True)[:limit]]


def context(state: dict, actor_id: str, text: str = '') -> dict:
    return {'enabled':enabled(state), 'max_new_sites': capacity(state), 'parent': state['location'],
            'hooks': relevant(state, actor_id, text)}


def apply(state: dict, additions: object, actor_id: str, source_id: str) -> dict:
    """Validate a complete batch before modifying state; repeats never rewrite canon."""
    from .campaign import local_site, string
    if not isinstance(additions, list) or len(additions) > 2:
        raise ValueError('Допускаются не более двух новых мест за реплику.')
    if not additions:
        latest = state.get('history', [{}])[-1] if state.get('history') else {}
        if enabled(state) and latest.get('world_request') and not relevant(state,actor_id):
            raise ValueError('Игрок запросил развитие мира, а сохранённых зацепок пока нет. Добавьте хотя бы одно местное место или слух через world_additions; не ограничивайтесь отказом или обещанием поискать позже.')
        return {'places': {}, 'hooks': {}}
    if not enabled(state):
        raise ValueError('В этой сцене нельзя добавлять новые места.')
    trial = deepcopy(state)
    world = trial['world']
    result = {'places': {}, 'hooks': {}}
    seen = set()
    for row in additions:
        if not isinstance(row, dict) or set(row) != {'name', 'premise', 'rumor', 'question', 'speaker_id'}:
            raise ValueError('Неверные поля дополнения мира.')
        name = unicodedata.normalize('NFKC', string(row['name'], 80))
        rumor = string(row['rumor'], 300)
        question = string(row['question'], 180)
        speaker = row['speaker_id']
        npc = world['entities'].get(speaker, {}) if isinstance(speaker, str) else {}
        if not isinstance(speaker, str) or (speaker and (npc.get('kind') != 'npc'
                or location(world, speaker) != state['location']
                or npc.get('status', 'standing') not in ('standing', 'surrendered'))):
            raise ValueError('Слух может сообщить только присутствующий собеседник.')
        existing = next((key for key, place in world['places'].items()
                         if unicodedata.normalize('NFKC', place['name']).casefold() == name.casefold()), None)
        if existing is not None:
            if row['premise'] != '':
                if not world['places'][existing].get('introduced_by') or row['premise'] != world['places'][existing].get('canon',[None])[0]:
                    raise ValueError('Уже известное место нельзя переписать: для новой зацепки к нему оставьте premise пустым.')
            premise = world['places'][existing]['description']
        else:
            premise = string(row['premise'], 240)
        before = set(world['places'])
        destination = existing or local_site(trial, state['location'], name)
        if destination in seen:
            raise ValueError('Одно место нельзя добавлять дважды в одной реплике.')
        seen.add(destination)
        if destination not in before:
            world['places'][destination].update(description=premise, canon=[premise], introduced_by=source_id)
            result['places'][destination] = deepcopy(world['places'][destination])
        hook_id = 'hook_' + hashlib.sha256((destination + '\0' + speaker + '\0' + question.casefold()).encode()).hexdigest()[:20]
        known = world.get('story_hooks', {}).get(hook_id)
        if known:
            if known['rumor'] != rumor or known['question'] != question:
                raise ValueError('Уже сохранённую зацепку нельзя переписать новой репликой.')
            continue
        heard = [key for key in world.get('party', [actor_id]) if location(world, key) == state['location']]
        if actor_id not in heard:
            heard.append(actor_id)
        hook = {'id': hook_id, 'destination': destination, 'rumor': rumor, 'question': question,
                'speaker_id': speaker, 'speaker_name': npc.get('name', 'Ведущий'),
                'introduced_at': state['location'], 'introduced_by': source_id,
                'heard_by': heard, 'status': 'unverified', 'evidence': []}
        world.setdefault('story_hooks', {})[hook_id] = hook
        result['hooks'][hook_id] = deepcopy(hook)
    validate(world)
    state['world'] = world
    return result


def visit(world: dict, destination: str, actor_id: str) -> None:
    for hook in world.get('story_hooks', {}).values():
        if hook['destination'] == destination and hook['status'] == 'unverified':
            hook.update(status='visited', visited_by=actor_id)


def validate_additions(state: dict, additions: object, actor_id: str, source_id: str) -> None:
    """Report all row errors together so one repair can fix the complete batch."""
    if not isinstance(additions,list) or len(additions)>2:
        apply(deepcopy(state),additions,actor_id,source_id)
        return
    errors=[]
    for index,row in enumerate(additions):
        try:apply(deepcopy(state),[row],actor_id,source_id)
        except ValueError as error:errors.append(f'world_additions[{index}]: {error}')
    if errors:raise ValueError('; '.join(errors))
    apply(deepcopy(state),additions,actor_id,source_id)


def examine(world: dict, lead: dict, actor_id: str) -> None:
    for hook in world.get('story_hooks', {}).values():
        if hook['destination'] == lead['source_origin'] and lead['id'] not in hook['evidence']:
            hook.update(status='investigated', examined_by=actor_id)
            hook['evidence'].append(lead['id'])


def scene_context(state: dict, place_id: str) -> list[dict]:
    return [deepcopy(hook) for hook in state['world'].get('story_hooks', {}).values()
            if hook['destination'] == place_id][:4]
