"""Stable scenario knowledge and current-turn grounding for a small GM model."""
from copy import deepcopy
import re

NPCS = {
    'hostess': {'name':'Хозяйка','location':'hall',
                'goal':'Вернуть дорожную книгу и сохранить порядок в постоялом дворе.',
                'knowledge':['Дорожная книга пропала.','Конюх во дворе мог что-то видеть.'],
                'dialogue':'Может обсуждать награду за помощь; просьба о плате сама по себе не означает покупку её книги.'},
    'stablehand': {'name':'Конюх','location':'yard',
                   'goal':'Избежать неприятностей, но помочь вернуть книгу, если можно говорить безопасно.',
                   'knowledge':['Видел, как дорожную книгу спрятали в кладовой у сундука.'],
                   'dialogue':'Осторожный свидетель. После угроз насторожен, но слышит новые вопросы, объяснения и попытки примирения; может отвечать по существу, ставить условия или искать защиты.'}}

CHECK_ALIASES = {
    'perception': ('восприяти','нимательност','perception'),
    'intimidation': ('запугивани','intimidation'),
    'athletics': ('атлетик','athletics'),
    'acrobatics': ('акробатик','acrobatics'),
    'thievery': ('воровств','thievery'),
    'stealth': ('скрытност','stealth'),
    'diplomacy': ('дипломати','убеждени','diplomacy'),
}


def requested_check(text: str) -> str | None:
    """Only an explicit named check, not every inspection or social question."""
    text = text.lower().strip()
    if re.match(r'^(?:почему|зачем|как|что значит|можно ли|нужно ли)\b',text) or re.search(r'\bне\s+(?:хочу\s+)?(?:провер|брос|кинут)',text):
        return None
    if re.search(r'\b(?:убеждаю|уговариваю|пытаюсь\s+(?:убедить|уговорить)|убедить\s+пытаюсь)\b',text):
        return 'diplomacy'
    if not re.search(r'проверк|проверить|проверяю|брос(?:ить|аю|ок)|кинуть\s+(?:кости|куб)', text):
        return None
    matches = [skill for skill, names in CHECK_ALIASES.items() if any(n in text for n in names)]
    if not matches and re.search(r'лж[её]|лг[аё]|подозр|обман|скрыва',text):
        return 'perception'
    return matches[0] if len(matches) == 1 else None


def unavailable_spell(text: str) -> str | None:
    # A narrow guard for the reported explicit unsupported cast; questions,
    # threats, quotations and ordinary thrown objects remain GM interpretation.
    if not re.match(r'^\s*(?:(?:хочу|пытаюсь)\s+)?(?:кидаю|кинуть|бросаю|бросить|кастую|кастовать|применяю|применить|использую|использовать|запускаю|запустить|атакую)\b',text,re.I):
        return None
    if re.search(r'\b(?:фа[йе]рбол\w*|fireball|огненн\w*\s+шар\w*)\b',text,re.I):
        return 'Фаербол'
    return None


def npc_context(state: dict) -> list[dict]:
    from .adventure_support import helper_location
    return [{**deepcopy(npc), 'id':key, 'location':helper_location(state,key),
             'agreed_to_help':state.get('npc_help',{}).get(key,False),
             'outcome':state.get('npc_outcomes',{}).get(key,'standing')}
            for key,npc in NPCS.items() if helper_location(state,key)==state['location']]


def conversation_context(state: dict, text: str) -> dict:
    # Keep the full stored history intact. Collapse consecutive repeated GM
    # speeches only in the prompt so repetition is not reinforced as few-shot style.
    history=[]
    previous_words=set()
    for entry in state['history'][-12:]:
        entry=deepcopy(entry)
        words=set(re.findall(r'\w+',entry.get('gm','').lower()))
        overlap=len(words & previous_words)/max(1,len(words | previous_words))
        if entry.get('action')=='narrate' and overlap>0.65:
            entry['gm']='[Повтор предыдущего отказа без нового ответа; не использовать как образец следующей реплики.]'
        else:
            previous_words=words
        history.append(entry)
    return {'history':history, 'present_npcs':npc_context(state),
            'current_turn':{'player_request':text,'explicit_check':requested_check(text),
                            'unavailable_spell':unavailable_spell(text),
                            'instruction':'Ответь именно на последнее сообщение. Настороженность NPC сохраняет отношение, но не заменяет содержательный ответ.'}}


def ground_decision(decision: dict, text: str, skills: dict, actor: dict, location: str, state: dict | None = None) -> dict:
    """Deterministic contracts for clear requests; no hidden rerolls or AI retries."""
    missing=unavailable_spell(text)
    if missing:
        from .progression import describe
        build=describe(actor)
        spells=', '.join(s['name'] for s in (build or {}).get('spells',{}).values())
        return {**decision,'action':'narrate','destination':'','location_after':location,'skill':'','objective':'',
                'narration':f'Заклинание «{missing}» недоступно в текущей сборке {actor["name"]}. Оно не выпущено; ячейки не потрачены. '
                            +(f'Доступные заклинания: {spells}. Выберите другое заклинание или опишите иной подход.' if spells else 'У персонажа нет реализованных заклинаний. Можно выбрать иной подход.')}
    # A question about a place/object cannot authorize changing the player's location.
    if re.match(r'^\s*(?:как|почему|зачем|где|кто|что|вижу|видно)\b',text,re.I):
        if decision['action']=='move' or decision.get('location_after',location)!=location:
            decision={**decision,'action':'narrate','destination':'','location_after':location,'objective':''}
    skill=requested_check(text)
    if skill and skill in skills and decision['action']=='narrate':
        decision={**decision,'action':'check','skill':skill,'objective':'','destination':'','location_after':location}
    destination=requested_destination(text,state or {})
    if destination:
        decision={**decision,'action':'move','destination':destination,'location_after':destination,'skill':'','objective':''}
    if state is not None and re.match(r'^\s*(?:забираю|беру|подбираю)\b',text,re.I):
        from .adventure_inventory import available_items
        candidates=[key for key,item in available_items(state).items() if any(alias in text.lower() for alias in item.get('aliases',[]))]
        if len(candidates)==1:
            decision={**decision,'action':'take','objective':candidates[0],'destination':'','location_after':location,'skill':''}
    if state is not None and decision['action']=='check' and decision.get('skill')=='diplomacy':
        from .adventure_support import wants_chest_help
        if wants_chest_help(state,text):decision={**decision,'objective':'help_chest'}
    return decision


def requested_utility(text: str) -> str | None:
    for spell, aliases in (('light',r'свет|light'),('detect_magic',r'обнаружение магии|detect magic')):
        if re.search(r'(?:заклинание|заговор|кастую|применяю|создаю)\s+[«"]?(?:'+aliases+r')\b',text,re.I):return spell
    return None


def requested_destination(text: str, state: dict) -> str | None:
    if not re.match(r'^\s*(?:иду|ид[её]м|перехожу|переходим|возвращаюсь|возвращаемся|бегу|направляюсь)\b',text,re.I):return None
    for key,pattern in (('store',r'\bв\s+кладов'),('hall',r'\bв\s+зал'),('yard',r'\bво?\s+двор')):
        if re.search(pattern,text,re.I):return key
    from .adventure_support import helper_location
    for npc,pattern in (('stablehand',r'\bк\s+конюх'),('hostess',r'\bк\s+хозяйк')):
        if re.search(pattern,text,re.I):return helper_location(state,npc)
    return None


def travel_route(world: dict, start: str, destination: str) -> list[str]:
    if destination not in world:raise ValueError('Мастер предложил неизвестное помещение.')
    pending=[[start]];visited={start}
    for path in pending:
        if path[-1]==destination:return path
        for neighbor in world[path[-1]]['exits']:
            if neighbor not in visited:visited.add(neighbor);pending.append(path+[neighbor])
    raise ValueError('Мастер предложил недоступный проход.')


def repeated_narration(decision: dict, history: list[dict], text: str = '') -> bool:
    if re.search(r'\bповтори(?:те)?\b',text,re.I):return False
    if decision.get('action')!='narrate':return False
    words=set(re.findall(r'\w+',decision.get('narration','').lower()))
    if len(words)<8:return False
    for entry in history[-6:]:
        other=set(re.findall(r'\w+',entry.get('gm','').lower()))
        if len(words & other)/max(1,len(words | other))>0.65:return True
    return False


def rewrite_context(context: dict) -> dict:
    context=deepcopy(context)
    for key in ('history','earlier_interactions'):
        context[key]=[{k:v for k,v in h.items() if k!='gm'} for h in context.get(key,[])]
    context['task']='Предыдущий вариант повторял старую реплику и не отвечал на новый вопрос. Напиши содержательный ответ на current_turn.player_request по present_npcs. Используй собственную новую реплику NPC; не описывай его руки, ремни, взгляд и позу. Объясни мотив или сообщи уместные сведения/условие разговора. Числовые результаты не меняются.'
    return context
