"""Finite stock, personal quotes and atomic trade facts; prototype house rules.

All amounts are integer copper pieces. The model never supplies a price.
"""
from copy import deepcopy

from .action_rules import attitude
from .world import location


def offer(world: dict, actor: dict, item_id: str, merchant_id: str, buying: bool = True) -> dict:
    item = world['entities'].get(item_id, {})
    merchant = world['entities'].get(merchant_id, {})
    policy = merchant.get('trade', {})
    if merchant.get('kind') != 'npc' or not policy or merchant.get('status', 'standing') != 'standing':
        raise ValueError('Этот персонаж сейчас не торгует.')
    if location(world, merchant_id) != location(world, actor['id']):
        raise ValueError('Торговец должен находиться рядом.')
    if attitude(merchant, actor['id']) == 'hostile':
        raise ValueError('Враждебный торговец отказывается от сделки с этим героем.')
    value = item.get('value_cp')
    if item.get('kind') != 'item' or type(value) is not int or value <= 0:
        raise ValueError('Для этой вещи нет подтверждённой цены.')
    if buying:
        if item.get('owner') != merchant_id:
            raise ValueError('Этой вещи больше нет у торговца.')
        price = value
        quote = policy.get('quotes', {}).get(actor['id'], {}).get(item_id, {})
        if quote.get('expires', 0) > world.get('time', 0):
            price = min(price, quote['price_cp'])
    else:
        if item.get('owner') != actor['id']:
            raise ValueError('Продать можно только свою вещь.')
        if item['item'].get('category') not in policy.get('buys_categories', []):
            raise ValueError('Торговец не покупает такие вещи.')
        if item['item'].get('broken'):
            raise ValueError('Скупка сломанных вещей пока не поддержана.')
        price = value // 2
        if price <= 0:
            raise ValueError('Торговец не предлагает денег за эту вещь.')
    return {'merchant_id': merchant_id, 'item_id': item_id, 'price_cp': price,
            'base_price_cp': value, 'buying': buying}


def prepare_trade(world: dict, actor: dict, step: dict, inventory_rules: dict | None) -> dict:
    rule, key = step['rule'], step['target']
    item = world['entities'][key]
    merchant_id = item.get('owner') if rule != 'sell' else step.get('recipient')
    terms = offer(world, actor, key, merchant_id, buying=rule != 'sell')
    merchant = world['entities'][merchant_id]
    result = {'rule': rule, 'step': deepcopy(step), 'actor': actor['id'], 'target': key,
              'seconds': 60 if rule == 'bargain' else 2, 'trade': terms}
    if rule == 'bargain':
        previous = merchant['trade'].get('quotes', {}).get(actor['id'], {}).get(key, {})
        if previous.get('retry_after', 0) > world.get('time', 0):
            raise ValueError('Сегодня вы уже обсуждали цену этой вещи; новое предложение пока не появилось.')
        result.update(skill='diplomacy', modifier=actor['stats']['diplomacy'], dc=10 + merchant.get('will', 0))
        return result
    if rule == 'buy':
        if actor.get('coins', 0) < terms['price_cp']:
            raise ValueError(f"Не хватает монет: цена {terms['price_cp']} мм, у героя {actor.get('coins', 0)} мм.")
        if inventory_rules:
            from .roster import InventoryRules
            rules = InventoryRules(inventory_rules)
            trial = deepcopy(actor)
            trial['coins'] -= terms['price_cp']
            trial['items'] = trial.get('items', []) + [rules.item({**item['item'], 'state': 'stowed', 'hands': 0})]
            if rules.summary(trial)['over_capacity']:
                raise ValueError('Покупку некуда нести: превышен максимальный Bulk.')
    elif merchant['trade'].get('cash_cp', 0) < terms['price_cp']:
        raise ValueError('У торговца не хватает наличных для этой покупки.')
    return result


def apply_trade(world: dict, action: dict, outcome: int | None) -> dict:
    terms, actor = action['trade'], action['actor']
    merchant = world['entities'][terms['merchant_id']]
    item = world['entities'][action['target']]
    result = {'rule': action['rule'], 'target': action['target'], 'merchant_id': terms['merchant_id'],
              'effects': [], 'text': ''}
    if action['rule'] == 'bargain':
        discount = (0, 0, 10, 20)[outcome]
        price = max(1, (terms['base_price_cp'] * (100 - discount) + 99) // 100)
        quote = {'price_cp': price, 'expires': world['time'] + 600, 'retry_after': world['time'] + 86400}
        merchant['trade'].setdefault('quotes', {}).setdefault(actor, {})[action['target']] = quote
        result['text'] = f"{merchant['name']}: цена для {world['entities'][actor]['name']} — {price} мм за «{item['name']}», действует 10 минут. Покупка ещё не совершена."
        result['quote'] = deepcopy(quote)
        return result
    price = terms['price_cp']
    old = item['owner']
    item['owner'] = actor if terms['buying'] else terms['merchant_id']
    item['item'].update(state='stowed', hands=0)
    merchant['trade']['cash_cp'] += price if terms['buying'] else -price
    result['effects'] = [{'type': 'coins', 'delta': -price if terms['buying'] else price},
                         {'type': 'transfer', 'entity': action['target'], 'from': old, 'to': item['owner']}]
    result['text'] = (f"{world['entities'][actor]['name']} " + ('покупает' if terms['buying'] else 'продаёт')
                      + f" «{item['name']}» за {price} мм. Торговец: {merchant['name']}.")
    return result


def trade_view(world: dict, actor: dict) -> dict:
    """Actual personal prices, separated from NPC promises and model prose."""
    vendors = {}
    for key, merchant in world['entities'].items():
        if not merchant.get('trade') or location(world, key) != location(world, actor['id']):
            continue
        rows = []
        for item_id, item in world['entities'].items():
            if item.get('owner') not in (key, actor['id']) or 'value_cp' not in item:
                continue
            try:
                terms = offer(world, actor, item_id, key, buying=item['owner'] == key)
            except ValueError:
                continue
            rows.append({**terms, 'name': item['name']})
        vendors[key] = {'name': merchant['name'], 'cash_cp': merchant['trade'].get('cash_cp', 0),
                        'attitude_to_hero': attitude(merchant, actor['id']), 'offers': rows}
    return {'currency': 'copper', 'hero_coins_cp': actor.get('coins', 0), 'merchants': vendors}
