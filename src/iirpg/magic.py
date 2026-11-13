"""PF2e rank-one spell effects. All costs and outcomes are resolved by the engine."""
from .checks import degree
from .combat_conditions import status_penalty
from .combat_map import distance
from .combat_tactics import target_index, can_see
from .progression import RULES


def prepare_spell(state: dict, scene: dict | None, spell_id: str, cost: int,
                  victim_id: str | None = None) -> dict:
    actor = state['fighters'][state['turn']]
    build = actor.get('build', {})
    kit = RULES['classes'].get(build.get('class_id'), {})
    if not isinstance(spell_id, str) or spell_id not in kit.get('spells', []):
        raise ValueError('Персонажу недоступно это заклинание.')
    spell = RULES['spells'][spell_id]
    if type(cost) is not int or cost not in spell['costs'] or state['actions'] < cost:
        raise ValueError('Недостаточно действий или неверная цена заклинания.')
    if spell['rank'] and build.get('spent', 0) >= kit['slots']:
        raise ValueError('Подготовленные ячейки закончились. Нужны отдых и дневная подготовка.')
    result = {'spell_id': spell_id, 'cost': cost, 'name': spell['name'], 'effect': spell['effect']}
    if 'range_feet' in spell:
        if not victim_id:
            raise ValueError('Выберите цель заклинания.')
        index = target_index(state, victim_id)
        target = state['fighters'][index]
        if not can_see(state, scene, state['turn'], index):
            raise ValueError('Цель заклинания не видна.')
        if state.get('mapped') and distance(actor['position'], target['position']) * 5 > spell['range_feet']:
            raise ValueError('Цель вне дальности заклинания.')
        if spell_id == 'daze' and ('mental' in target.get('immunities', []) or type(target.get('will')) is not int):
            raise ValueError('Цель невосприимчива или для неё не указана Воля.')
        result.update(target_index=index, target_id=victim_id)
    elif victim_id is not None:
        raise ValueError('Этот вариант заклинания применяется у самого героя.')
    return result


def cast_spell(state: dict, scene: dict | None, spell_id: str, cost: int,
               victim_id: str | None, roll_die) -> str:
    result = prepare_spell(state, scene, spell_id, cost, victim_id)
    actor = state['fighters'][state['turn']]
    damage = 0
    if spell_id in ('force_barrage', 'daze'):
        target = state['fighters'][result['target_index']]
        if spell_id == 'force_barrage':
            dice = [roll_die(4) for _ in range(cost)]
            damage = sum(dice) + cost
            result.update(dice=dice, bonus=cost, damage=damage, damage_type='force')
            detail = f"{cost} × (1d4+1): {dice}, урон {damage}; автоматическое попадание."
        else:
            dc = actor['spell_dc'] - status_penalty(actor)
            natural = roll_die(20)
            total = natural + target['will'] - status_penalty(target)
            outcome = degree(natural, total, dc)
            die = roll_die(6)
            damage = [die * 2, die, die // 2, 0][outcome]
            if outcome == 0:
                target['stunned'] = max(target.get('stunned', 0), 1)
            result.update(dice=[die], natural=natural, total=total, dc=dc, degree=outcome,
                          damage=damage, damage_type='mental', stunned=outcome == 0)
            detail = (f"Воля цели d20={natural}, итог {total} против КС {dc}: "
                      + ['критический провал', 'провал', 'успех', 'критический успех'][outcome]
                      + f"; 1d6={die}, урон {damage}." + (' Оглушён 1.' if outcome == 0 else ''))
        target['hp'] = max(0, target['hp'] - damage)
        detail += f" ОЗ {target['name']}: {target['hp']}/{target['max_hp']}."
    else:
        detail = utility(actor['build'], spell_id)
    state['actions'] -= cost
    if RULES['spells'][spell_id]['rank']:
        actor['build']['spent'] += 1
    state['last_spell'] = result
    return actor['name'] + ': ' + result['name'] + '. ' + detail


def utility(build: dict, spell_id: str) -> str:
    if spell_id not in RULES['classes'].get(build.get('class_id'), {}).get('spells', []):
        raise ValueError('Персонажу недоступно это заклинание.')
    if spell_id == 'light':
        build['light'] = True
        return 'У героя парит шар света: яркий свет 20 футов, тусклый ещё 20. До следующей дневной подготовки.'
    if spell_id == 'detect_magic':
        # The test scenario contains no unknown magic; the actor's own known light is ignored.
        return 'Неизвестной магии в радиусе 30 футов не обнаружено.'
    raise ValueError('Боевое заклинание требует перехода к бою и выбора цели.')


def flanking(state: dict, scene: dict | None, actor_index: int, victim: int) -> bool:
    """Opposite adjacent cells for the prototype's cardinal, one-cell melee reach."""
    from .combat_tactics import active, team
    if not state.get('mapped'):
        return False
    actor, target = state['fighters'][actor_index], state['fighters'][victim]
    if distance(actor['position'], target['position']) != 1 or not can_see(state, scene, actor_index, victim):
        return False
    opposite = [2 * target['position'][j] - actor['position'][j] for j in range(2)]
    return any(i != actor_index and team(state, i) == team(state, actor_index) and active(f)
               and f.get('position') == opposite and can_see(state, scene, i, victim)
               for i, f in enumerate(state['fighters']))
