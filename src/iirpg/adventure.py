"""Persistent game-master sessions with validated, sequential action plans."""
from copy import deepcopy
from datetime import datetime, timezone
import json
import time
from uuid import UUID, uuid5
from .storage import MessageConflict
from .checks import degree
from .world import scenario, location
from .action_rules import RULES, AID_DC, validate_proposal, prepare, apply, aid_roll, aid_bonus, take_player_aid
from .session_context import PLANNER_PROMPT, context, schema, repeats_previous_reply
from .session_world import initialize, actor_view, sync_inventory, persist_effects, compatibility_view
from .character_resources import save_character
from .progression import rest
from .magic import utility
from .maps import install_map
from .story_memory import MAX_STORY_ENTRIES, fit_context
from . import story_narration, campaign, campaign_storage, evening, world_growth
from .ai import session_reasoning
from .session_agent import SessionAgent, AgentRejected
from .party import members, acting_character
from .roster import InventoryRules


def current_world(state):
    initialize(state)
    return deepcopy(state['world']['places'])


class AdventureStore:

    def __init__(self, ai):
        self.ai, self.store = ai, ai.store
        with self.store._connection() as c:
            campaign_storage.install(c)
            c.executescript('CREATE TABLE IF NOT EXISTS adventures(room_id TEXT PRIMARY KEY,body TEXT NOT NULL); CREATE TABLE IF NOT EXISTS adventure_requests(room_id TEXT,request_id TEXT,command TEXT,body TEXT,PRIMARY KEY(room_id,request_id)); CREATE TABLE IF NOT EXISTS session_training_archive(room_id TEXT,encounter_id TEXT,body TEXT,PRIMARY KEY(room_id,encounter_id));')

    def publish(self,c,room,state):
        start=state['revision']-len(state['history'])
        for index,entry in enumerate(state['history'],start+1):
            index=entry.setdefault('chat_index',index)
            for sender,text in (('first',entry['player']),('second',entry['gm']+'\n\n'+entry.get('result',''))):
                if not text.strip():continue
                key=str(uuid5(UUID(entry['player_request_id']),'session.player')) if sender=='first' and entry.get('player_request_id') else str(uuid5(UUID(room),f'session:{index}:{sender}'))
                envelope={'schema_version':1,'type':'chat.message','sender':sender,'text':text.strip(),'visibility':{'mode':'all','chat_ids':[]},'choices':[],'metadata':{'session_revision':index,**{k:entry[k] for k in ('player_id','player_name','character_id','actor_name') if entry.get(k)},**({'combat_id':entry['combat_id']} if entry.get('combat_id') else {}),**({'cancelled_offer_id':entry['cancelled_offer_id']} if entry.get('cancelled_offer_id') else {})},'created_at':datetime.now(timezone.utc).isoformat()}
                c.execute('INSERT OR IGNORE INTO queue_events(room_id,request_id,envelope) VALUES(?,?,?)',(room,key,json.dumps(envelope,ensure_ascii=False)))

    def sync_combat(self,c,room,state):
        link=state.get('active_combat')
        if not link:return
        row=c.execute('SELECT body FROM encounters WHERE room_id=?',(room,)).fetchone()
        battle=json.loads(row[0]) if row else {}
        if battle.get('id')!=link['id']:raise ValueError('Связанный бой недоступен.')
        if battle['status']=='active':return
        heroes=[f for f in battle['fighters'] if f.get('character_id') in members(state)]
        hero=next((f for f in heroes if f.get('character_id')==state['character_id']),heroes[0])
        if state.get('party_ids'):
            state.setdefault('hero_hps',{}).update({f['character_id']:f['hp'] for f in heroes})
            for fighter in heroes:
                actor, _ = self.actor(c,room,fighter['character_id'])
                actor['sheet']['values']['hp_current']=fighter['hp']
                save_character(c,room,actor)
        enemy=next(f for f in battle['fighters'] if f['team']=='enemies')
        if hero.get('character_id')==state['character_id']:state['hero_hp']=hero['hp']
        outcome='surrendered' if enemy.get('surrendered') else 'defeated' if enemy['hp']<=0 else 'standing'
        state.setdefault('npc_outcomes',{})[link['target']]=outcome
        initialize(state)
        state['world']['entities'][link['target']]['status']=outcome
        text='Столкновение окончено. '+battle['winner']+'. '+enemy['name']+': '+{'surrendered':'сдался','defeated':'выбыл и не продолжает сопротивление','standing':'остался на ногах'}[outcome]+f". ОЗ героя: {hero['hp']}/{hero['max_hp']}. Это нелетальная учебная схватка; нулевые ОЗ не означают смерть."
        state['history']=(state['history']+[{'player':'','gm':text,'action':'combat_result'}])[-MAX_STORY_ENTRIES:]
        state['last_combat']=state.pop('active_combat');state['revision']+=1
        if state.get('plan', {}).get('status') == 'awaiting_combat':
            state['plan'].update(status='awaiting_choice', reason='Бой завершён; дальнейшие действия выберите следующим сообщением.')
        c.execute('UPDATE adventures SET body=? WHERE room_id=?',(json.dumps(state,ensure_ascii=False),room))
        self.publish(c,room,state)

    def react(self, room: str, offer_id: str) -> dict:
        UUID(offer_id)
        state=self.read(room)
        # A reaction is tied to the completed check, never to a caller's invented result.
        with self.store._connection() as c:
            row=c.execute('SELECT envelope FROM queue_events WHERE room_id=? AND request_id=?',(room,str(uuid5(UUID(str(uuid5(UUID(offer_id),'dice.offer.accept'))),'tool.result')))).fetchone()
        if not row:raise ValueError('Сначала выполните проверку.')
        if not json.loads(row[0]).get('metadata',{}).get('session_check'):raise ValueError('Это не проверка сессии.')
        key=str(uuid5(UUID(offer_id),'session.reaction'+(':'+state['timeline_id'] if state.get('timeline_id') else '')))
        with self.store._connection() as c:
            prior=c.execute('SELECT body FROM adventure_requests WHERE room_id=? AND request_id=?',(room,key)).fetchone()
        if prior and json.loads(prior[0]).get('status')=='done':
            with self.store._connection() as c:return campaign_storage.thaw_result(c,json.loads(prior[0])['result'])
        latest=next((h for h in reversed(state['history']) if h.get('action')=='check_result'),{})
        if latest.get('offer_id')!=offer_id:raise ValueError('Это не последняя проверка сессии.')
        if state.get('active_combat'): return state
        # A helper result already explains the modifier and offers the ally's
        # card. No NPC may speak for the two players or finish their next check.
        if latest.get('rule') == 'aid_check': return state
        speakers = [key for key, npc in state['world']['entities'].items() if npc['kind'] == 'npc'
                    and npc.get('status', 'standing') in ('standing', 'surrendered')
                    and location(state['world'], key) == state['location']]
        if not speakers: return state
        return self.execute(room,{'revision':state['revision'],'character_id':latest.get('character_id',state['character_id']),'text':'Опиши немедленные последствия последней проверки по результату кубика. Не назначай новый бросок. Если действие затронуло NPC, опиши его реакцию с учётом предыдущего общения. Агрессию учитывай только если игрок угрожал NPC или нападал на него. Для работы с предметами без NPC не упоминай агрессию и отсутствие свидетелей.'},key,reaction=True)


    def _load(self, c, room):
        self.store._require_room(c, room)
        row = c.execute('SELECT body FROM adventures WHERE room_id=?', (room,)).fetchone()
        return campaign_storage.hydrate(c, json.loads(row[0])) if row else {'revision': 0, 'location': 'hall', 'history': [], 'character_id': None}

    def save(self, c, room, state):
        compatibility_view(state)
        evening.refresh(state['world'])
        active=state['world'].get('adventure') if evening.enabled(state) else None
        if active:
            episode=next(e for e in state['campaign']['episodes'] if e['id']==active['id'])
            episode['status']=active['status']
            if active['status']=='completed':
                episode.update(outcome=active['outcome'],summary=active['summary'])
                if 'result_ref' not in episode:
                    episode['result_ref']=campaign_storage.put(c,'evening_result',{'adventure':active,'evidence':evening.found(state['world'])})
        campaign_storage.archive(c, state)
        c.execute('INSERT INTO adventures VALUES(?,?) ON CONFLICT(room_id) DO UPDATE SET body=excluded.body',
                  (room, json.dumps(campaign_storage.pack(c, state), ensure_ascii=False)))

    def sync_inventory(self, c, room, state):
        sync_inventory(c, room, state, self.ai.combat.roster._load(c, room))

    def view(self, state):
        compatibility_view(state)
        return {**deepcopy(state), **({'adventure':evening.public(state)} if evening.enabled(state) else {}), 'location_id': state['location'],
                'place': deepcopy(state['world']['places'][state['location']]),
                'places': {k: v['name'] for k, v in state['world']['places'].items()}}

    def read(self, room: str) -> dict:
        with self.store._connection() as c:
            c.execute('BEGIN IMMEDIATE')
            state = self._load(c, room)
            self.sync_inventory(c, room, state)
            self.sync_combat(c, room, state)
            self.resolve_pending(c, room, state)
            self.publish(c, room, state)
            self.save(c, room, state)
        return self.view(state)

    def status(self, room: str) -> dict:
        """Public activity indicator without exposing prompts or provider traces."""
        with self.store._connection() as c:
            self.store._require_room(c,room)
            saved=c.execute('SELECT body FROM adventures WHERE room_id=?',(room,)).fetchone()
            public=evening.public(json.loads(saved[0])) if saved else None
            extra={'adventure':public} if public else {}
            rows=c.execute('SELECT command,body FROM ai_requests WHERE room_id=? AND started>? ORDER BY started',(room,time.time()-120)).fetchall()
            for row in rows:
                trace=json.loads(row['body'])
                if trace.get('status') not in ('running','chosen'):continue
                actor_id=json.loads(row['command']).get('character_id')
                actor=c.execute('SELECT body FROM characters WHERE room_id=? AND id=?',(room,actor_id)).fetchone() if actor_id else None
                return {'thinking':True,'character_id':actor_id,'actor_name':json.loads(actor[0])['name'] if actor else None,**extra}
        return {'thinking':False,**extra}

    def actor(self, c, room, identity):
        roster = self.ai.combat.roster._load(c, room)
        actor = next((a for a in roster['characters'] if a['id'] == identity), None)
        if not actor: raise ValueError('Выберите существующего персонажа.')
        return actor_view(actor, roster['character_rules']), roster

    def start_combat(self, c, room, state, actor, action, key):
        target = state['world']['entities'][action['target']]
        row = c.execute('SELECT body FROM encounters WHERE room_id=?', (room,)).fetchone()
        battle = json.loads(row[0]) if row else {'revision': 0}
        if battle.get('status') == 'active':
            if battle.get('session_link'): raise ValueError('В комнате уже идёт сюжетный бой.')
            c.execute('INSERT OR IGNORE INTO session_training_archive VALUES(?,?,?)', (room, battle['id'], json.dumps(battle, ensure_ascii=False)))
            battle.update(status='cancelled', revision=battle['revision'] + 1)
            c.execute('UPDATE encounters SET body=? WHERE room_id=?', (json.dumps(battle, ensure_ascii=False), room))
        scene = self.ai.scene._load(c, room)
        tactical = deepcopy(self.ai.combat.maps['tavern'])
        tactical['name'] = state['world']['places'][state['location']]['name']
        tactical['description'] = 'Тактическая схема места столкновения.'
        install_map(scene, tactical)
        c.execute('INSERT INTO scenes VALUES(?,?) ON CONFLICT(room_id) DO UPDATE SET body=excluded.body', (room, json.dumps(scene, ensure_ascii=False)))
        link = {'target': action['target'], 'target_name': target['name'], 'location': state['location'], 'hero_hp': state.get('hero_hp') if not state.get('party_ids') else None, 'hero_hps':state.get('hero_hps',{})}
        party=[]
        for identity in members(state):
            member,_=self.actor(c,room,identity)
            if state.get('hero_hps',{}).get(identity,member['sheet']['values']['hp_current'])>0:party.append(identity)
        battle = self.ai.combat.execute(room, {'action': 'start', 'revision': battle['revision'], 'character_ids': party, 'group_id': target['combat_group']},
                                       key, _connection=c, _session=link)
        state['active_combat'] = {**link, 'id': battle['id']}
        return {'rule': 'combat', 'target': action['target'], 'combat_id': battle['id'],
                'text': 'Начинается столкновение с ' + target['name'] + '. Сначала инициатива; попадание ещё не определено.', 'effects': []}

    def check_card(self, state: dict, actor: dict, roster: dict, action: dict, step_key: str,
                   aid: dict | None = None) -> dict:
        """Freeze a card without publishing it or rolling dice."""
        offer_id = str(uuid5(UUID(step_key), 'session.check'))
        pending = {'skill': action['skill'], 'modifier': action['modifier'], 'dc': action['dc'],
                   'offer_id': offer_id, 'roll_request_id': str(uuid5(UUID(offer_id), 'dice.offer.accept')),
                   'prepared_action': deepcopy(action), 'plan_id': state['plan']['id'],
                   'character_id': actor['id'], 'actor_name': actor['name'], 'player_id': actor.get('player_id')}
        if action.get('modifier_breakdown'):
            pending['modifier_breakdown'] = deepcopy(action['modifier_breakdown'])
        if aid: pending['aid'] = deepcopy(aid)
        target = state['world']['entities'].get(action['target'], {})
        detail = RULES[action['rule']].get('label', action['rule']) + ' — ' + target.get('name', action['target'])
        if action['step'].get('service'):
            detail += ': ' + target.get('services', {}).get(action['step']['service'], {}).get('name', action['step']['service'])
        elif action.get('commitment'):
            detail += ': ' + action['commitment']['name']
        pending['description'] = detail
        pending['label'] = roster['character_rules']['profiles'][actor['sheet']['profile']]['statistics'][action['skill']]['label']
        return pending

    def player_aid_card(self, c, room: str, state: dict, assisted: dict, aid: dict) -> dict | None:
        """An ally's attempt requests the helper's own roll before its own card."""
        try:
            helper, roster = self.actor(c, room, aid['helper_id'])
        except ValueError:
            return None
        if helper['sheet']['values']['hp_current'] <= 0:
            return None
        skill = aid['skill']
        if skill not in helper.get('aid_skills', []):
            return None
        offer_id = str(uuid5(UUID(assisted['offer_id']), 'player.aid:' + helper['id']))
        label = 'Помочь ' + assisted['actor_name'] + ' — ' + state['world']['entities'][aid['task']]['name']
        action = {'rule': 'aid_check', 'actor': helper['id'], 'target': assisted['character_id'],
                  'skill': skill, 'modifier': helper['stats'][skill], 'dc': AID_DC, 'seconds': 0,
                  'rank': helper.get('ranks', {}).get(skill, 0), 'step': {
                      'rule': 'aid', 'target': assisted['character_id'], 'task': aid['task'], 'skill': skill}}
        return {'kind': 'player_aid', 'skill': skill, 'modifier': action['modifier'], 'dc': AID_DC,
                'offer_id': offer_id, 'roll_request_id': str(uuid5(UUID(offer_id), 'dice.offer.accept')),
                'prepared_action': action, 'assisted_check': deepcopy(assisted),
                'plan_id': assisted['plan_id'], 'character_id': helper['id'], 'actor_name': helper['name'],
                'player_id': helper.get('player_id'),
                'label': roster['character_rules']['profiles'][helper['sheet']['profile']]['statistics'][skill]['label'], 'description': label,
                'modifier_breakdown': [{'label': 'Навык помощника', 'value': action['modifier'], 'character_id': helper['id']}]}

    def run_plan(self, c, room, state):
        plan = state['plan']
        events = []
        while plan['index'] < len(plan['steps']):
            actor, roster = self.actor(c, room, state.get('plan',{}).get('character_id',state['character_id']))
            step_key = str(uuid5(UUID(plan['id']), 'step:' + str(plan['index'])))
            try:
                step = plan['steps'][plan['index']]
                target = state['world']['entities'].get(step.get('target'), {})
                if step['rule'] == 'hold' and target.get('owner') != actor['id']:
                    plan['steps'].insert(plan['index'], {'rule': 'take', 'target': step['target']})
                    step = plan['steps'][plan['index']]
                action = prepare(state['world'], actor, step, roster['rules'])
                if action['rule']=='give':
                    recipient=next((a for a in roster['characters'] if a['id']==action['step'].get('recipient')),None)
                    item=state['world']['entities'][action['target']]
                    if recipient and item['kind']=='item':
                        trial=deepcopy(recipient);trial['items'].append(InventoryRules(roster['rules']).item(item['item']))
                        if InventoryRules(roster['rules']).summary(trial)['over_capacity']:raise ValueError('Получатель не сможет нести этот предмет: превышен Bulk.')
            except ValueError as error:
                plan.update(status='blocked', reason=str(error))
                events.append({'rule': plan['steps'][plan['index']]['rule'], 'text': str(error), 'blocked': True})
                break
            participant = state['world']['entities'].get(action['target'], {})
            if participant.get('kind') == 'npc': state['conversation_with'] = action['target']
            if action.get('trade'): state['conversation_with'] = action['trade']['merchant_id']
            if action.get('skill'):
                player_aid = take_player_aid(state['world'], actor, action)
                aid = aid_roll(state['world'], actor, action, self.ai.combat.roll_die) if not player_aid else None
                if aid:
                    action['modifier'] += aid['bonus']
                    action.setdefault('modifier_breakdown', []).append({'label': 'Помощь NPC', 'value': aid['bonus'], 'helper_id': aid['npc']})
                pending = self.check_card(state, actor, roster, action, step_key, aid)
                if player_aid:
                    pending = self.player_aid_card(c, room, state, pending, player_aid) or pending
                state['pending_check'] = pending
                plan['status'] = 'awaiting_roll'
                self.offer(c, room, pending)
                events.append({'rule': action['rule'], 'target': action['target'], 'text': f"{pending['description']}. Проверка: {pending['label']} ({pending['modifier']:+d}), КС {pending['dc']}. Ожидает броска {pending['actor_name']} в карточке проверки."})
                break
            if action['rule'] == 'combat':
                event = self.start_combat(c, room, state, actor, action, step_key)
                plan.update(status='awaiting_combat', index=plan['index'] + 1)
            else:
                event = apply(state['world'], action)
                persist_effects(c, room, state, actor, event['effects'], roster['rules'])
                if action['rule'] == 'cast':
                    ability = action['step']['ability']
                    if ability == 'detect_magic':
                        detected = any(e.get('magical') and location(state['world'], k) == state['location'] for k, e in state['world']['entities'].items())
                        event['text'] = 'В пределах текущего помещения ощущается неизвестная магия.' if detected else 'В пределах текущего помещения неизвестной магии не обнаружено.'
                    else:
                        event['text'] = utility(actor.get('build', {}), ability)
                    save_character(c, room, actor)
                if action['rule'] == 'rest':
                    if state.get('hero_hp') is not None:
                        actor['sheet']['values']['hp_current'] = min(actor['sheet']['values']['hp_current'], state.get('hero_hps',{}).get(actor['id'], state['hero_hp']) if actor['id']==state['character_id'] else state.get('hero_hps',{}).get(actor['id'], actor['sheet']['values']['hp_current']))
                    event['text'] = rest(actor)
                    if state.get('party_ids'):state.setdefault('hero_hps',{})[actor['id']]=actor['sheet']['values']['hp_current']
                    if actor['id']==state['character_id']:state['hero_hp'] = actor['sheet']['values']['hp_current']
                    state['elapsed_hours'] = state.get('elapsed_hours', 0) + 24
                    save_character(c, room, actor)
                plan['index'] += 1
            state['location'] = location(state['world'], actor['id'])
            events.append(event)
            plan.setdefault('receipts', []).append({'step': step_key, **event})
            if state.get('active_combat'): break
        if plan['index'] == len(plan['steps']) and plan['status'] not in ('awaiting_combat', 'blocked'):
            plan['status'] = 'completed'
        return events

    def offer(self, c, room, pending):
        from .dice import DiceTool
        modifier, dc = pending['modifier'], pending['dc']
        arguments = DiceTool().prepare({'system': 'd20', 'roll': f'1d20{modifier:+d}',
                    'success': {'operator': '>=', 'target': dc}, 'label': pending.get('label', pending['skill'])}).arguments
        offer = {'schema_version': 1, 'type': 'tool.offer', 'sender': 'second', 'recipient': 'first',
                 'tool': 'dice.roll', 'arguments': arguments, 'roll_request_id': pending['roll_request_id'],
                 'text': pending.get('description', pending['skill']) + f". Бросьте проверку: {pending.get('label', pending['skill'])} ({modifier:+d}) против КС {dc}.",
                 'visibility': {'mode': 'all', 'chat_ids': []}, 'choices': [], 'metadata': {'session_check': True, **{k:pending[k] for k in ('character_id','actor_name','player_id') if pending.get(k)}},
                 'created_at': datetime.now(timezone.utc).isoformat()}
        if pending.get('aid'):
            aid = pending['aid']; offer['text'] += f" Помощь (Aid): d20={aid['natural']}, поправка {aid['bonus']:+d}; только эта проверка."
        c.execute('INSERT INTO queue_events(room_id,request_id,envelope) VALUES(?,?,?)', (room, pending['offer_id'], json.dumps(offer, ensure_ascii=False)))

    def migrate_pending(self, state):
        pending = state.get('pending_check')
        if not pending or 'prepared_action' in pending: return
        # An old offer is a frozen promise. Do not reprice it, require new tools or roll again.
        mapping = scenario(state['scenario_id']).get('legacy', {}).get('pending_checks', {}).get(pending.get('objective'), {})
        pending['legacy_effects'] = deepcopy(mapping.get('success', []))
        pending['prepared_action'] = {'rule': 'legacy_check', 'skill': pending['skill'], 'target': '', 'seconds': 2,
                                      'step': {}, 'actor': state['character_id']}

    def restore_assisted_check(self, c, room: str, state: dict, pending: dict,
                               aid: dict | None = None) -> dict:
        """The beneficiary's original frozen attempt resumes without a model call."""
        assisted = deepcopy(pending['assisted_check'])
        if aid is not None:
            assisted['aid'] = deepcopy(aid)
            assisted['modifier'] += aid['bonus']
            assisted['prepared_action']['modifier'] = assisted['modifier']
            part = {'label': 'Помощь ' + pending['actor_name'], 'value': aid['bonus'], 'helper_id': pending['character_id']}
            assisted.setdefault('modifier_breakdown', []).append(part)
            assisted['prepared_action']['modifier_breakdown'] = deepcopy(assisted['modifier_breakdown'])
        state['pending_check'] = assisted
        if state.get('plan', {}).get('id') == assisted['plan_id']:
            state['plan']['status'] = 'awaiting_roll'
        self.offer(c, room, assisted)
        return assisted

    def resolve_player_aid(self, c, room: str, state: dict, pending: dict,
                           natural: int, total: int, outcome: int) -> None:
        action = pending['prepared_action']
        bonus = aid_bonus(outcome, action['rank'])
        assisted = pending['assisted_check']
        aid = {'kind': 'player', 'helper_id': pending['character_id'], 'recipient_id': assisted['character_id'],
               'task': action['step']['task'], 'skill': pending['skill'], 'natural': natural,
               'modifier': pending['modifier'], 'dc': pending['dc'], 'degree': outcome, 'bonus': bonus}
        check = {k: aid[k] for k in ('skill', 'natural', 'modifier', 'dc', 'degree')}
        check.update(total=total, aid_bonus=bonus)
        text = (f"{pending['actor_name']} помогает {assisted['actor_name']}: d20={natural}, итог {total} против КС {pending['dc']}. "
                f"Поправка помощи {bonus:+d} только к этой проверке. Теперь бросает {assisted['actor_name']}: "
                f"{assisted['label']} ({assisted['modifier'] + bonus:+d}), КС {assisted['dc']}.")
        self.restore_assisted_check(c, room, state, pending, aid)
        state['revision'] += 1
        state['history'] = (state['history'] + [{'player': '', 'gm': text, 'check': check,
            'action': 'check_result', 'rule': 'aid_check', 'target': assisted['character_id'],
            'effects': [], 'offer_id': pending['offer_id'], 'character_id': pending['character_id'],
            'actor_name': pending['actor_name'], 'player_id': pending.get('player_id')}])[-MAX_STORY_ENTRIES:]
        self.publish(c, room, state)
        self.save(c, room, state)

    def resolve_pending(self, c, room, state):
        pending = state.get('pending_check')
        if not pending: return
        self.migrate_pending(state)
        result_id = str(uuid5(UUID(pending['roll_request_id']), 'tool.result'))
        row = c.execute('SELECT envelope FROM queue_events WHERE room_id=? AND request_id=?', (room, result_id)).fetchone()
        if not row: return
        event = json.loads(row[0])
        if event.get('offer_id') != pending['offer_id'] or event.get('requested_by') != 'first':
            raise ValueError('Результат не принадлежит ожидающей проверке.')
        actor, roster = self.actor(c, room, pending.get('character_id',state['character_id']))
        natural = event['tool_result']['rolls'][0]['values'][0]
        total = natural + pending['modifier']; outcome = degree(natural, total, pending['dc'])
        if pending.get('kind') == 'player_aid':
            self.resolve_player_aid(c, room, state, pending, natural, total, outcome)
            return
        check = {k: pending[k] for k in ('skill', 'modifier', 'dc')}
        check.update(natural=natural, total=total, degree=outcome)
        if pending.get('aid'): check['aid'] = pending['aid']
        action = pending['prepared_action']
        if action['rule'] == 'legacy_check':
            effects = []
            if outcome >= 2:
                for effect in pending.get('legacy_effects', []):
                    target = state['world']['entities'][effect['target']]
                    if effect['type'] == 'open': target.update(open=True, locked=False)
                    elif effect['type'] == 'take':
                        effects.append({'type': 'transfer', 'entity': effect['target'], 'from': target['owner'], 'to': actor['id']})
                        target['owner'] = actor['id']
                    elif effect['type'] == 'agreement':
                        target.setdefault('agreements', {})[actor['id']] = {'service': effect['service'], 'expires': state['world']['time'] + 86400}
            resolved = {'text': 'Сохранённая проверка разрешена по условиям исходной попытки.', 'effects': effects}
        else:
            resolved = apply(state['world'], action, outcome)
        persist_effects(c, room, state, actor, resolved['effects'], roster['rules'])
        participant = state['world']['entities'].get(action['target'], {})
        if participant.get('kind') == 'npc':
            participant['memories'] = (participant.get('memories', []) + [{'actor': actor['id'], 'rule': action['rule'], 'degree': outcome, 'time': state['world']['time'], 'service': action['step'].get('service'),
                **({'description': action['commitment']['name'], 'task': action['commitment']['task'], 'help_skill': action['commitment']['help_skill']} if action.get('commitment') else {})}])[-16:]
        text = f"{pending.get('label', pending['skill'])}: d20={natural}, итог {total} против КС {pending['dc']} — " + ['критический провал', 'провал', 'успех', 'критический успех'][outcome] + '. ' + resolved['text']
        state.pop('pending_check')
        plan = state.get('plan')
        if plan and plan.get('id') == pending.get('plan_id'):
            plan.setdefault('receipts', []).append({'step': str(uuid5(UUID(plan['id']), 'step:' + str(plan['index']))), 'check': check, **resolved})
            plan['index'] += 1
            if outcome >= 2:
                # Progress is not completion: stop at a still-locked multi-stage obstacle.
                target = state['world']['entities'].get(action['target'], {})
                if RULES.get(action['rule'], {}).get('stop_after_check'):
                    plan.update(status='awaiting_choice' if plan['index'] < len(plan['steps']) else 'completed', reason='Ответ NPC получен. Дальнейшие действия — после вашего следующего сообщения.')
                elif action['rule'] == 'pick_lock' and target.get('locked'):
                    plan.update(status='blocked', reason='Замок требует ещё успешных попыток; продолжение по новой заявке.')
                else:
                    plan['status'] = 'running'
                    text += ' ' + ' '.join(e['text'] for e in self.run_plan(c, room, state))
            else:
                plan.update(status='failed', reason='Проверка не удалась; оставшиеся шаги не выполнены.')
        state['revision'] += 1
        state['history'] = (state['history'] + [{'player': '', 'gm': text, 'check': check, 'action': 'check_result', 'rule': action['rule'], 'target': action['target'], 'effects': resolved['effects'], 'offer_id': pending['offer_id'], 'character_id':actor['id'], 'actor_name':actor['name'], 'player_id':actor.get('player_id'), **({'combat_id': state['active_combat']['id']} if state.get('active_combat') else {})}])[-MAX_STORY_ENTRIES:]
        self.publish(c, room, state)
        self.save(c, room, state)

    def cancel_pending(self, c, room, state, *, announce=True):
        pending = state.pop('pending_check', None)
        if not pending: return 'Нет ожидающей попытки для отмены.'
        state.setdefault('cancelled_offers', []).append(pending['offer_id'])
        if pending.get('kind') == 'player_aid':
            assisted = self.restore_assisted_check(c, room, state, pending)
            text = 'Помощь отменена без броска. Основная попытка сохранена: теперь бросает ' + assisted['actor_name'] + ', без поправки помощника.'
            if announce:
                self.event(c, room, str(uuid5(UUID(pending['offer_id']), 'cancelled')), 'second', text, {'cancelled_offer_id': pending['offer_id']})
            return text
        if state.get('plan'): state['plan']['status'] = 'cancelled'
        if announce:
            self.event(c, room, str(uuid5(UUID(pending['offer_id']), 'cancelled')), 'second', 'Попытка отменена; кости не брошены. Уже выполненные шаги сохранены.', {'cancelled_offer_id': pending['offer_id']})
        return 'Попытка отменена; кости не брошены. Уже выполненные шаги сохранены.'

    def cancel(self, room: str, offer_id: str) -> dict:
        UUID(offer_id)
        with self.store._connection() as c:
            c.execute('BEGIN IMMEDIATE')
            state = self._load(c, room)
            self.sync_inventory(c, room, state)
            if offer_id in state.get('cancelled_offers', []): return self.view(state)
            if any(json.loads(r[0]).get('status') in ('running', 'chosen') for r in c.execute('SELECT body FROM ai_requests WHERE room_id=? AND started>?', (room, time.time() - 120))):
                raise MessageConflict('Дождитесь ответа мастера перед отменой.')
            self.resolve_pending(c, room, state)
            if state.get('pending_check', {}).get('offer_id') != offer_id:
                raise MessageConflict('Эта проверка уже завершена или не ожидается.')
            self.cancel_pending(c, room, state)
            state['revision'] += 1
            self.save(c, room, state)
            return self.view(state)

    def event(self, c, room, key, sender, text, metadata):
        envelope = {'schema_version': 1, 'type': 'chat.message', 'sender': sender, 'text': text,
                    'visibility': {'mode': 'all', 'chat_ids': []}, 'choices': [], 'metadata': metadata,
                    'created_at': datetime.now(timezone.utc).isoformat()}
        c.execute('INSERT OR IGNORE INTO queue_events(room_id,request_id,envelope) VALUES(?,?,?)', (room, key, json.dumps(envelope, ensure_ascii=False)))

    def repair_repetition(self, room: str, key: str, payload: dict, proposal: dict) -> dict:
        """One metered semantic repair, never a transport retry or a rules override."""
        repair_key = str(uuid5(UUID(key), 'session.semantic_repair'))
        request = deepcopy(payload)
        data = json.loads(request['messages'][1]['content'])
        data['feedback'] = 'Предыдущий ответ повторил старую реплику вместо разбора нового player. Прочитай последнее сообщение, текущего собеседника и факты. Заново различи вопрос, уточнение и действие. Не продолжай старую тему автоматически. Верни тот же протокол.'
        data['previous_reply'] = proposal['reply'][:500]
        budget = self.ai.config.get('session_context_max_bytes', self.ai.config['context_max_bytes'])
        try:
            data = fit_context(data, budget)
        except ValueError:
            return proposal
        request['messages'][1]['content'] = json.dumps(data, ensure_ascii=False)
        now = time.time()
        trace = {'status': 'running', 'role': 'session_gm_repair', 'parent_request_id': key, 'started': now, 'request': request}
        with self.store._connection() as c:
            c.execute('BEGIN IMMEDIATE')
            used = c.execute("SELECT (SELECT COUNT(*) FROM ai_requests WHERE date(started,'unixepoch')=date(?,'unixepoch'))+COALESCE((SELECT count FROM ai_deleted_usage WHERE day=date(?,'unixepoch')),0)", (now, now)).fetchone()[0]
            if used >= self.ai.config['daily_request_limit']: return proposal
            c.execute('INSERT INTO ai_requests VALUES(?,?,?,?,?)', (room, repair_key, json.dumps({'parent': key}), now, json.dumps(trace, ensure_ascii=False)))
        try:
            response = self.ai.client.complete(request, self.ai.config['timeout_seconds'])
            trace['response'] = response
            result = validate_proposal(json.loads(response['content']))
            trace['status'] = 'completed'
            return result
        except (ValueError, KeyError, TypeError) as error:
            trace.update(status='failed', error=str(error))
            return proposal
        finally:
            with self.store._connection() as c:
                c.execute('UPDATE ai_requests SET body=? WHERE room_id=? AND request_id=?', (json.dumps(trace, ensure_ascii=False), room, repair_key))

    def prepare_narration(self, c, room: str, state: dict, actor: dict, key: str) -> dict | None:
        """Reserve a metered presentation request in the effects transaction."""
        entry = state['history'][-1]
        if not self.ai.config.get('session_narration', False) or state.get('active_combat') or (entry['action'] != 'question' and not entry.get('events')):
            return None
        if entry['action'] == 'question' and entry.get('voice') == 'gm' and not world_growth.enabled(state):
            return None
        if any(e.get('rule')=='report_adventure' for e in entry.get('events',[])):return None
        for event in entry.get('events', []):
            target = state['world']['entities'].get(event.get('target'), {})
            if event.get('rule') == 'aid' and target.get('kind') == 'pc':
                return None
            if event.get('rule') == 'give' and any(state['world']['entities'].get(effect.get('to'), {}).get('kind') == 'pc'
                                                   for effect in event.get('effects', [])):
                return None
        if entry['action'] == 'act' and (state.get('pending_check') or state.get('plan', {}).get('status') == 'blocked'):
            # The roll card already announces the attempt. Wait for the actual
            # outcome before playing acceptance/refusal or physical progress.
            return None
        now = time.time()
        used = c.execute("SELECT (SELECT COUNT(*) FROM ai_requests WHERE date(started,'unixepoch')=date(?,'unixepoch'))+COALESCE((SELECT count FROM ai_deleted_usage WHERE day=date(?,'unixepoch')),0)", (now, now)).fetchone()[0]
        if used >= self.ai.config['daily_request_limit']:
            return None
        try:
            actor,_=self.actor(c,room,actor['id'])
            request = story_narration.payload(state, actor, self.ai.config)
            if state.get('campaign'):
                data = json.loads(request['messages'][1]['content'])
                partner = (data.get('conversation_partner') or {}).get('id')
                data['recollections'] = campaign_storage.recall(c,state,state['history'][-1]['player'],actor['id'],partner) + data['recollections']
                request['messages'][1]['content'] = json.dumps(fit_context(data,self.ai.config.get('session_context_max_bytes',48000)),ensure_ascii=False)
        except ValueError:
            return None
        narration_key = str(uuid5(UUID(key), 'session.narration'))
        trace = {'request_id': narration_key, 'parent_request_id': key, 'role': 'session_narration',
                 'status': 'running', 'started': now, 'revision': state['revision'], 'request': request}
        c.execute('INSERT INTO ai_requests VALUES(?,?,?,?,?)',
                  (room, narration_key, json.dumps({'parent': key}), now, json.dumps(trace, ensure_ascii=False)))
        return trace

    def narrate(self, room: str, trace: dict, result: dict) -> dict:
        """Commit validated local seeds with prose; failures keep prior action effects."""
        started = time.monotonic()
        try:
            response = self.ai.client.complete(trace['request'], self.ai.config['timeout_seconds'])
            trace['response'] = response
            if self.ai.config.get('session_agent',False):
                with self.store._connection() as c:snapshot=self._load(c,room)
                if snapshot['revision'] != trace['revision']:
                    trace.update(status='stale',error='Сцена изменилась; описание не опубликовано.')
                    return result
                def validate_narration(candidate):
                    text=story_narration.reply(candidate)
                    story_narration.validate_trade_amounts(text,json.loads(trace['request']['messages'][1]['content']))
                    world_growth.validate_additions(snapshot,json.loads(candidate['content']).get('world_additions',[]),
                                                   snapshot['history'][-1]['character_id'],trace['request_id'])
                original_response=response
                response=SessionAgent(self,room,trace['request_id'],trace['revision']).run('narration',trace['request'],response,validate_narration)
                trace['verification_checked']=True
                if response is not original_response:trace['accepted_content']=response['content']
            text = story_narration.reply(response)
            story_narration.validate_trade_amounts(text,json.loads(trace['request']['messages'][1]['content']))
            with self.store._connection() as c:
                c.execute('BEGIN IMMEDIATE')
                state = self._load(c, room)
                if state['revision'] != trace['revision']:
                    trace.update(status='stale', error='Сцена изменилась; описание не опубликовано.')
                    return result
                additions = world_growth.apply(state,json.loads(response['content']).get('world_additions',[]),
                                               state['history'][-1]['character_id'],trace['request_id'])
                for place_id, place in additions['places'].items():
                    state['campaign']['scene_refs'][place_id] = campaign_storage.put(c,'scene',{'places':{place_id:place},'entities':{}})
                for hook_id, hook in additions['hooks'].items():
                    state['campaign'].setdefault('hook_refs',{})[hook_id] = campaign_storage.put(c,'story_hook',hook)
                text = story_narration.render_references(text,state['world'])
                source = state['history'][-1]
                if text == source['gm'].strip() and not any(additions.values()):
                    trace.update(status='completed', deduplicated=True)
                    return result
                entry = {k: deepcopy(source[k]) for k in ('character_id', 'actor_name', 'player_id', 'player_name', 'place_id', 'listeners') if k in source}
                entry.update(player='', gm=text, action='question' if source['action'] == 'question' else 'narration', player_request_id=trace['request_id'],
                             source_request_id=trace['parent_request_id'], chat_index=state['revision'] + 1)
                entry['voice'] = source.get('voice','npc') if source['action'] == 'question' else 'gm'
                entry['place_id'] = state['location']
                entry['listeners'] = [k for k, v in state['world']['entities'].items()
                                      if v['kind'] == 'npc' and location(state['world'], k) == state['location']]
                state['revision'] += 1
                state['history'] = (state['history'] + [entry])[-MAX_STORY_ENTRIES:]
                self.save(c, room, state)
                self.publish(c, room, state)
                result = self.view(state)
                stored_result = campaign_storage.freeze_result(c, result)
                c.execute('UPDATE adventure_requests SET body=? WHERE room_id=? AND request_id=?',
                          (json.dumps({'status': 'done', 'result': stored_result}, ensure_ascii=False), room, trace['parent_request_id']))
                parent = json.loads(c.execute('SELECT body FROM ai_requests WHERE room_id=? AND request_id=?',
                                             (room, trace['parent_request_id'])).fetchone()[0])
                parent.update(result=stored_result, narration_request_id=trace['request_id'])
                c.execute('UPDATE ai_requests SET body=? WHERE room_id=? AND request_id=?',
                          (json.dumps(parent, ensure_ascii=False), room, trace['parent_request_id']))
                trace.update(status='completed')
        except (ValueError, KeyError, TypeError) as error:
            trace.update(status='failed', error=str(error))
            if self.ai.config.get('session_agent',False):
                if isinstance(error,AgentRejected):trace['verification_issues']=error.issues
                with self.store._connection() as c:
                    self.event(c,room,str(uuid5(UUID(trace['request_id']),'verification.error')),'second',
                        'Не удалось согласовать описание с состоянием мира. Уже выполненные действия сохранены; можно продолжить или уточнить заявку.',{'ai_error':True})
        finally:
            trace['duration_ms'] = round((time.monotonic() - started) * 1000)
            with self.store._connection() as c:
                c.execute('UPDATE ai_requests SET body=? WHERE room_id=? AND request_id=?',
                          (json.dumps(trace, ensure_ascii=False), room, trace['request_id']))
        return result

    def adventure_control(self, room: str, command: dict, key: str) -> dict:
        try:UUID(key)
        except (ValueError,TypeError,AttributeError):raise ValueError('Нужен UUID операции.') from None
        if set(command)!={'action','revision','character_id'} or type(command['revision']) is not int or command['action'] not in ('start','finish','abandon'):
            raise ValueError('Нужны действие start/finish/abandon, revision и character_id.')
        encoded=json.dumps(command,sort_keys=True)
        with self.store._connection() as c:
            c.execute('BEGIN IMMEDIATE');state=self._load(c,room)
            self.sync_inventory(c,room,state);self.sync_combat(c,room,state);self.resolve_pending(c,room,state)
            prior=c.execute('SELECT command,body FROM adventure_requests WHERE room_id=? AND request_id=?',(room,key)).fetchone()
            if prior:
                if prior[0]!=encoded:raise MessageConflict('UUID уже использован.')
                body=json.loads(prior[1])
                if body['status']=='done':return campaign_storage.thaw_result(c,body['result'])
                raise ValueError('Этот запрос уже отправлен. Для новой попытки нужен новый запрос.')
            if not evening.enabled(state) or len(members(state))!=1:raise ValueError('Этот режим рассчитан на одного героя и одно короткое приключение.')
            if state['revision']!=command['revision']:raise MessageConflict('История изменилась. Обновите страницу.')
            actor,_=self.actor(c,room,command['character_id'])
            if actor['id']!=state['character_id']:raise PermissionError('Выберите героя этого сольного приключения.')
            if state.get('pending_check') or state.get('active_combat'):raise ValueError('Сначала завершите или отмените текущую проверку либо бой.')
            if any(json.loads(r[0]).get('status') in ('running','chosen') for r in c.execute('SELECT body FROM ai_requests WHERE started>?',(time.time()-120,))):
                raise MessageConflict('Дождитесь ответа мастера.')
            active=state['world'].get('adventure')
            if command['action']=='start':
                if active and active['status']!='completed':raise ValueError('Сначала завершите текущую историю; новая не заменит её автоматически.')
                if actor['sheet']['values']['hp_current']<=0:raise ValueError('Герой выбыл. Восстановите его перед новой историей.')
                if not self.ai.client.ready():raise ValueError('Ключ бесплатного ИИ не настроен.')
                c.execute('INSERT INTO adventure_requests VALUES(?,?,?,?)',(room,key,encoded,json.dumps({'status':'running'})))
            else:
                if not active or active['status']=='completed':raise ValueError('Нет текущей незавершённой истории.')
                if command['action']=='finish':
                    if len(evening.found(state['world']))!=2:raise ValueError('Пока найдены не оба свидетельства. Можно продолжить поиск или завершить без решения.')
                    if state['location']!=active['home']:
                        apply(state['world'],prepare(state['world'],actor,{'rule':'move','target':active['home']}))
                        state['location']=active['home']
                    event=apply(state['world'],prepare(state['world'],actor,{'rule':'report_adventure','target':active['patron_id']}))
                    text=event['text']
                else:
                    state['world']['time']+=2;text=evening.finish(state['world'],actor['id'],abandon=True)
                state['revision']+=1
                state['history']=(state['history']+[{'player':'Завершаю расследование.' if command['action']=='finish' else 'Отказываюсь от дальнейшего расследования.',
                    'gm':text,'action':'adventure_end','character_id':actor['id'],'actor_name':actor['name'],'player_request_id':key,'chat_index':state['revision']}])[-MAX_STORY_ENTRIES:]
                self.save(c,room,state);self.publish(c,room,state);result=self.view(state)
                c.execute('INSERT INTO adventure_requests VALUES(?,?,?,?)',(room,key,encoded,json.dumps({'status':'done','result':campaign_storage.freeze_result(c,result)},ensure_ascii=False)))
                return result
        request=evening.payload(state,self.ai.config)
        def review_outline(candidate):
            plan=evening.validate(candidate,state['world'])
            trial=deepcopy(state)
            identity='evening_'+uuid5(UUID(key),'episode').hex[:12]
            evening.start(trial,plan,identity)
            data={'heroes':[{'id':actor['id'],'name':actor['name']}],
                  'new_patron':{'name':plan['patron']['name'],'role':plan['patron']['role']},
                  'new_places':{key:{'name':place['name'],'description':place['description']}
                                for key,place in trial['world']['places'].items() if place.get('adventure_id')==identity},
                  'private_truth':plan['truth'],'bounds':state['campaign']['bounds']}
            return data,{'opening':plan['opening'],'npc_request':plan['patron']['request']}
        try:
            for attempt in range(1 if self.ai.config.get('session_agent') else 2):
                trace_key=str(uuid5(UUID(key),'evening.outline:'+str(attempt)));now=time.time()
                trace={'status':'running','role':'adventure_outline','started':now,'request':deepcopy(request),'parent_request_id':key}
                with self.store._connection() as c:
                    c.execute('BEGIN IMMEDIATE')
                    if self._load(c,room)['revision']!=state['revision']:raise MessageConflict('Мир изменился во время подготовки истории.')
                    used=c.execute("SELECT (SELECT COUNT(*) FROM ai_requests WHERE date(started,'unixepoch')=date(?,'unixepoch'))+COALESCE((SELECT count FROM ai_deleted_usage WHERE day=date(?,'unixepoch')),0)",(now,now)).fetchone()[0]
                    if used>=self.ai.config['daily_request_limit']:raise ValueError('Достигнут дневной лимит ИИ.')
                    c.execute('INSERT INTO ai_requests VALUES(?,?,?,?,?)',(room,trace_key,json.dumps(command),now,json.dumps(trace,ensure_ascii=False)))
                response=None;repair=None;started=time.monotonic()
                try:
                    response=self.ai.client.complete(request,self.ai.config['timeout_seconds']);trace['response']=response
                    if self.ai.config.get('session_agent'):
                        original_response=response
                        response=SessionAgent(self,room,trace_key,state['revision']).run('outline',request,response,
                            lambda candidate:evening.validate(json.loads(candidate['content']),state['world']),
                            review_projection=review_outline)
                        trace['verification_checked']=True
                        if response is not original_response:trace['accepted_content']=response['content']
                    plan=evening.validate(json.loads(response['content']),state['world']);trace['status']='completed'
                except (ValueError,TypeError,KeyError) as error:
                    trace.update(status='failed',error=str(error))
                    if isinstance(error,AgentRejected):trace['verification_issues']=error.issues
                    if isinstance(error,MessageConflict):raise
                    if response is not None and attempt==0 and not self.ai.config.get('session_agent'):repair=str(error)
                    else:raise ValueError('Не удалось подготовить историю: '+str(error)) from None
                finally:
                    trace['duration_ms']=round((time.monotonic()-started)*1000)
                    with self.store._connection() as c:c.execute('UPDATE ai_requests SET body=? WHERE room_id=? AND request_id=?',(json.dumps(trace,ensure_ascii=False),room,trace_key))
                if repair is None:break
                request['messages'].extend([{'role':'assistant','content':response.get('content','')},{'role':'user','content':'План отклонён: '+repair+' Исправь и верни весь JSON. Мир ещё не изменён.'}])
            with self.store._connection() as c:
                c.execute('BEGIN IMMEDIATE');current=self._load(c,room)
                if current['revision']!=state['revision']:raise MessageConflict('История изменилась; новый план не применён.')
                self.sync_inventory(c,room,current);evening.validate(plan,current['world'])
                identity='evening_'+uuid5(UUID(key),'episode').hex[:12]
                text=evening.start(current,plan,identity)
                current['world']['adventure']['outline_ref']=campaign_storage.put(c,'evening_outline',plan)
                current['revision']+=1
                current['history']=(current['history']+[{'player':'Начинаю новое приключение на один вечер.','gm':text,'action':'adventure_start',
                    'character_id':actor['id'],'actor_name':actor['name'],'player_request_id':key,'chat_index':current['revision']}])[-MAX_STORY_ENTRIES:]
                self.save(c,room,current);self.publish(c,room,current);result=self.view(current)
                c.execute('UPDATE adventure_requests SET body=? WHERE room_id=? AND request_id=?',(json.dumps({'status':'done','result':campaign_storage.freeze_result(c,result)},ensure_ascii=False),room,key))
                return result
        except (ValueError,KeyError,TypeError) as error:
            with self.store._connection() as c:c.execute('UPDATE adventure_requests SET body=? WHERE room_id=? AND request_id=?',(json.dumps({'status':'failed','error':str(error)},ensure_ascii=False),room,key))
            raise

    def materialize_campaign(self, room: str, key: str, state: dict, proposal: dict, text: str, actor_id: str) -> tuple[dict, dict, list[tuple[str, dict]]]:
        if evening.enabled(state) and (not state['world'].get('adventure') or state['world']['adventure']['status']=='completed'):
            return state,proposal,[]
        if not state.get('campaign') or state.get('pending_check'):
            return state, proposal, []
        trial, proposal, needed = campaign.creation_plan(state, proposal)
        additions = []
        for place_id in needed:
            request = campaign.generation_payload(trial,place_id,text,self.ai.config)
            for attempt in range(1 if self.ai.config.get('session_agent') else 2):
                generation_key = str(uuid5(UUID(key),'campaign.scene:'+place_id+':'+str(attempt)))
                now = time.time()
                trace = {'request_id':generation_key,'parent_request_id':key,'role':'campaign_generation',
                         'status':'running','started':now,'request':deepcopy(request),'place_id':place_id,'attempt':attempt}
                with self.store._connection() as c:
                    c.execute('BEGIN IMMEDIATE')
                    if self._load(c,room)['revision'] != state['revision']:raise MessageConflict('Мир изменился во время подготовки места.')
                    used = c.execute("SELECT (SELECT COUNT(*) FROM ai_requests WHERE date(started,'unixepoch')=date(?,'unixepoch'))+COALESCE((SELECT count FROM ai_deleted_usage WHERE day=date(?,'unixepoch')),0)",(now,now)).fetchone()[0]
                    if used >= self.ai.config['daily_request_limit']:raise ValueError('Достигнут дневной лимит ИИ; новое место не создано.')
                    c.execute('INSERT INTO ai_requests VALUES(?,?,?,?,?)',(room,generation_key,json.dumps({'character_id':actor_id}),now,json.dumps(trace,ensure_ascii=False)))
                started = time.monotonic(); response = None; repair = None
                try:
                    response = self.ai.client.complete(request,self.ai.config['timeout_seconds'])
                    trace['response'] = response
                    if self.ai.config.get('session_agent'):
                        original_response=response
                        response=SessionAgent(self,room,generation_key,state['revision']).run('scene',request,response,
                            lambda candidate:campaign.scene(trial['world'],place_id,json.loads(candidate['content']),require_discoveries=True))
                        trace['verification_checked']=True
                        if response is not original_response:trace['accepted_content']=response['content']
                    addition = campaign.scene(trial['world'],place_id,json.loads(response['content']),require_discoveries=True)
                    trial['world']['places'].update(addition['places']);trial['world']['entities'].update(addition['entities'])
                    additions.append((place_id,addition));trace['status'] = 'completed'
                except (ValueError,KeyError,TypeError) as error:
                    trace.update(status='failed',error=str(error))
                    if isinstance(error,AgentRejected):trace['verification_issues']=error.issues
                    if isinstance(error,MessageConflict):raise
                    if response is not None and attempt == 0 and not self.ai.config.get('session_agent'):
                        repair = str(error)
                    else:
                        raise ValueError('Не удалось создать новое место: '+str(error)) from None
                finally:
                    trace['duration_ms'] = round((time.monotonic()-started)*1000)
                    with self.store._connection() as c:
                        c.execute('UPDATE ai_requests SET body=? WHERE room_id=? AND request_id=?',(json.dumps(trace,ensure_ascii=False),room,generation_key))
                if repair is None:break
                request['messages'].extend([{'role':'assistant','content':response.get('content','')},
                    {'role':'user','content':'Сцена отклонена проверкой: '+repair+' Исправь её и верни весь JSON по прежней схеме. Мир ещё не изменён.'}])
        return trial, proposal, additions

    def execute(self, room: str, command: dict, key: str, *, reaction: bool = False) -> dict:
        try: UUID(key)
        except (ValueError, TypeError, AttributeError): raise ValueError('Нужен UUID запроса.') from None
        if set(command) - {'revision', 'text', 'character_id'} or type(command.get('revision')) is not int:
            raise ValueError('Нужны revision, text и character_id.')
        text = command.get('text')
        if not isinstance(text, str) or not text.strip() or len(text) > 1000: raise ValueError('Сообщение: от 1 до 1000 символов.')
        encoded = json.dumps({k: v for k, v in command.items() if not reaction or k != 'revision'}, sort_keys=True)
        with self.store._connection() as c:
            c.execute('BEGIN IMMEDIATE'); state = self._load(c, room)
            self.sync_inventory(c, room, state); self.sync_combat(c, room, state); self.resolve_pending(c, room, state)
            prior = c.execute('SELECT command,body FROM adventure_requests WHERE room_id=? AND request_id=?', (room, key)).fetchone()
            if prior:
                if prior[0] != encoded: raise MessageConflict('UUID уже использован.')
                saved = json.loads(prior[1])
                if saved['status'] == 'done': return campaign_storage.thaw_result(c,saved['result'])
                raise ValueError('Запрос уже отправлен. Обновите историю; автоматического повтора нет.')
            if state.get('active_combat'): raise ValueError('Идёт бой. Откройте боевой экран.')
            if state['revision'] != command['revision']: raise MessageConflict('История изменилась. Обновите страницу.')
            actor, roster = self.actor(c, room, command.get('character_id'))
            if members(state) and actor['id'] not in members(state):raise ValueError('Этот герой не входит в группу сеанса.')
            hp=state.get('hero_hps',{}).get(actor['id'],state.get('hero_hp') if actor['id']==state['character_id'] else actor['sheet']['values']['hp_current'])
            if hp==0:raise ValueError('Этот герой выбыл. Другие участники группы могут продолжать.')
            initial = deepcopy(state)
            initialize(state, actor)
            if not self.ai.client.ready(): raise ValueError('Ключ бесплатного ИИ не настроен.')
            now = time.time()
            count = c.execute("SELECT (SELECT COUNT(*) FROM ai_requests WHERE date(started,'unixepoch')=date(?,'unixepoch'))+COALESCE((SELECT count FROM ai_deleted_usage WHERE day=date(?,'unixepoch')),0)", (now, now)).fetchone()[0]
            if count >= self.ai.config['daily_request_limit']: raise ValueError('Достигнут дневной лимит ИИ.')
            if any(json.loads(r[0]).get('status') in ('running', 'chosen') for r in c.execute('SELECT body FROM ai_requests WHERE started>?', (now - 120,))):
                raise MessageConflict('Другой запрос ИИ ещё выполняется.')
            if c.execute('SELECT 1 FROM ai_requests WHERE room_id=? AND request_id=?', (room, key)).fetchone(): raise MessageConflict('UUID уже использован другим запросом ИИ.')
            prompt_context = context(state, actor, text, reaction)
            old = campaign_storage.recall(c,state,text,actor['id'])
            prompt_context['recollections'] = old + prompt_context['recollections']
            recent = c.execute("""SELECT envelope FROM queue_events WHERE room_id=?
                AND json_extract(envelope,'$.type')='chat.message'
                AND json_extract(envelope,'$.metadata.session_revision') IS NULL
                AND json_extract(envelope,'$.metadata.client_request_id') IS NULL
                AND json_extract(envelope,'$.metadata.ai_error') IS NULL
                ORDER BY id DESC LIMIT 4""", (room,)).fetchall()
            prompt_context['recent_chat'] = [{'speaker': e['sender'], 'text': e['text'][:400]} for e in (json.loads(r[0]) for r in reversed(recent))]
            budget = self.ai.config.get('session_context_max_bytes', self.ai.config['context_max_bytes'])
            prompt_context = fit_context(prompt_context, budget)
            if reaction:
                prompt_context = {k: v for k, v in prompt_context.items() if k in (
                    'hero', 'hero_id', 'shared_party', 'conversation_partner', 'gm_knowledge', 'observations',
                    'history', 'recollections', 'pending_check', 'location_id', 'phase', 'confirmed_results', 'last_check_resolution', 'trade', 'investigation', 'adventure', 'player')}
                prompt_context = fit_context(story_narration.narrative_scope(prompt_context, state['world']), budget)
            payload = {'model': self.ai.config.get('session_model', self.ai.config['model']), 'messages': [{'role': 'system', 'content': story_narration.prompt_for(prompt_context, reaction=True) if reaction else PLANNER_PROMPT}, {'role': 'user', 'content': json.dumps(prompt_context, ensure_ascii=False)}],
                       'max_tokens': 4096 if self.ai.config.get('session_reasoning') else 1800, 'temperature': 0.2,
                       'reasoning': session_reasoning(self.ai.config),
                       'provider': {'max_price': {'prompt': 0, 'completion': 0}},
                       'response_format': {'type': 'json_schema', 'json_schema': {'name': 'action_plan', 'strict': True, 'schema': schema(state, text,actor['id'])}}}
            trace = {'status': 'running', 'role': 'session_gm', 'started': now, 'request': payload}
            if not reaction: self.event(c, room, str(uuid5(UUID(key), 'session.player')), 'first', text, {'client_request_id': key,'character_id':actor['id'],'actor_name':actor['name'],'player_id':actor.get('player_id'),'player_name':next((p['name'] for p in roster['players'] if p['id']==actor.get('player_id')),None)})
            self.save(c, room, initial)
            c.execute('INSERT INTO adventure_requests VALUES(?,?,?,?)', (room, key, encoded, json.dumps(trace, ensure_ascii=False)))
            c.execute('INSERT INTO ai_requests VALUES(?,?,?,?,?)', (room, key, encoded, now, json.dumps(trace, ensure_ascii=False)))
        provider_started = time.monotonic()
        try:
            response = self.ai.client.complete(payload, self.ai.config['timeout_seconds']); trace['response'] = response
            trace['duration_ms'] = round((time.monotonic() - provider_started) * 1000)
            if self.ai.config.get('session_agent',False):
                original_response=response
                response=SessionAgent(self,room,key,state['revision']).run('narration' if reaction else 'plan',payload,response,
                                lambda candidate:validate_proposal(json.loads(candidate['content'])))
                trace['verification_checked']=True
                if response is not original_response:trace['accepted_content']=response['content']
            proposal = validate_proposal(json.loads(response['content']))
            if not reaction and not self.ai.config.get('session_agent',False) and repeats_previous_reply(proposal, state['history'], text):
                proposal = self.repair_repetition(room, key, payload, proposal)
                trace['semantic_repair_checked'] = True
            if reaction and (proposal['intent'] != 'question' or proposal['steps']): raise ValueError('Реакция не должна назначать новое действие.')
            trial, proposal, additions = self.materialize_campaign(room,key,state,proposal,text,actor['id'])
            with self.store._connection() as c:
                c.execute('BEGIN IMMEDIATE'); current = self._load(c, room)
                if current['revision'] != state['revision']: raise MessageConflict('Сессия изменилась; ответ не применён.')
                # Reload actual inventory after the network boundary, not the stale prompt snapshot.
                self.sync_inventory(c, room, current); state = current
                actor, roster = self.actor(c, room, command.get('character_id'))
                initialize(state, actor)
                self.sync_inventory(c, room, state)
                if additions:
                    campaign.merge(state,trial,additions)
                    for place_id, addition in additions:
                        state['campaign']['scene_refs'][place_id] = campaign_storage.put(c,'scene',addition)
                interlocutor = proposal.get('interlocutor')
                if interlocutor:
                    npc = state['world']['entities'].get(interlocutor, {})
                    if npc.get('kind') != 'npc' or location(state['world'], interlocutor) != state['location'] or npc.get('status', 'standing') not in ('standing', 'surrendered'):
                        if (proposal['intent']=='question' and npc.get('kind')=='npc'
                                and location(state['world'],interlocutor)==state['location']
                                and npc.get('status')=='defeated'):
                            values=actor['sheet']['values']
                            proposal.update(voice='gm',reply=f"{npc['name']} выбыл из боя и сейчас не может отвечать. Ваши ОЗ: {values['hp_current']}/{values['hp_max']}.")
                            state.pop('conversation_with',None)
                        else:
                            raise ValueError('Выбранный собеседник не находится рядом.')
                    else:
                        state['conversation_with'] = interlocutor
                proposal['reply'] = story_narration.render_references(proposal['reply'],state['world'])
                entry = {'place_id': state['location'], 'listeners': [k for k, v in state['world']['entities'].items() if v['kind'] == 'npc' and location(state['world'], k) == state['location']], 'player': '' if reaction else text, 'gm': proposal['reply'], 'action': 'reaction' if reaction else proposal['intent'], 'player_request_id': key,'character_id':actor['id'],'actor_name':actor['name'],'player_id':actor.get('player_id'),'player_name':next((p['name'] for p in roster['players'] if p['id']==actor.get('player_id')),None)}
                entry['voice'] = proposal.get('voice', 'npc')
                if proposal.get('world_request') and proposal['intent']=='question':entry['world_request']=True
                if entry.get('world_request') and world_growth.enabled(state):
                    entry['gm']='Вы расспрашиваете о новой зацепке поблизости.'
                deferred_reply = (self.ai.config.get('session_agent') and self.ai.config.get('session_narration')
                        and proposal['intent']=='question' and (entry['voice']=='npc' or world_growth.enabled(state)))
                if deferred_reply:
                    entry['gm']=''
                if proposal['intent'] == 'cancel':
                    pending = state.get('pending_check')
                    if pending and pending.get('character_id', state['character_id']) != actor['id']:
                        entry.update(action='clarify', voice='gm',
                                     gm='Эту проверку может отменить только её автор. Попытка товарища по-прежнему ожидает его броска.')
                    else:
                        if pending: entry['cancelled_offer_id'] = pending['offer_id']
                        entry['gm'] = self.cancel_pending(c, room, state, announce=False)
                elif proposal['intent'] == 'act':
                    if evening.enabled(state) and (not state['world'].get('adventure') or state['world']['adventure']['status']=='completed'):
                        entry['gm']='Эта история завершена. Можно начать новое приключение.' if state['world'].get('adventure') else 'Начните приключение кнопкой «Сгенерировать историю».'
                    elif state.get('pending_check'):
                        entry['gm'] = 'Есть ожидающая проверка. Можно уточнить её, бросить кости или отменить попытку.'
                    elif not proposal['steps']:
                        entry['gm'] = 'Уточните цель и способ действия.'
                    else:
                        state['plan'] = {'id': key, 'steps': proposal['steps'], 'index': 0, 'status': 'running', 'original_text': text, 'receipts': [], 'character_id':actor['id']}
                        events = self.run_plan(c, room, state)
                        entry['gm'] = '\n'.join(e['text'] for e in events)
                        entry['events'] = events
                        if state.get('active_combat'): entry['combat_id'] = state['active_combat']['id']
                elif not entry['gm'] and not deferred_reply:
                    entry['gm'] = 'Что вы хотите узнать или попробовать?'
                state['revision'] += 1
                entry['chat_index'] = state['revision']
                state['history'] = (state['history'] + [entry])[-MAX_STORY_ENTRIES:]
                self.publish(c, room, state); self.save(c, room, state)
                result = self.view(state)
                stored_result = campaign_storage.freeze_result(c, result)
                trace.update(status='completed', result=stored_result)
                c.execute('UPDATE adventure_requests SET body=? WHERE room_id=? AND request_id=?', (json.dumps({'status': 'done', 'result': stored_result}, ensure_ascii=False), room, key))
                c.execute('UPDATE ai_requests SET body=? WHERE room_id=? AND request_id=?', (json.dumps(trace, ensure_ascii=False), room, key))
                narration = self.prepare_narration(c, room, state, actor, key) if not reaction else None
            return self.narrate(room, narration, result) if narration else result
        except (ValueError, KeyError, TypeError) as error:
            trace.setdefault('duration_ms', round((time.monotonic() - provider_started) * 1000))
            trace.update(status='failed', error='Ответ не применён: ' + str(error))
            if isinstance(error,AgentRejected):trace['verification_issues']=error.issues
            with self.store._connection() as c:
                for table in ('adventure_requests', 'ai_requests'):
                    c.execute(f'UPDATE {table} SET body=? WHERE room_id=? AND request_id=?', (json.dumps(trace, ensure_ascii=False), room, key))
                self.event(c, room, str(uuid5(UUID(key), 'session.error')), 'second', 'Мастер не смог разобрать заявку. Ничего не исполнено; уточните цель и способ.', {'ai_error': True})
            if isinstance(error, MessageConflict):raise MessageConflict(trace['error']) from None
            raise ValueError(trace['error']) from None
