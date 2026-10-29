"""PF2e Remaster Demoralize, without storage, model calls or HTTP."""
from .checks import degree
from .combat_conditions import status_penalty
from .combat_map import distance
from .combat_tactics import can_hear, can_see, fighter_id, target_index


def prepare_demoralize(state: dict, scene: dict | None, victim_id: str | None, delivery: str = 'speech') -> dict:
    rules = state['skill_rules']
    actor_index = state['turn'];actor = state['fighters'][actor_index]
    if state['actions'] < rules['cost']:
        raise ValueError('Для Деморализации нужно одно действие.')
    victim = target_index(state, victim_id);target = state['fighters'][victim]
    if delivery not in ('speech', 'nonlinguistic'):
        raise ValueError('Способ запугивания: speech или nonlinguistic.')
    if type(actor.get('intimidation')) is not int or type(target.get('will')) is not int:
        raise ValueError('Для проверки нужны Запугивание героя и Воля цели.')
    if state.get('mapped'):
        contact = state.get('awareness', {}).get(fighter_id(state, actor_index), {}).get('contacts', {}).get(fighter_id(state, victim))
        if not can_see(state, scene, actor_index, victim) and (not contact or contact.get('last_position_checked_empty')):
            raise ValueError('Участник не осведомлён об этой цели.')
        if distance(actor['position'], target['position']) * 5 > rules['range_feet']:
            raise ValueError('Цель дальше 30 футов.')
    if target.get('deafened') or not can_hear(state, scene, victim, actor_index, require_language=False):
        raise ValueError('Цель не слышит запугивание.')
    if set(target.get('immunities', [])) & {'mental', 'emotion', 'fear'}:
        raise ValueError('Цель невосприимчива к этому эффекту страха.')
    actor_id = fighter_id(state, actor_index)
    if target.get('demoralize_immunity', {}).get(actor_id, 0) > state['round']:
        raise ValueError('Цель временно невосприимчива к вашей повторной Деморализации.')
    shared = set(actor.get('languages', ['common'])) & set(target.get('languages', ['common']))
    language_penalty = rules['language_penalty'] if delivery == 'nonlinguistic' or not shared else 0
    modifier = actor['intimidation'] - status_penalty(actor) + language_penalty
    dc = rules['dc_base'] + target['will'] - status_penalty(target)
    return {'label': 'Запугивание', 'modifier': modifier, 'dc': dc, 'cost': rules['cost'],
            'target_id': fighter_id(state, victim), 'target_index': victim, 'target_name': target['name'],
            'delivery': delivery, 'language_penalty': language_penalty, 'status_penalty': status_penalty(actor),
            'source_url': rules['source_url'], 'success_frightened': rules['success_frightened'],
            'critical_frightened': rules['critical_frightened'], 'outcomes': rules.get('outcomes', {}),
            'targeting': {**rules.get('targeting', {'mode':'single','kind':'creature','max_targets':1}), 'target_ids':[fighter_id(state,victim)]}}


def demoralize(state: dict, scene: dict | None, victim_id: str | None, delivery: str, roll_die) -> str:
    check = prepare_demoralize(state, scene, victim_id, delivery)
    actor = state['fighters'][state['turn']];target = state['fighters'][check['target_index']]
    natural = roll_die(20);total = natural + check['modifier'];outcome = degree(natural, total, check['dc'])
    amount = state['skill_rules']['critical_frightened'] if outcome == 3 else state['skill_rules']['success_frightened'] if outcome == 2 else 0
    target['frightened'] = max(target.get('frightened', 0), amount)
    target.setdefault('demoralize_immunity', {})[fighter_id(state, state['turn'])] = state['round'] + state['skill_rules']['immunity_rounds']
    state['actions'] -= check['cost']
    state['last_check'] = {**check, 'natural': natural, 'total': total, 'degree': outcome, 'frightened_applied': amount}
    result = ['критический провал', 'провал', 'успех', 'критический успех'][outcome]
    return (f"{actor['name']} деморализует {target['name']}: Запугивание d20={natural} {check['modifier']:+d}, "
            f"итог {total} против КС Воли {check['dc']} — {result}. "
            + (f"Цель: Испуган {target['frightened']}. " if amount else 'Испуг не наложен. ')
            + 'Повторная попытка этого участника недоступна 10 минут игрового времени.')
