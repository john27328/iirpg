"""Immutable compressed campaign artifacts and a branch-local journal chain.

Checkpoints retain references; loading a branch never reads another branch's
future. Old text is archived, not replaced with an AI-written summary.
"""
from copy import deepcopy
import hashlib
import json
from sqlite3 import Connection
import zlib

from .story_memory import HISTORY_FIELDS, words


def install(connection: Connection) -> None:
    connection.executescript('''CREATE TABLE IF NOT EXISTS campaign_blobs(
        hash TEXT PRIMARY KEY,kind TEXT NOT NULL,body BLOB NOT NULL);
        CREATE TABLE IF NOT EXISTS campaign_journal(
        head TEXT PRIMARY KEY,previous TEXT,tokens TEXT NOT NULL);''')


def put(connection: Connection, kind: str, value: dict) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()
    digest = hashlib.sha256(kind.encode() + b'\0' + raw).hexdigest()
    connection.execute('INSERT OR IGNORE INTO campaign_blobs VALUES(?,?,?)', (digest, kind, zlib.compress(raw, 6)))
    return digest


def get(connection: Connection, digest: str, kind: str) -> dict:
    row = connection.execute('SELECT body FROM campaign_blobs WHERE hash=? AND kind=?', (digest, kind)).fetchone()
    if not row:raise ValueError('Не найден сохранённый фрагмент кампании.')
    return json.loads(zlib.decompress(row[0]))


def archive(connection: Connection, state: dict) -> None:
    if not state.get('campaign') or len(state['history']) <= 96:return
    entries, state['history'] = state['history'][:-64], state['history'][-64:]
    meta = state['campaign']; previous = meta.get('journal_head')
    digest = put(connection, 'journal', {'previous':previous,'entries':entries})
    tokens = words(' '.join(h.get('player','') + ' ' + h.get('gm','') for h in entries))
    tokens.update(h['character_id'] for h in entries if h.get('character_id'))
    for h in entries:tokens.update(h.get('listeners', []))
    connection.execute('INSERT OR IGNORE INTO campaign_journal VALUES(?,?,?)', (digest, previous, json.dumps(sorted(tokens),ensure_ascii=False)))
    meta.update(journal_head=digest, archived_entries=meta.get('archived_entries',0)+len(entries))


def pack(connection: Connection, state: dict) -> dict:
    result = deepcopy(state)
    if not result.get('campaign'):return result
    removed = []
    for digest in result['campaign'].get('scene_refs', {}).values():
        base = get(connection, digest, 'scene')
        for group in ('places','entities'):
            for key, value in base[group].items():
                current = result['world'][group]
                if key not in state['world'][group]:removed.append([group,key])
                elif current.get(key) == value:current.pop(key)
    result['campaign']['removed_records'] = removed
    return result


def hydrate(connection: Connection, state: dict) -> dict:
    if not state.get('campaign'):return state
    overlays = state['world']
    base = {'places':{}, 'entities':{}}
    for digest in state['campaign'].get('scene_refs', {}).values():
        fragment = get(connection, digest, 'scene')
        for group in base:base[group].update(fragment[group])
    for group in base:state['world'][group] = {**base[group], **overlays[group]}
    for group, key in state['campaign'].pop('removed_records', []):state['world'][group].pop(key,None)
    return state


def freeze_result(connection: Connection, result: dict) -> dict:
    if not result.get('campaign'):return result
    return {'campaign_result_ref':put(connection,'result',pack(connection,result))}


def thaw_result(connection: Connection, result: dict) -> dict:
    if set(result) == {'campaign_result_ref'}:
        return hydrate(connection,get(connection,result['campaign_result_ref'],'result'))
    return result


def recall(connection: Connection, state: dict, text: str, actor: str, partner: str | None = None) -> list[dict]:
    head = state.get('campaign',{}).get('journal_head')
    if not head:return []
    query = words(text)
    rows = connection.execute('''WITH RECURSIVE chain(head,previous,tokens) AS (
        SELECT head,previous,tokens FROM campaign_journal WHERE head=?
        UNION ALL SELECT j.head,j.previous,j.tokens FROM campaign_journal j JOIN chain c ON j.head=c.previous LIMIT 4096)
        SELECT head,tokens FROM chain''',(head,)).fetchall()
    ranked = []
    for index, row in enumerate(rows):
        tokens = set(json.loads(row['tokens']))
        score = len(query & tokens)*4 + int(actor in tokens) + int(partner in tokens)*2
        ranked.append((score,-index,row['head']))
    entries = []
    for _, _, digest in sorted(ranked,reverse=True)[:4]:
        for index, h in enumerate(get(connection,digest,'journal')['entries']):
            if partner and partner not in h.get('listeners',[]):continue
            score = len(query & words(h.get('player','')+' '+h.get('gm','')))*4 + int(h.get('character_id')==actor)
            entries.append((score,index,{'source':h.get('player_request_id') or h.get('offer_id') or f'{digest}:{index}',
                'audience_known':'listeners' in h, **{k:deepcopy(h[k]) for k in HISTORY_FIELDS if k in h}}))
    return [row[2] for row in sorted(entries,key=lambda r:(r[0],r[1]),reverse=True)[:3]]
