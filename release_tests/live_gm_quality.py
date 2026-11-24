"""Opt-in matched GM probes; default captures production contexts without network.

Synthetic dialogue fixtures, isolated database, zero-price providers only. Reports
contain exact requests and responses. No retries, no player saves, no paid routes.
Scores are deliberately left to human review: JSON validity is not GM quality.
"""
import argparse
from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
import time
from typing import Callable
from uuid import uuid4

from iirpg.action_rules import validate_proposal
from iirpg.application import ChatService
from iirpg.dice import DiceTool
from iirpg.server import ChatHTTPServer


class CaptureClient:
    def ready(self) -> bool:
        return True

    def complete(self, payload: dict, timeout: int) -> dict:
        self.payload = deepcopy(payload)
        return {'content': json.dumps({'intent': 'question', 'reply': 'Ответ заглушки.', 'steps': []})}


def fixtures() -> list[dict]:
    introduction = {'player': 'Меня зовут Мирон. Мой брат утонул, когда я погнался за наградой. С тех пор помогаю бесплатно.',
                    'gm': 'Хозяйка негромко повторяет ваше имя и обещает не предлагать денег.', 'action': 'question'}
    filler = [{'player': f'Осматриваю зал глазами, ничего не трогаю. Уточнение {n}.',
               'gm': 'Столы, лестница и дверь во двор остаются на прежних местах.', 'action': 'question'} for n in range(16)]
    return [
        {'id': 'recent_memory', 'history': [introduction],
         'text': 'Как меня зовут и почему я не хочу брать плату? Только отвечаю хозяйке, ничего не делаю.',
         'review': 'Помнит Мирона и смерть брата; не выдумывает иную мотивацию.'},
        {'id': 'distant_memory', 'history': [introduction, *filler],
         'text': 'Помнишь, как меня зовут и почему я не беру плату? Только разговор с хозяйкой.',
         'review': 'Те же факты после 16 промежуточных обменов. Признание отсутствия памяти честнее выдумки.'},
        {'id': 'long_reply_memory', 'history': [
            {'player': 'Что конюх просил передать тебе вчера?',
             'gm': 'Хозяйка долго описывает повседневную работу. ' * 18 + 'Затем она дословно передаёт условную фразу конюха: «Семь тихих колоколов».', 'action': 'question'}],
         'text': 'Повтори дословно условную фразу конюха, которую ты только что передала. Только разговор.',
         'review': 'Фраза «Семь тихих колоколов» находится после 500-го символа реплики.'},
        {'id': 'social_repair', 'place': 'yard', 'history': [
            {'player': 'Помоги, иначе пожалеешь.', 'gm': 'Конюх отказывается и отступает.', 'action': 'check_result',
             'rule': 'coerce', 'target': 'stablehand'},
            {'player': 'Что ты видел?', 'gm': 'Я видел, как книгу спрятали в кладовой.', 'action': 'question'}],
         'text': 'Извини, я перегнул. Я хочу помочь хозяйке. Спокойно представляюсь и пытаюсь расположить тебя к себе.',
         'review': 'make_impression к stablehand, без смены адресата на hostess и без повторения отказа.'},
        {'id': 'request_without_travel', 'place': 'yard', 'history': [],
         'text': 'Прошу помочь мне открыть сундук в кладовой.',
         'review': 'request(stablehand, help). Место будущей задачи не разрешает move.'},
        {'id': 'explicit_travel_threat', 'history': [],
         'text': 'Иду во двор и угрожаю конюху: помоги открыть сундук, иначе пожалеешь. Хочу принудить его.',
         'review': 'move(yard), coerce(stablehand, help). Адресат назван явно; не подменять его хозяйкой.'},
        {'id': 'unimplemented_creativity', 'place': 'store', 'history': [],
         'text': 'Я не хочу ломать сундук: обвязываю крышку верёвкой, перекидываю её через стеллаж и тяну, используя его как блок. Пробую так открыть сундук.',
         'review': 'Разумно сопоставить force_open либо объяснить предел. Нельзя объявить сундук открытым до проверки.'},
        {'id': 'unknown_culprit', 'history': [],
         'text': 'Хозяйка, назови имя того, кто спрятал книгу. Если ты этого не видела и не знаешь, так и скажи.',
         'review': 'Не придумывает виновника. У хозяйки нет такого знания.'},
        {'id': 'compound_action', 'history': [],
         'text': 'Убираю оружие и щит, иду в кладовую, беру лом в руку, вскрываю сундук ломом и забираю книгу.',
         'review': 'Все приготовления, переход, hold(crowbar), force_open(chest), take(book), без преждевременного результата.'},
        {'id': 'npc_character', 'place': 'yard', 'history': [],
         'text': 'Я опускаюсь на ступеньку рядом с конюхом: «Ты всё время ждёшь обвинения. Что ты боишься потерять? Я пока ничего от тебя не прошу».',
         'review': 'Живой ответ на страх, без придуманной преступной биографии, броска и автоматического согласия помочь.'},
    ]


def enrichment(case: dict) -> dict:
    """Hand-authored experimental dossier; not a claimed automatic memory system."""
    notes = []
    for entry in case['history']:
        if 'Мой брат утонул' in entry['player']:
            notes.append({'source': 'fixture.player', 'fact': entry['player']})
        if 'Семь тихих колоколов' in entry['gm']:
            notes.append({'source': 'fixture.gm', 'fact': 'Хозяйка передала фразу конюха: «Семь тихих колоколов».'})
    return {
        'episodic_notes': notes,
        'authored_npc_profiles': {
            'hostess': {'voice': 'Коротко, по делу; тепло проявляет поступками.',
                        'priority': 'Вернуть книгу, не обвиняя постояльцев без доказательств.'},
            'stablehand': {'voice': 'Говорит осторожно, о работе охотнее, чем о себе.',
                           'stakes': 'Боится потерять место и доверие хозяйки, а не разоблачения в краже.',
                           'boundary': 'Осторожность не означает вину; не выдумывать прошлого преступления.'}},
        'narrative_guidance': 'Отвечай из ситуации и характера собеседника. Можно делать описательные детали, не меняющие состояние. Факты эпизодических заметок имеют указанный источник. Новые механические последствия по-прежнему только через steps.'}


def journey(server: ChatHTTPServer, report: dict, save: Callable[[], None]) -> None:
    """A short organic conversation, including real post-roll narration."""
    hero = server.games.heroes.list()['heroes'][0]['id']
    room = server.games.execute({'action': 'create', 'scenario': 'missing-book', 'hero_id': hero,
                                 'name': 'Organic GM conversation'}, str(uuid4()))['room_id']
    actor = server.adventure.read(room)['character_id']
    report['room_id'] = room
    report['turns'] = []

    def record(kind, text, state):
        report['turns'].append({'kind': kind, 'player': text, 'entry': state['history'][-1],
                                'location': state['location'], 'pending': state.get('pending_check'),
                                'plan': state.get('plan')})
        report['traces'] = server.ai.traces(room)
        report['attempts'] = server.ai.status()['requests_today']
        save()
        print(json.dumps({'kind': kind, 'text': text, 'gm': state['history'][-1]['gm']}, ensure_ascii=False), flush=True)

    def say(text):
        state = server.adventure.read(room)
        try:
            state = server.adventure.execute(room, {'revision': state['revision'], 'character_id': actor,
                                                     'text': text}, str(uuid4()))
            record('player', text, state)
            return state
        except ValueError as error:
            report['turns'].append({'kind': 'error', 'player': text, 'error': str(error)})
            report['traces'] = server.ai.traces(room)
            report['attempts'] = server.ai.status()['requests_today']
            save()
            return server.adventure.read(room)

    say('Здравствуйте. Зови меня Кир. Я ищу сестру, она пропала в дороге. Я помогу с книгой, но денег не возьму. Что эта книга значит лично для тебя?')
    say('Если я её найду, ты позволишь посмотреть, не записывалась ли здесь моя сестра? Я пока просто спрашиваю.')
    say('Спасибо. Я выхожу во двор.')
    say('Подхожу к конюху: «Не бойся, я никого не обвиняю. Меня зовут Кир. Что ты видел в тот вечер?»')
    say('Прошу помочь мне открыть сундук в кладовой.')
    # Continue the attempt after a wrong interpretation, as a player would.
    state = say('Я пока никуда не зову тебя идти. Если я ушёл, возвращаюсь во двор к конюху. Спокойно знакомлюсь и стараюсь расположить его к себе.')
    pending = state.get('pending_check')
    if pending:
        ChatService(server.service.store, DiceTool(roll_die=lambda sides: 20)).send(room, {
            'sender': 'first', 'type': 'tool.call', 'tool': 'dice.roll',
            'request_id': pending['roll_request_id'], 'offer_id': pending['offer_id']})
        state = server.adventure.read(room)
        record('controlled_d20_20', '', state)
        try:
            state = server.adventure.react(room, pending['offer_id'])
            record('after_roll', '', state)
        except ValueError as error:
            report['turns'].append({'kind': 'reaction_error', 'error': str(error)})
            save()
    say('Возвращаюсь к хозяйке в зал.')
    say('Ты помнишь, как я представился, кого ищу и почему не хочу брать у тебя деньги? Только разговор.')


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live', action='store_true')
    parser.add_argument('--model', help='Override the session model with an explicit free model.')
    parser.add_argument('--samples', type=int, default=1, help='Independent samples per case and variant, without retries.')
    parser.add_argument('--no-narration', action='store_true', help='Disable post-action prose for a planner-only comparison.')
    parser.add_argument('--reasoning', action='store_true', help='Enable internal reasoning for endpoints requiring it.')
    parser.add_argument('--reasoning-tokens', type=int, help='Optional supported reasoning budget, 128–2048.')
    parser.add_argument('--journey', action='store_true', help='Organic end-to-end conversation; requires --live.')
    parser.add_argument('--alternate-model', default='inclusionai/ling-3.2-flash:free')
    parser.add_argument('--variants', nargs='+', choices=['baseline', 'history', 'grounded', 'alternate', 'dialogue'],
                        default=['baseline', 'history', 'grounded', 'alternate'])
    parser.add_argument('--case', nargs='+', choices=[f['id'] for f in fixtures()])
    parser.add_argument('--max-requests', type=int, default=40)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    if args.journey and not args.live:
        parser.error('--journey requires --live.')
    dialogue_cases = {'recent_memory', 'distant_memory', 'long_reply_memory', 'unknown_culprit', 'npc_character'}
    if 'dialogue' in args.variants and (not args.case or set(args.case) - dialogue_cases):
        parser.error('The dialogue ablation requires explicit conversation-only --case values.')
    if not args.alternate_model.endswith(':free'):
        parser.error('Only explicit :free alternative models are allowed.')
    if args.model and not (args.model.endswith(':free') or args.model == 'openrouter/free'):
        parser.error('--model must be free.')
    if not 1 <= args.samples <= 5:
        parser.error('--samples must be between 1 and 5.')
    if args.reasoning_tokens is not None and not 128 <= args.reasoning_tokens <= 2048:
        parser.error('--reasoning-tokens must be between 128 and 2048.')
    if not 1 <= args.max_requests <= 64:
        parser.error('--max-requests must be between 1 and 64.')
    run_id = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + uuid4().hex[:8]
    output = args.output or Path('.local') / ('gm-quality-' + run_id + '.json')
    output.parent.mkdir(parents=True, exist_ok=True)
    database = output.with_suffix('.sqlite3')
    if output.exists() or database.exists():
        parser.error('Output and database must be new; existing reports are never overwritten.')
    server = ChatHTTPServer(('127.0.0.1', 0), database)
    client = server.ai.client
    if args.model:
        server.ai.config['session_model'] = args.model
    if args.reasoning or args.reasoning_tokens is not None:
        server.ai.config['session_reasoning'] = True
    if args.reasoning_tokens is not None:
        server.ai.config['session_reasoning_tokens'] = args.reasoning_tokens
    narration = server.ai.config.get('session_narration', False) and not args.no_narration
    server.ai.config['session_narration'] = False
    capture = CaptureClient()
    server.ai.client = capture
    report = {'created_at': run_id, 'live': args.live, 'database': str(database),
              'primary_model': server.ai.config.get('session_model', server.ai.config['model']), 'alternate_model': args.alternate_model,
              'limitations': ['Synthetic histories, not organic long campaigns.',
                              'Independent stochastic samples; no causal claim about model size.',
                              'Grounded adds authored facts and manually retrieved notes.',
                              'History variant restores full fixture dialogue up to 32000 bytes.',
                              'Proposals are inspected, not applied; end-to-end validation is separate.'],
              'calls': [], 'captures': [], 'attempts': 0}

    def save() -> None:
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2))

    # Check the actual presentation boundary without a provider: even a valid
    # descriptive announcement is replaced by mechanical event text on act.
    class ActionCapture(CaptureClient):
        def complete(self, payload: dict, timeout: int) -> dict:
            self.payload = deepcopy(payload)
            return {'content': json.dumps({'intent': 'act', 'reply': 'Вы направляетесь к двери под стук дождя по навесу.',
                                          'steps': [{'rule': 'move', 'target': 'yard', 'method': 'walk'}]})}

    try:
        if args.journey:
            server.ai.client = client
            server.ai.config['session_narration'] = narration
            server.ai.config['daily_request_limit'] = args.max_requests
            journey(server, report, save)
            print(str(output), flush=True)
            return
        for case in fixtures():
            if args.case and case['id'] not in args.case:
                continue
            hero = server.games.heroes.list()['heroes'][0]['id']
            room = server.games.execute({'action': 'create', 'scenario': 'missing-book',
                                         'hero_id': hero, 'name': 'Synthetic GM quality fixture'}, str(uuid4()))['room_id']
            state = server.adventure.read(room)
            actor = state['character_id']
            with server.service.store._connection() as c:
                state = server.adventure._load(c, room)
                state['history'] = deepcopy(case['history'])
                state['revision'] = len(state['history'])
                state['location'] = case.get('place', 'hall')
                state['world']['entities'][actor]['place'] = state['location']
                # Publish synthetic history through the ordinary request-ID path.
                for entry in state['history']:
                    entry['player_request_id'] = str(uuid4())
                    entry['character_id'] = actor
                if state['location'] == 'yard':
                    state['conversation_with'] = 'stablehand'
                server.adventure.save(c, room, state)
            result = server.adventure.execute(room, {'revision': state['revision'], 'character_id': actor,
                                                     'text': case['text']}, str(uuid4()))
            baseline = deepcopy(capture.payload)
            ctx = json.loads(baseline['messages'][1]['content'])
            report['captures'].append({'case': case['id'], 'history_in_fixture': len(case['history']),
                                       'history_sent': len(ctx['history']),
                                       'context_bytes': len(baseline['messages'][1]['content'].encode()),
                                       'stub_reply_visible': result['history'][-1]['gm']})
            for sample, variant in ((n, v) for n in range(args.samples) for v in args.variants):
                payload = deepcopy(baseline)
                data = json.loads(payload['messages'][1]['content'])
                if variant == 'history':
                    data['history'] = deepcopy(case['history'])
                    while len(json.dumps(data, ensure_ascii=False).encode()) > 32000 and len(data['history']) > 1:
                        data['history'].pop(0)
                if variant == 'grounded':
                    data['gm_quality_experiment'] = enrichment(case)
                if variant == 'alternate':
                    payload['model'] = args.alternate_model
                if variant == 'dialogue':
                    # Isolate conversation from action planning. This changes both
                    # prompt and input selection, not model or canonical NPC facts.
                    data = {k: v for k, v in data.items() if k in (
                        'player', 'conversation_partner', 'hero', 'hero_id', 'location_id',
                        'gm_knowledge', 'history', 'observations')}
                    payload['messages'][0]['content'] = (
                        'Ты ведущий настольной ролевой игры, отвечаешь на русском от лица NPC '
                        'conversation_partner. hero — персонаж игрока, не ты. '
                        'Отвечай на последнюю реплику player, учитывая историю, знания и цели NPC. '
                        'Не придумывай неизвестных NPC фактов и не управляй героем. '
                        'Это только разговор: никаких бросков или изменений механики. '
                        'Верни JSON {"intent":"question","reply":"ответ NPC до 5 предложений","steps":[]}.'
                    )
                payload['messages'][1]['content'] = json.dumps(data, ensure_ascii=False)
                row = {'case': case['id'], 'variant': variant, 'sample': sample, 'review_criterion': case['review'],
                       'context_bytes': len(payload['messages'][1]['content'].encode()),
                       'request': payload, 'status': 'captured'}
                report['calls'].append(row)
                if args.live and report['attempts'] < args.max_requests:
                    report['attempts'] += 1
                    row['status'] = 'running'
                    save()  # Count attempts even on interruption; never automatically retry.
                    started = time.monotonic()
                    try:
                        row['response'] = client.complete(payload, server.ai.config['timeout_seconds'])
                        row['proposal'] = validate_proposal(json.loads(row['response']['content']))
                        row['status'] = 'valid_protocol'
                    except (ValueError, TypeError, KeyError) as error:
                        row.update(status='error', error=str(error))
                    row['duration_ms'] = round((time.monotonic() - started) * 1000)
                save()
                print(json.dumps({k: row[k] for k in ('case', 'variant', 'status', 'context_bytes')}, ensure_ascii=False), flush=True)
        hero = server.games.heroes.list()['heroes'][0]['id']
        room = server.games.execute({'action': 'create', 'scenario': 'missing-book',
                                     'hero_id': hero, 'name': 'Offline presentation probe'}, str(uuid4()))['room_id']
        state = server.adventure.read(room)
        server.ai.client = ActionCapture()
        result = server.adventure.execute(room, {'revision': state['revision'], 'character_id': state['character_id'],
                                                 'text': 'Иду во двор.'}, str(uuid4()))
        report['offline_presentation_probe'] = {
            'proposal_reply': 'Вы направляетесь к двери под стук дождя по навесу.',
            'visible_reply': result['history'][-1]['gm'], 'location': result['location']}
        save()
    finally:
        server.server_close()
    print(str(output), flush=True)


if __name__ == '__main__':
    main()
