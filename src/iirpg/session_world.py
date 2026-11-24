"""Persistence adapter for scenario entities and real roster inventories."""
from copy import deepcopy
import json
from uuid import UUID, uuid5
from .world import scenario
from .character_sheet import CharacterRules
from .character_resources import save_character
from .progression import describe
from .roster import InventoryRules
from .party import members, SETTINGS


def initialize(state: dict, actor: dict | None = None) -> None:
    config = scenario(state.get('scenario_id', 'missing-book'))
    if 'world' not in state:
        world = deepcopy(config['world'])
        world['time'] = int(state.get('elapsed_hours', 0) * 3600)
        legacy = config.get('legacy', {})
        for key, spec in legacy.get('item_owner', {}).items():
            if state.get(spec.get('returned_flag')):
                world['entities'][key]['owner'] = spec['recipient']
            elif state.get(spec.get('recovered_flag')) or key in state.get('collected_items', {}):
                world['entities'][key]['owner'] = state.get('character_id') or '$hero'
        for key, identity in state.get('collected_items', {}).items():
            if key in world['entities']:
                entity = world['entities'][key]
                entity['roster_item_id'] = identity
                if entity['kind'] == 'currency':
                    entity.update(owner=state.get('character_id'), amount=0)
                elif key not in legacy.get('item_owner', {}):
                    entity['owner'] = state.get('character_id')
        for key, outcome in state.get('npc_outcomes', {}).items():
            if key in world['entities']:
                world['entities'][key]['status'] = outcome
        for key, place in state.get('npc_locations', {}).items():
            if key in world['entities']:
                world['entities'][key]['place'] = place
        for key, agreed in state.get('npc_help', {}).items():
            if agreed and key in world['entities']:
                world['entities'][key]['agreements'] = {state.get('character_id'): {'service': 'help', 'expires': world['time'] + 86400}}
        if any(state.get(s.get('recovered_flag')) for s in legacy.get('item_owner', {}).values()):
            for key in legacy.get('open_with_recovered', []):
                world['entities'][key].update(open=True, locked=False)
        for quest in world.get('quests', {}).values():
            if world['entities'][quest['item']].get('owner') == quest['owner']:
                quest['completed'] = True
        state['world'] = world
    state['scenario_id'] = config['id']
    # Add authored presentation data to old saves without changing their facts,
    # agreements, attitudes, ownership or existing custom NPC characterizations.
    for key, template in config['world']['entities'].items():
        if key in state['world']['entities'] and 'personality' in template:
            state['world']['entities'][key].setdefault('personality', deepcopy(template['personality']))
    state.setdefault('location', config['start'])
    if state.get('party_ids'):
        state['world']['party'] = list(state['party_ids'])
        state['world']['party_mode'] = SETTINGS['travel_mode']
    if actor:
        if not state.get('party_ids') or not state.get('character_id'):
            state['character_id'] = actor['id']
        state['world']['entities'].setdefault(actor['id'], {'kind': 'pc', 'name': actor['name'], 'place': state['location']})
        for entity in state['world']['entities'].values():
            if entity.get('owner') == '$hero':
                entity['owner'] = actor['id']


def actor_view(actor: dict, rules: dict) -> dict:
    build = describe(actor)
    values = actor['sheet']['values']
    ranks = {key[:-5]: ['untrained', 'trained', 'expert', 'master', 'legendary'].index(value)
             for key, value in values.items() if key.endswith('_rank')}
    return {**deepcopy(actor), 'stats': CharacterRules(rules).statistics(actor['sheet'], actor['attributes']),
            'ranks': ranks, 'abilities': build['spells'] if build else {},
            'aid_skills': [key for key, spec in rules['profiles'][actor['sheet']['profile']]['statistics'].items()
                           if spec.get('group') == 'skills' or key == 'perception']}


def sync_inventory(c, room: str, state: dict, roster: dict) -> None:
    """Reconcile known world items against actual inventory; never respawn deleted loot."""
    identities = members(state)
    actors = [a for a in roster['characters'] if a['id'] in identities]
    if not actors:
        initialize(state)
        return
    migrated = 'world' not in state
    for actor in actors:
        initialize(state, actor)
        for key, entity in state['world']['entities'].items():
            if entity['kind'] != 'item' or entity.get('owner') != actor['id']:
                continue
            identity = entity.setdefault('roster_item_id', str(uuid5(UUID(room), 'adventure.item.' + key)))
            actual = next((i for i in actor.get('items', []) if i['id'] == identity), None)
            if actual:
                entity['item'] = {k: deepcopy(v) for k, v in actual.items() if k != 'id'}
            if not c.execute('SELECT 1 FROM inventory_items WHERE room_id=? AND id=? AND character_id=?', (room, identity, actor['id'])).fetchone():
                if migrated and not state.get('inventory_version'):
                    body = InventoryRules(roster['rules']).item(entity['item'])
                    entity['item'] = deepcopy(body)
                    c.execute('INSERT INTO inventory_items VALUES(?,?,?,?)', (identity, room, actor['id'], json.dumps(body, ensure_ascii=False)))
                    c.execute('UPDATE room_rosters SET revision=revision+1 WHERE room_id=?', (room,))
                else:
                    entity['owner'] = None
        linked = {e.get('roster_item_id') for e in state['world']['entities'].values()}
        for item in actor.get('items', []):
            if item['id'] not in linked:
                state['world']['entities']['inventory:' + item['id']] = {
                    'kind': 'item', 'name': item['name'], 'owner': actor['id'],
                    'roster_item_id': item['id'], 'item': {k: deepcopy(v) for k, v in item.items() if k != 'id'}}

    state['inventory_version'] = 2


def persist_effects(c, room: str, state: dict, actor: dict, effects: list, roster_rules: dict) -> None:
    for effect in effects:
        if effect['type'] == 'coins':
            balance = actor['coins'] + effect['delta']
            if not 0 <= balance <= 10000000:
                raise ValueError('Недопустимый остаток монет после сделки.')
            actor['coins'] = balance
            save_character(c, room, actor)
            continue
        if effect['type'] == 'item_state':
            row = c.execute('SELECT body FROM inventory_items WHERE room_id=? AND id=? AND character_id=?', (room, effect['item'], actor['id'])).fetchone()
            if not row: raise ValueError('Предмета больше нет в инвентаре.')
            body = json.loads(row[0]); body.update(state=effect['state'], hands=effect['hands'])
            c.execute('UPDATE inventory_items SET body=? WHERE room_id=? AND id=?', (json.dumps(body, ensure_ascii=False), room, effect['item']))
            c.execute('UPDATE room_rosters SET revision=revision+1 WHERE room_id=?', (room,))
            continue
        if effect['type'] == 'break_tool':
            row = c.execute('SELECT body FROM inventory_items WHERE room_id=? AND id=? AND character_id=?', (room, effect['item'], actor['id'])).fetchone()
            if row:
                body = json.loads(row[0]); body['broken'] = True
                for entity in state['world']['entities'].values():
                    if entity.get('roster_item_id') == effect['item']: entity['item'] = deepcopy(body)
                c.execute('UPDATE inventory_items SET body=? WHERE room_id=? AND id=?', (json.dumps(body, ensure_ascii=False), room, effect['item']))
                c.execute('UPDATE room_rosters SET revision=revision+1 WHERE room_id=?', (room,))
            continue
        entity = state['world']['entities'][effect['entity']]
        if entity['kind'] == 'currency':
            amount = entity.get('amount', 0)
            if effect['to'] == actor['id']:
                actor['coins'] += amount
            elif effect['from'] == actor['id']:
                if actor['coins'] < amount: raise ValueError('У героя больше нет этих монет.')
                actor['coins'] -= amount
            save_character(c, room, actor)
            if effect['to'] != actor['id']:
                other = c.execute('SELECT body FROM characters WHERE room_id=? AND id=?',(room,effect['to'])).fetchone()
                if other:
                    recipient = {'id':effect['to'], **json.loads(other[0])}
                    recipient['coins'] += amount
                    save_character(c, room, recipient)
        else:
            identity = entity.setdefault('roster_item_id', str(uuid5(UUID(room), 'adventure.item.' + effect['entity'])))
            if effect['from'] == actor['id']:
                removed = c.execute('DELETE FROM inventory_items WHERE room_id=? AND id=? AND character_id=?', (room, identity, actor['id'])).rowcount
                if not removed: raise ValueError('Предмета больше нет в инвентаре.')
            recipient = c.execute('SELECT id FROM characters WHERE room_id=? AND id=?', (room, effect['to'])).fetchone()
            if recipient:
                body = InventoryRules(roster_rules).item(entity['item'])
                entity['item'] = deepcopy(body)
                c.execute('INSERT INTO inventory_items VALUES(?,?,?,?)', (identity, room, effect['to'], json.dumps(body, ensure_ascii=False)))
                state.setdefault('collected_items', {})[effect['entity']] = identity
            c.execute('UPDATE room_rosters SET revision=revision+1 WHERE room_id=?', (room,))


def compatibility_view(state: dict) -> None:
    """Read-only old API fields, derived from entity state, never used for adjudication."""
    config = scenario(state['scenario_id'])
    for key, spec in config.get('legacy', {}).get('item_owner', {}).items():
        owner = state['world']['entities'][key].get('owner')
        state[spec['recovered_flag']] = owner in (*members(state), spec['recipient'])
        goals = [q for q in state['world'].get('quests', {}).values() if q['item'] == key and q['owner'] == spec['recipient']]
        state[spec['returned_flag']] = any(q.get('completed') for q in goals) if goals else owner == spec['recipient']
