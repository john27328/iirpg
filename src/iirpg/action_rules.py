"""Bounded Pathfinder action adjudication; AI selects actions, never writes effects."""
from copy import deepcopy
import json
from pathlib import Path
from .checks import degree
from .world import accessible, location, route

ATTITUDES = ['hostile', 'unfriendly', 'indifferent', 'friendly', 'helpful']
ATTITUDE_LABELS = dict(zip(ATTITUDES, ['враждебное', 'недружелюбное', 'безразличное', 'дружелюбное', 'готовность помочь']))
RULES = json.loads((Path(__file__).with_name('rules') / 'actions_pf2e.json').read_text())['actions']
AID_DC = RULES['aid'].get('dc', 15)


def validate_proposal(value: object) -> dict:
    required = {'intent', 'reply', 'steps'}
    if not isinstance(value, dict) or not required <= set(value) or set(value) - required - {'voice', 'interlocutor', 'world_request'}:
        raise ValueError('Мастер вернул неверный формат плана.')
    if 'world_request' in value and type(value['world_request']) is not bool:
        raise ValueError('world_request должен быть true или false.')
    if 'interlocutor' in value and (not isinstance(value['interlocutor'], str) or len(value['interlocutor']) > 120):
        raise ValueError('Неверная ссылка на собеседника.')
    if 'voice' in value and value['voice'] not in ('gm', 'npc'):
        raise ValueError('Неизвестный голос ответа мастера.')
    if value['intent'] not in ('question', 'act', 'clarify', 'cancel'):
        raise ValueError('Неизвестный тип намерения.')
    if not isinstance(value['reply'], str) or len(value['reply']) > 1600:
        raise ValueError('Слишком длинная реплика мастера.')
    if not isinstance(value['steps'], list) or len(value['steps']) > 8:
        raise ValueError('План должен содержать не более восьми шагов.')
    if value['intent'] != 'act' and value['steps']:
        raise ValueError('Вопрос или уточнение не разрешает действий.')
    for step in value['steps']:
        if not isinstance(step, dict) or set(step) - {'rule', 'target', 'recipient', 'service', 'task', 'skill', 'ability', 'method'}:
            raise ValueError('Неизвестные поля шага.')
        if any(not isinstance(v, str) or len(v) > 120 for v in step.values()) or step.get('rule') not in RULES:
            raise ValueError('Неизвестное действие или ссылка на сущность.')
    return deepcopy(value)


def attitude(entity: dict, actor: str) -> str:
    return entity.get('impressions', {}).get(actor, entity.get('attitudes', {}).get(actor, entity.get('attitude', 'indifferent')))


def shift_attitude(entity: dict, actor: str, delta: int) -> None:
    old = ATTITUDES.index(entity.get('attitudes', {}).get(actor, entity.get('attitude', 'indifferent')))
    entity.setdefault('attitudes', {})[actor] = ATTITUDES[max(0, min(4, old + delta))]
    if actor in entity.get('impressions', {}):
        current = ATTITUDES.index(entity['impressions'][actor])
        entity['impressions'][actor] = ATTITUDES[max(0, min(4, current + delta))]
    entity['social_revision'] = entity.get('social_revision', 0) + 1


def tools(actor: dict, tag: str) -> list[dict]:
    return [i for i in actor.get('items', []) if tag in i.get('tags', []) and not i.get('broken')]


def agreements_for(npc: dict, actor: str, now: int) -> list[dict]:
    """Open promises coexist; the old single agreement remains readable."""
    entries = [npc.get('agreements', {}).get(actor, {})]
    entries.extend(c for c in npc.get('commitments', []) if c.get('actor') == actor)
    return [entry for entry in entries if entry.get('expires', 0) > now]


def prepare(world: dict, actor: dict, step: dict, inventory_rules: dict | None = None) -> dict:
    """Freeze prerequisites and DC before dice. No mutation and no randomness here."""
    step = deepcopy(step)
    for field in ('target', 'recipient', 'task'):
        ref = step.get(field)
        if ref and ref not in world['entities'] and ref not in world['places']:
            aliases = [key for key, entity in world['entities'].items() if entity.get('roster_item_id') == ref]
            if len(aliases) == 1: step[field] = aliases[0]
    rule = step['rule']
    key = step.get('target', '')
    place = location(world, actor['id'])
    target = world['entities'].get(key)
    prepared = {'rule': rule, 'step': deepcopy(step), 'actor': actor['id'], 'target': key,
                'seconds': RULES[rule].get('seconds', 2)}
    if rule == 'explore':
        raise ValueError('Новая сцена должна быть создана перед перемещением.')
    if rule == 'move':
        if step.get('method') not in (None, '', 'walk', 'run'):
            raise ValueError('Такой способ перемещения не поддержан. Можно идти пешком либо применить доступную способность.')
        destination = key if key in world['places'] else location(world, key)
        prepared['route'] = route(world, place, destination)
        prepared['destination'] = destination
        prepared['seconds'] = max(0, len(prepared['route']) - 1) * 6
        return prepared
    if rule == 'look':
        if key and key not in (place, *world['entities']):
            raise ValueError('Уточните объект осмотра.')
        if target and (location(world, key) != place or target.get('hidden') or not accessible(world, key, actor['id'])):
            raise ValueError('Отсюда объект не виден.')
        return prepared
    if rule == 'rest':
        if not world['places'][place].get('safe_rest'):
            raise ValueError('Здесь небезопасно отдыхать. Найдите защищённое место.')
        prepared['seconds'] = 86400
        return prepared
    if rule == 'cast':
        ability = step.get('ability', '')
        spells = actor.get('abilities', {})
        if ability not in spells:
            raise ValueError('Такой способности нет у персонажа. Доступные заклинания: ' + (', '.join(spells) or 'нет') + '.')
        from .progression import describe
        build = describe(actor)
        if spells[ability].get('rank', 0) and (not build or build['slots_remaining'] <= 0):
            raise ValueError('Для заклинания не осталось дневных ячеек.')
        if ability not in ('light', 'detect_magic'):
            return prepare(world, actor, {'rule': 'combat', 'target': key})
        return prepared
    if not target:
        raise ValueError('Нужна существующая цель действия. Уточните, с чем вы взаимодействуете.')
    if location(world, key) != place:
        raise ValueError('Цель должна находиться рядом. Сначала подойдите к ней.')
    if target.get('hidden') and rule != 'seek':
        raise ValueError('Эта цель пока не обнаружена.')
    if rule in ('buy', 'sell', 'bargain'):
        from .trade import prepare_trade
        return prepare_trade(world, actor, step, inventory_rules)
    if rule == 'report_adventure':
        from .evening import check_report
        check_report(world,actor['id'],key)
        return prepared
    if rule == 'talk':
        if target['kind'] != 'npc' or target.get('status', 'standing') not in ('standing', 'surrendered'):
            raise ValueError('Этот персонаж сейчас не может отвечать.')
        return prepared
    if rule == 'aid' and target['kind'] == 'pc':
        if key == actor['id']:
            raise ValueError('Нельзя помогать самому себе.')
        if not {key, actor['id']} <= set(world.get('party', [])):
            raise ValueError('Помощник и получатель должны входить в одну группу.')
        task = world['entities'].get(step.get('task'))
        if not task or location(world, step['task']) != place:
            raise ValueError('Для подготовки помощи нужно находиться у места задачи.')
        if step.get('skill') not in actor.get('aid_skills', []):
            raise ValueError('Укажите доступный помощнику навык.')
        prepared['player_aid'] = {'helper_id': actor['id'], 'recipient_id': key,
                                 'task': step['task'], 'skill': step['skill']}
        return prepared
    if rule in ('combat', 'make_impression', 'request', 'coerce', 'sense_motive', 'aid'):
        if target['kind'] != 'npc' or target.get('status', 'standing') != 'standing':
            raise ValueError('Этот персонаж сейчас не может участвовать в действии.')
    if rule in ('open', 'force_open', 'pick_lock'):
        if target['kind'] != 'container':
            raise ValueError('Нужна дверь или контейнер.')
        if target.get('open') and rule != 'open':
            raise ValueError('Объект уже открыт; новый бросок не нужен.')
    if rule == 'open' and target.get('locked'):
        raise ValueError('Заперто. Нужен ключ, вскрытие замка или силовой способ.')
    if rule in ('stow', 'hold'):
        if target['kind'] != 'item' or target.get('owner') != actor['id']:
            raise ValueError('Можно убрать или взять в руки только свою вещь.')
        item = next((i for i in actor.get('items', []) if i['id'] == target.get('roster_item_id')), None)
        if not item: raise ValueError('Предмета больше нет в инвентаре.')
        hands = item['required_hands'] if rule == 'hold' else 0
        if sum(i.get('hands', 0) for i in actor.get('items', []) if i['id'] != item['id']) + hands > 2:
            raise ValueError('Не хватает рук: сначала уберите другую вещь.')
        prepared.update(item_id=item['id'], hands=hands)
    if rule in ('take', 'give'):
        if target['kind'] not in ('item', 'currency') or not accessible(world, key, actor['id']):
            raise ValueError('Предмет недоступен: сначала откройте контейнер или получите согласие владельца.')
        if rule == 'take' and target.get('owner') == actor['id']:
            raise ValueError('Это уже у вас.')
        free = 2 - sum(i.get('hands', 0) for i in actor.get('items', []))
        if rule == 'take' and free < 1:
            raise ValueError('Нужна свободная рука. Уберите оружие или щит через инвентарь.')
        if rule == 'take' and target['kind'] == 'item' and inventory_rules:
            from .roster import InventoryRules
            rules = InventoryRules(inventory_rules)
            trial = deepcopy(actor)
            trial['items'] = trial.get('items', []) + [rules.item(target['item'])]
            if rules.summary(trial)['over_capacity']:
                raise ValueError('Превышен максимальный Bulk. Освободите место в инвентаре.')
        if rule == 'give':
            recipient = world['entities'].get(step.get('recipient'))
            if target['kind'] == 'currency' and actor.get('coins', 0) < target.get('amount', 0):
                raise ValueError('У героя больше нет этих монет.')
            if target.get('owner') != actor['id']:
                raise ValueError('Этот предмет вам не принадлежит.')
            if not recipient or recipient['kind'] not in ('npc', 'pc') or location(world, step['recipient']) != place or recipient.get('status') == 'defeated':
                raise ValueError('Получатель должен находиться рядом и быть в состоянии принять предмет.')
    if rule == 'aid':
        task = world['entities'].get(step.get('task'))
        def supports(agreement):
            if agreement.get('description'):
                return agreement.get('task') == step.get('task') and agreement.get('help_skill') == step.get('skill')
            return step.get('skill') in target.get('services', {}).get(agreement.get('service'), {}).get('skills', [])
        if not any(supports(agreement) for agreement in agreements_for(target, actor['id'], world.get('time', 0))):
            raise ValueError('Сначала получите согласие на такую помощь.')
        if not task or location(world, step['task']) != place:
            raise ValueError('Помощник должен находиться у места задачи.')
        return prepared
    if rule == 'combat':
        if not target.get('combat_group'):
            raise ValueError('Для этой цели пока не настроен бой.')
        return prepared
    skill = RULES[rule].get('skill')
    penalty = 0
    adjustments = []
    if rule == 'force_open':
        if 'force_dc' not in target:
            raise ValueError('Здесь не задан силовой способ доступа. Уточните другой способ.')
        dc = target['force_dc']
        if not any(i.get('state') == 'held' for i in tools(actor, 'crowbar')):
            adjustments.append({'label': 'Без лома в руках', 'value': -2})
        if target.get('jammed'):
            adjustments.append({'label': 'Механизм заклинило после критического провала', 'value': -2})
        penalty = sum(change['value'] for change in adjustments)
    elif rule == 'pick_lock':
        if not target.get('locked') or not target.get('lock'):
            raise ValueError('Замок уже открыт или отсутствует. Можно открыть объект без броска.')
        if actor.get('ranks', {}).get('thievery', 0) < 1:
            raise ValueError('Для вскрытия замка требуется trained Воровство.')
        kit = tools(actor, 'thieves_tools')
        if not kit:
            raise ValueError('Нужен исправный набор взломщика. Обычная дубинка не заменяет отмычки.')
        if kit[0].get('state') not in ('worn', 'held'):
            raise ValueError('Сначала наденьте или возьмите набор взломщика в инвентаре.')
        prepared['tool_id'] = kit[0]['id']
        dc = target['lock']['dc']
    elif rule in ('request', 'coerce'):
        offered = target.get('services', {})
        if not step.get('service') and step.get('method', '').strip():
            task = step.get('task', '')
            # A social check's own Diplomacy/Intimidation is not a promise of
            # Aid. Without a task the optional skill field has no help effect.
            help_skill = step.get('skill', '') if task else ''
            if not task:
                prepared['step']['skill'] = ''
            if task and task not in world['entities']:
                raise ValueError('Нужно указать существующий объект помощи.')
            if help_skill and (not task or help_skill not in actor.get('stats', {})):
                raise ValueError('Для помощи укажите объект задачи и существующий навык.')
            service = {'name': step['method'].strip(), 'task': task, 'help_skill': help_skill,
                       'follow': bool(task and help_skill)}
            prepared['commitment'] = service
        else:
            # Compatibility for existing plans and frozen checks. New GM plans
            # describe the request directly; authors need not enumerate services.
            if not step.get('service'):
                choices = [key for key, value in offered.items() if value.get('acceptable') and not value.get('harmful')]
                if len(choices) == 1:
                    step['service'] = choices[0]
                    prepared['step']['service'] = choices[0]
            service = offered.get(step.get('service'))
            if not service or not service.get('acceptable') or service.get('harmful'):
                raise ValueError('Опишите, о чём вы просите собеседника. Бросок не заставит выполнить любое требование.')
        def matches(agreement):
            if prepared.get('commitment'):
                return (agreement.get('description') == service['name'] and agreement.get('task', '') == service.get('task', '')
                        and agreement.get('help_skill', '') == service.get('help_skill', ''))
            return agreement.get('service') == step.get('service')
        if rule == 'request' and any(matches(a) for a in agreements_for(target, actor['id'], world.get('time', 0))):
            prepared['already_agreed'] = True
            return prepared
        if rule == 'request' and attitude(target, actor['id']) not in ('friendly', 'helpful'):
            raise ValueError('Для Request нужно friendly/helpful отношение. Сначала расположите собеседника или измените подход.')
        if rule == 'coerce' and target.get('coerce_immunity', {}).get(actor['id'], 0) > world.get('time', 0):
            raise ValueError('Повторное принуждение сейчас не действует после критического провала.')
        dc = service.get('dc', 10 + target.get('will', 0)) if rule == 'request' else 10 + target.get('will', 0)
    elif rule == 'make_impression':
        dc = 10 + target.get('will', 0)
    elif rule == 'sense_motive':
        if target.get('assessed', {}).get(actor['id']) == target.get('social_revision', 0):
            raise ValueError('Вы уже оценили поведение; повторите после существенного изменения ситуации.')
        dc = 10 + target.get('deception', 0)
    elif rule == 'seek':
        if not target.get('hidden'):
            raise ValueError('Это уже видно, бросок не требуется.')
        if 'detection_dc' not in target:
            raise ValueError('Для скрытой цели не настроена проверка обнаружения.')
        dc = target['detection_dc']
    elif rule == 'improvise':
        ruling = target.get('affordances', {}).get(step.get('method'))
        if not ruling and step.get('method', '').strip() and step.get('skill') == 'athletics' and target.get('kind') == 'container' and 'force_dc' in target:
            # A new fictional method can use an existing mechanical resolution;
            # no per-rope/per-lever/per-shelf scenario entry is required.
            return prepare(world, actor, {**step, 'rule': 'force_open'}, inventory_rules)
        if not ruling or ruling.get('effect') not in ('open', 'reveal'):
            raise ValueError('Для этого необычного способа пока нет проверяемого эффекта. Уточните цель и способ; мастер не будет выдумывать исполнение.')
        skill, dc = ruling['skill'], ruling['dc']
        prepared['ruling'] = deepcopy(ruling)
    else:
        return prepared
    if skill not in actor.get('stats', {}):
        raise ValueError('Этот навык недоступен персонажу.')
    prepared.update(skill=skill, dc=dc, modifier=actor['stats'][skill] + penalty,
                    modifier_breakdown=[{'label': 'Навык героя', 'value': actor['stats'][skill], 'character_id': actor['id']}, *adjustments])
    return prepared


def apply(world: dict, action: dict, outcome: int | None = None) -> dict:
    """Apply only registered effects. Returns facts for narration, not AI claims."""
    rule, key, actor = action['rule'], action['target'], action['actor']
    target = world['entities'].get(key, {})
    step = action['step']
    name = target.get('name', key)
    result = {'rule': rule, 'target': key, 'text': '', 'effects': []}
    if action.get('skill') and outcome is None:
        raise ValueError('Сначала нужен результат проверки.')
    world['time'] = world.get('time', 0) + action['seconds']
    if rule in ('buy', 'sell', 'bargain'):
        from .trade import apply_trade
        result = apply_trade(world, action, outcome)
    elif rule == 'move':
        origin = location(world, actor)
        travellers = world.get('party', [actor]) if world.get('party_mode') == 'together' else [actor]
        for traveller in travellers:
            world['entities'][traveller]['place'] = action['destination']
        for npc in world['entities'].values():
            for traveller in travellers:
                npc.get('impressions', {}).pop(traveller, None)
                follows = any(npc.get('services', {}).get(a.get('service'), {}).get('follow') or
                              (a.get('follow') and npc.get('place') == origin)
                              for a in agreements_for(npc, traveller, world['time']))
                if npc.get('status', 'standing') == 'standing' and follows:
                    npc['place'] = action['destination']
        result['text'] = ('Группа переходит: ' if len(travellers) > 1 else 'Вы переходите: ') + ' → '.join(world['places'][p]['name'] for p in action['route'][1:] or action['route']) + '.'
    elif rule == 'look':
        if target.get('kind') == 'container' and target.get('open'):
            result['text'] = name + ': открыто.'
            contents = [e['name'] for e in world['entities'].values() if e.get('owner') == key and not e.get('hidden')]
            if contents:result['text'] += ' Внутри: ' + ', '.join(contents) + '.'
        else:
            result['text'] = target.get('description', world['places'][location(world, actor)]['description'])
    elif rule == 'report_adventure':
        from .evening import finish
        result['text']=finish(world,actor)
    elif rule == 'talk':
        result['text'] = 'Вы обращаетесь к ' + name + '.'
    elif rule == 'open':
        target['open'] = True
        result['text'] = name + ': открыто.'
    elif rule in ('stow', 'hold'):
        state = 'held' if rule == 'hold' else 'stowed'
        target['item'].update(state=state, hands=action['hands'])
        result['effects'].append({'type': 'item_state', 'item': action['item_id'], 'state': state, 'hands': action['hands']})
        result['text'] = ('Вы берёте в руки: ' if rule == 'hold' else 'Вы убираете: ') + name + '.'
    elif rule in ('take', 'give'):
        old = target['owner']
        target['owner'] = actor if rule == 'take' else step['recipient']
        result['effects'].append({'type': 'transfer', 'entity': key, 'from': old, 'to': target['owner']})
        result['text'] = ('Вы забираете: ' if rule == 'take' else 'Вы передаёте: ') + name + '.'
    elif rule == 'force_open':
        if outcome >= 2:
            target.update(open=True, locked=False, damaged=outcome == 2)
        elif outcome == 0:
            target['jammed'] = True
        result['text'] = (name + ': проход открыт.' if outcome >= 2 else name + ': не поддаётся.' + (' Механизм заклинило: следующие силовые попытки с −2.' if outcome == 0 else ' Можно изменить способ или подготовку.'))
    elif rule == 'pick_lock':
        lock = target['lock']
        if outcome >= 2:
            lock['progress'] = min(lock['required'], lock.get('progress', 0) + (2 if outcome == 3 else 1))
            lock['traces'] = lock.get('traces', False) or outcome == 2
            if lock['progress'] >= lock['required']:
                target['locked'] = False
        if outcome == 0:
            result['effects'].append({'type': 'break_tool', 'item': action['tool_id']})
        result['text'] = f"{name}: прогресс замка {lock['progress']}/{lock['required']}. " + ('Замок открыт; крышку можно открыть без броска.' if not target['locked'] else 'Замок пока закрыт.')
        if outcome == 0:
            result['text'] += ' Набор взломщика сломан; требуется ремонт или замена.'
    elif rule == 'make_impression':
        old = ATTITUDES.index(attitude(target, actor))
        target.setdefault('impressions', {})[actor] = ATTITUDES[max(0, min(4, old + (-1, 0, 1, 2)[outcome]))]
        target['social_revision'] = target.get('social_revision', 0) + 1
        result['text'] = name + ': отношение к вам — ' + ATTITUDE_LABELS[attitude(target, actor)] + '.'
    elif rule in ('request', 'coerce'):
        if action.get('already_agreed'):
            result['text'] = name + ': согласие на эту услугу уже получено и продолжает действовать.'
            return result
        if outcome >= 2:
            if action.get('commitment'):
                commitment = action['commitment']
                terms = {'description': commitment['name'], 'task': commitment['task'],
                         'help_skill': commitment['help_skill'], 'follow': commitment['follow']}
                label = commitment['name']
            else:
                terms = {'service': step['service']}
                label = target['services'][step['service']]['name']
            target.setdefault('agreements', {})[actor] = {**terms, 'expires': world['time'] + 86400, 'coerced': rule == 'coerce'}
            if action.get('commitment'):
                previous = [c for c in target.get('commitments', []) if c['expires'] > world['time']]
                target['commitments'] = (previous + [{'actor': actor, **target['agreements'][actor]}])[-32:]
            result['text'] = name + ': соглашается — ' + label + '.'
            if outcome == 2:
                result['text'] += ' Условие: только оговорённая услуга, без опасности для помощника.'
        else:
            result['text'] = name + ': не соглашается. Можно изменить просьбу или подход.'
            if outcome == 0 and rule == 'request':
                shift_attitude(target, actor, -1)
        if rule == 'coerce':
            shift_attitude(target, actor, -1)
            if outcome == 0:
                target.setdefault('coerce_immunity', {})[actor] = world['time'] + 604800
            result['text'] += ' Угрозы ухудшили отношение; NPC помнит принуждение.'
    elif rule == 'sense_motive':
        target.setdefault('assessed', {})[actor] = target.get('social_revision', 0)
        result['text'] = ('Вы замечаете намерение: ' + target.get('motive', 'явного скрытого замысла не видно') if outcome == 3 else 'Вы оцениваете поведение: ' + target.get('demeanor', 'обычное в этой ситуации') if outcome == 2 else 'Вам не удаётся уверенно оценить намерения собеседника.')
    elif rule == 'seek':
        if outcome >= 2:
            target['hidden'] = False
        result['text'] = 'Вы обнаружили: ' + name + '.' if outcome >= 2 else 'Поиск не дал результата.'
    elif rule == 'aid':
        if action.get('player_aid'):
            aids = world.setdefault('aid', {})
            for recipient, aid in list(aids.items()):
                if aid.get('helper_id') == actor:
                    aids.pop(recipient)
            aids[key] = {**action['player_aid'], 'expires': world['time'] + 60}
            result['text'] = world['entities'][actor]['name'] + ': готовится помочь ' + name + ' с задачей «' + world['entities'][step['task']]['name'] + '». Бросок помощника потребуется при попытке товарища.'
        else:
            world.setdefault('aid', {})[actor] = {'npc': key, 'task': step['task'], 'skill': step['skill'], 'expires': world['time'] + 60}
            result['text'] = name + ': готовится помочь следующей проверке — ' + world['entities'][step['task']]['name'] + '.'
    elif rule == 'improvise':
        if outcome >= 2:
            if action['ruling']['effect'] == 'open':
                target.update(open=True, locked=False)
            else:
                target['hidden'] = False
        result['text'] = name + (': необычный способ сработал.' if outcome >= 2 else ': попытка не удалась; можно попробовать другой подход.')
    from .campaign_leads import reveal, visit
    if rule == 'move':visit(world,action['destination'],actor)
    sources=[key]
    if rule=='look' and key in ('',location(world,actor)):
        from .evening import inspection_sources
        sources.extend(inspection_sources(world,actor,location(world,actor)))
    for source_id in sources:
        lead = reveal(world,source_id,actor,rule)
        if lead:
            result.setdefault('discoveries',[]).append(lead)
            result['text'] += ' Вы обнаружили: ' + lead['observation'] + ' След ведёт к «' + world['places'][lead['destination']]['name'] + '». ' + lead['question']
    from .evening import refresh
    refresh(world)
    for quest in world.get('quests', {}).values():
        if not quest.get('completed') and world['entities'][quest['item']].get('owner') == quest['owner']:
            quest['completed'] = True
            quest['completed_by'] = actor
            result['text'] += ' ' + quest['text']
    return result


def aid_bonus(outcome: int, rank: int) -> int:
    """Player Core Aid degrees, shared by NPC and player-controlled helpers."""
    return [-1, 0, 1, 4 if rank == 4 else 3 if rank == 3 else 2][outcome]


def take_player_aid(world: dict, actor: dict, prepared: dict) -> dict | None:
    """Consume matching preparation, never roll a player's dice here."""
    aid = world.get('aid', {}).get(actor['id'])
    if not aid or not aid.get('helper_id') or aid['task'] != prepared['target']:
        return None
    world['aid'].pop(actor['id'])
    helper = world['entities'].get(aid['helper_id'], {})
    if aid['expires'] <= world.get('time', 0) or helper.get('kind') != 'pc' or helper.get('status', 'standing') != 'standing':
        return None
    if aid['helper_id'] not in world.get('party', []) or location(world, aid['helper_id']) != location(world, actor['id']):
        return None
    return deepcopy(aid)


def aid_roll(world: dict, actor: dict, prepared: dict, roll_die) -> dict | None:
    """Aid is a single prepared, willing helper's task-specific reaction."""
    aid = world.get('aid', {}).get(actor['id'])
    if not aid or aid.get('helper_id') or aid['task'] != prepared['target'] or aid['skill'] != prepared['skill'] or aid['expires'] <= world.get('time', 0):
        return None
    npc = world['entities'][aid['npc']]
    if npc.get('status', 'standing') != 'standing' or location(world, aid['npc']) != location(world, actor['id']):
        return None
    world['aid'].pop(actor['id'])
    modifier = npc.get('skills', {}).get(aid['skill'], 0)
    natural = roll_die(20)
    outcome = degree(natural, natural + modifier, AID_DC)
    rank = npc.get('ranks', {}).get(aid['skill'], 0)
    bonus = aid_bonus(outcome, rank)
    return {**aid, 'natural': natural, 'modifier': modifier, 'dc': AID_DC, 'degree': outcome, 'bonus': bonus}
