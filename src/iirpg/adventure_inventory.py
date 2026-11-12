"""Scenario items use the same validated records as the character inventory."""
import json
from pathlib import Path
from uuid import UUID,uuid5
from .roster import InventoryRules

ITEMS=json.loads((Path(__file__).with_name('rules')/'adventure_items.json').read_text())

def acquire(c,room,state,item_id,roster):
    item=ITEMS[item_id]
    collected=state.setdefault('collected_items',{})
    if item_id in collected:raise ValueError('Этот предмет уже забран.')
    actor=state['character_id']
    if not actor:raise ValueError('Выберите персонажа для находки.')
    body=InventoryRules(roster['rules']).item({k:v for k,v in item.items() if k not in ('location','requires_check')})
    identity=str(uuid5(UUID(room),'adventure.item.'+item_id))
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
