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
    'diplomacy': ('дипломати','diplomacy'),
}


def requested_check(text: str) -> str | None:
    """Only an explicit named check, not every inspection or social question."""
    text = text.lower().strip()
    if re.match(r'^(?:почему|зачем|как|что значит|можно ли|нужно ли)\b',text) or re.search(r'\bне\s+(?:хочу\s+)?(?:провер|брос|кинут)',text):
        return None
    if not re.search(r'проверк|проверить|проверяю|брос(?:ить|аю|ок)|кинуть\s+(?:кости|куб)', text):
        return None
    matches = [skill for skill, names in CHECK_ALIASES.items() if any(n in text for n in names)]
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
    return [{**deepcopy(npc), 'id':key, 'outcome':state.get('npc_outcomes',{}).get(key,'standing')}
            for key,npc in NPCS.items() if npc['location']==state['location']]


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


def ground_decision(decision: dict, text: str, skills: dict, actor: dict, location: str) -> dict:
    """Deterministic contracts for clear requests; no hidden rerolls or AI retries."""
    missing=unavailable_spell(text)
    if missing:
        from .progression import describe
        build=describe(actor)
        spells=', '.join(s['name'] for s in (build or {}).get('spells',{}).values())
        return {**decision,'action':'narrate','destination':'','location_after':location,'skill':'','objective':'',
                'narration':f'Заклинание «{missing}» недоступно в текущей сборке {actor["name"]}. Оно не выпущено; ячейки не потрачены. '
                            +(f'Доступные заклинания: {spells}. Выберите другое заклинание или опишите иной подход.' if spells else 'У персонажа нет реализованных заклинаний. Можно выбрать иной подход.')}
    skill=requested_check(text)
    if skill and skill in skills and decision['action']=='narrate':
        return {**decision,'action':'check','skill':skill,'objective':'','destination':'','location_after':location}
    return decision


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
