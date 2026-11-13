"""Scenario items use the same validated records as the character inventory."""
import json
from pathlib import Path
from uuid import UUID,uuid5
from .roster import InventoryRules

ITEMS=json.loads((Path(__file__).with_name('rules')/'adventure_items.json').read_text())

def available_items(state):
    from .adventure_support import helper_location
    result={}
    for key,item in ITEMS.items():
        location=helper_location(state,item['loot_of']) if item.get('loot_of') else item['location']
        if location==state['location'] and key not in state.get('collected_items',{}) and (not item.get('loot_of') or state.get('npc_outcomes',{}).get(item['loot_of'])=='defeated'):
            result[key]={**item,'location':location}
    return result

def acquire(c,room,state,item_id,roster,*,recovered=False):
    item=ITEMS[item_id]
    collected=state.setdefault('collected_items',{})
    if item_id in collected:raise ValueError('Этот предмет уже забран.')
    if item_id not in available_items(state) and not (recovered and item_id=='book' and state.get('book')):raise ValueError('Предмет сейчас недоступен.')
    actor=state['character_id']
    if not actor:raise ValueError('Выберите персонажа для находки.')
    rules=InventoryRules(roster['rules'])
    body=rules.item({k:v for k,v in item.items() if k not in ('location','requires_check','loot_of','coin_amount','aliases')})
    identity=str(uuid5(UUID(room),'adventure.item.'+item_id))
    if item.get('coin_amount'):
        from .character_resources import save_character
        character=next(a for a in roster['characters'] if a['id']==actor)
        # The roster's generic coin counter records the number of coins, without conversion.
        from .roster import integer
        character['coins']=integer(character['coins']+item['coin_amount'],'Монеты',0,10000000)
        save_character(c,room,character)
    else:
        c.execute('INSERT INTO inventory_items VALUES(?,?,?,?)',(identity,room,actor,json.dumps(body,ensure_ascii=False)))
        c.execute('UPDATE room_rosters SET revision=revision+1 WHERE room_id=?',(room,))
    collected[item_id]=identity
    return body['name']

def carries(c,room,state,item_id):
    identity=state.get('collected_items',{}).get(item_id)
    return bool(identity and c.execute('SELECT 1 FROM inventory_items WHERE room_id=? AND character_id=? AND id=?',(room,state['character_id'],identity)).fetchone())

def give_book(c,room,state):
    if not carries(c,room,state,'book'):raise ValueError('Книги нет в инвентаре героя.')
    c.execute('DELETE FROM inventory_items WHERE room_id=? AND character_id=? AND id=?',(room,state['character_id'],state['collected_items']['book']))
    c.execute('UPDATE room_rosters SET revision=revision+1 WHERE room_id=?',(room,))
