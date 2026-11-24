"""Select sourced dialogue episodes from the current save branch, without AI.

Recollections are statements and witnessed events, never patches to world truth.
Keeping them inside session history makes undo/load isolate memory naturally.
"""
from copy import deepcopy
import json
import re

MAX_STORY_ENTRIES = 256
HISTORY_FIELDS = ('player', 'gm', 'check', 'action', 'rule', 'target', 'character_id',
                  'actor_name', 'player_name', 'place_id', 'listeners', 'chat_index', 'voice')


def words(text: str) -> set[str]:
    """Small lexical retrieval aid; it never interprets or executes player intent."""
    return {word[:5] for word in re.findall(r'[^\W\d_]+', text.casefold()) if len(word) > 3}


def dialogue(history: list[dict], text: str, actor_id: str,
             partner_id: str | None) -> tuple[list[dict], list[dict]]:
    recent = [{k: deepcopy(h[k]) for k in HISTORY_FIELDS if k in h} for h in history[-8:]]
    older = [(index, h) for index, h in enumerate(history[:-8]) if h.get('player') or h.get('gm')]
    query = words(text)
    ranked = []
    for index, entry in older:
        # A different player's backstory can be relevant to the shared scene;
        # keep its speaker and audience rather than attributing it to this hero.
        overlap = len(query & words(entry.get('player', '') + ' ' + entry.get('gm', '')))
        audience = entry.get('listeners')
        heard = partner_id is not None and audience is not None and partner_id in audience
        same_actor = entry.get('character_id') in (None, actor_id)
        score = overlap * 4 + int(heard) * 2 + int(same_actor)
        ranked.append((score, index, entry))
    # Retain the start of the relationship as well as query matches. This also
    # recovers facts when the player asks "remember?" without repeating keywords.
    anchors = [(i, h) for i, h in older if h.get('player') and h.get('action') in ('question', 'clarify', None)
               and h.get('character_id') in (None, actor_id)
               and (partner_id is None or h.get('listeners') is None or partner_id in h['listeners'])][:2]
    selected = {i: h for i, h in anchors}
    for _, index, entry in sorted(ranked, key=lambda row: (row[0], row[1]), reverse=True):
        if len(selected) >= 6:
            break
        selected[index] = entry
    recalled = []
    for index, entry in sorted(selected.items()):
        recalled.append({'source': entry.get('player_request_id') or entry.get('offer_id') or f'history:{index}',
                         'audience_known': 'listeners' in entry,
                         **{k: deepcopy(entry[k]) for k in HISTORY_FIELDS if k in entry}})
    return recent, recalled


def fit_context(data: dict, budget: int) -> dict:
    """Keep current truth and the latest exchange intact; drop duplicate prose first."""
    data = deepcopy(data)
    def size() -> int:
        return len(json.dumps(data, ensure_ascii=False).encode())
    while size() > budget and data.get('recent_chat'):
        data['recent_chat'].pop(0)
    while size() > budget and len(data.get('history', [])) > 4:
        data['history'].pop(0)
    while size() > budget and data.get('recollections'):
        data['recollections'].pop()
    while size() > budget and len(data.get('history', [])) > 1:
        data['history'].pop(0)
    if size() > budget:
        raise ValueError('Слишком большой контекст сцены для настроенного лимита ИИ.')
    # The actual request is last, after the catalog and all quoted older speech.
    if 'player' in data:
        data['player'] = data.pop('player')
    return data
