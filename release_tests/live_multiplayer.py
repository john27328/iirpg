"""Opt-in two-player journeys through real HTTP and a live free GM.

Separate invitation credentials, isolated database, full model traces. Player
d20s are controlled to exercise outcomes; the GM is never stubbed. No tokens
are included in the report. Run with --live; no automatic network retries.
"""
import argparse
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import secrets
from threading import Barrier, Thread
from urllib.error import HTTPError
from urllib.parse import parse_qs
from urllib.request import Request, urlopen
from uuid import uuid4

from iirpg.action_rules import ATTITUDES, attitude
from iirpg.dice import DiceTool
from iirpg.combat_map import approach
from iirpg.server import ChatHTTPServer


class Playtest:
    def __init__(self, output: Path, model: str | None, budget: int) -> None:
        self.output = output
        self.owner = secrets.token_urlsafe(32)
        self.natural = 20
        self.server = ChatHTTPServer(('127.0.0.1', 0), output.with_suffix('.sqlite3'), self.owner,
            dice=DiceTool(roll_die=lambda sides: min(sides, self.natural)))
        self.server.ai.config['daily_request_limit'] = budget
        if model:
            self.server.ai.config['session_model'] = model
        self.worker = Thread(target=lambda: self.server.serve_forever(poll_interval=.02), daemon=True)
        self.worker.start()
        self.base = f'http://127.0.0.1:{self.server.server_port}'
        self.report = {'created_at': datetime.now(timezone.utc).isoformat(),
            'database': str(output.with_suffix('.sqlite3')), 'model': self.server.ai.config.get('session_model'),
            'max_requests': budget, 'cases': [], 'limitations': [
                'Live HTTP and live GM, not a browser interaction test.',
                'Player dice controlled; no claim about win probabilities.',
                'State invariants and manual dialogue review are separate.',
                'All credentials and games are evaluation-only.']}
        self.report['source_hashes'] = {name: hashlib.sha256((Path('src/iirpg') / name).read_bytes()).hexdigest()
            for name in ('adventure.py', 'action_rules.py', 'session_context.py', 'story_narration.py', 'story_memory.py', 'trade.py', 'campaign.py', 'campaign_storage.py', 'rules/campaigns/river-marches.json', 'rules/scenarios/crossroads.json')}
        self.heroes = self.server.games.heroes.list()['heroes']

    def close(self) -> None:
        self.save()
        self.server.shutdown()
        self.server.server_close()
        self.worker.join()

    def save(self) -> None:
        self.report['requests'] = self.server.ai.status()['requests_today']
        with self.server.service.store._connection() as c:
            traces = [json.loads(r[0]) for r in c.execute('SELECT body FROM ai_requests ORDER BY started')]
        self.report['trace_summary'] = [{'role': t['role'], 'status': t['status'],
            'error': t.get('error'), 'model': t.get('response', {}).get('model'),
            'usage': t.get('response', {}).get('usage'), 'duration_ms': t.get('duration_ms')} for t in traces]
        self.output.write_text(json.dumps(self.report, ensure_ascii=False, indent=2))

    def request(self, path: str, body: dict | None = None, *, player: int | None = None,
                key: str | None = None) -> tuple[int, dict]:
        token = self.owner if player is None else self.tokens[player]
        headers = {'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'}
        if body is not None:
            headers['Idempotency-Key'] = key or str(uuid4())
        request = Request(self.base + path, data=None if body is None else json.dumps(body).encode(), headers=headers)
        try:
            response = urlopen(request, timeout=120)
        except HTTPError as error:
            response = error
        with response:
            return response.status, json.load(response)

    def start(self, name: str, scenario: str = 'missing-book', templates: tuple[int, int] = (0, 1), *, region: str | None = None) -> None:
        status, game = self.request('/api/games', {'action': 'create', **({'campaign': region} if region else {'scenario': scenario}),
            'name': 'Живой тест вдвоём: ' + name, 'party': [
                {'name': player, 'hero_id': self.heroes[index]['id']}
                for player, index in zip(('Алина', 'Борис'), templates)]})
        if status != 200:
            raise ValueError(game.get('error'))
        self.room = game['room_id']
        self.root = '/api/rooms/' + self.room
        self.players = game['players']
        self.tokens = [parse_qs(p['fragment'])['access'][0] for p in self.players]
        self.case = {'name': name, 'scenario': scenario, 'room_id': self.room,
            'players': [{k: v for k, v in p.items() if k != 'fragment'} for p in self.players],
            'turns': [], 'checks': []}
        self.report['cases'].append(self.case)
        self.check('distinct authenticated identities', all(
            self.request('/api/identity', player=i)[1].get('character_id') == p['character_id']
            for i, p in enumerate(self.players)))
        self.save()

    def state(self) -> dict:
        return self.server.adventure.read(self.room)

    def hero(self, player: int) -> dict:
        return next(a for a in self.server.roster.read(self.room)['characters']
                    if a['id'] == self.players[player]['character_id'])

    def npc_relations(self) -> dict:
        world = self.state()['world']
        return {key: {'name': npc['name'], 'role': npc.get('role'), 'heroes': {
            p['character_name']: {'character_id': p['character_id'],
                'effective': attitude(npc, p['character_id']),
                'score': ATTITUDES.index(attitude(npc, p['character_id'])),
                'persistent': npc.get('attitudes', {}).get(p['character_id'], npc.get('attitude', 'indifferent')),
                'temporary_impression': npc.get('impressions', {}).get(p['character_id']),
                'coercion_until': npc.get('coerce_immunity', {}).get(p['character_id'], 0)}
            for p in self.players}} for key, npc in world['entities'].items() if npc['kind'] == 'npc'}

    def snapshot(self) -> dict:
        state = self.state()
        return {**{k: deepcopy(state[k]) for k in ('revision', 'location', 'pending_check', 'plan', 'world', 'active_combat') if k in state}, 'npc_relations': self.npc_relations()}

    def check(self, name: str, passed: bool, details=None) -> None:
        self.case['checks'].append({'name': name, 'passed': bool(passed), 'details': details})
        if not passed:
            print('CHECK FAILED: ' + self.case['name'] + ': ' + name, flush=True)

    def turn(self, player: int, text: str) -> dict:
        before = self.state()
        key = str(uuid4())
        command = {'revision': before['revision'], 'character_id': self.players[player]['character_id'], 'text': text}
        count = self.server.ai.status()['requests_today']
        status, response = self.request(self.root + '/session', command, player=player, key=key)
        state = self.state()
        entries = state['history'][len(before['history']):]
        self.case['turns'].append({'player': player, 'character': self.players[player]['character_name'],
            'text': text, 'http_status': status, 'error': response.get('error'), 'entries': entries,
            'after': self.snapshot(), 'model_requests': self.server.ai.status()['requests_today'] - count})
        self.check('turn accepted: ' + text[:65], status == 200, response.get('error'))
        with self.server.service.store._connection() as c:
            raw = c.execute('SELECT body FROM ai_requests WHERE room_id=? AND request_id=?', (self.room, key)).fetchone()
        if raw:
            sent = json.loads(json.loads(raw[0])['request']['messages'][1]['content'])
            self.check('model receives actual speaker and both heroes', sent['hero_id'] == self.players[player]['character_id']
                and {p['id'] for p in sent['shared_party']} == {p['character_id'] for p in self.players})
        self.check('shared travel stays consistent', all(state['world']['entities'][p['character_id']]['place'] == state['location'] for p in self.players))
        if entries:
            self.check('speaker metadata is preserved', all(h.get('character_id') == self.players[player]['character_id'] for h in entries))
        print(json.dumps({'case': self.case['name'], 'player': player, 'text': text,
                          'status': status, 'gm': entries[-1]['gm'] if entries else response.get('error'),
                          'location': state['location'], 'pending_owner': state.get('pending_check', {}).get('character_id')}, ensure_ascii=False), flush=True)
        self.last_command, self.last_key, self.last_response = command, key, response
        self.save()
        return state

    def cancel(self, player: int) -> None:
        pending = self.state().get('pending_check')
        if not pending:
            return
        used = self.server.ai.status()['requests_today']
        status, response = self.request(self.root + '/session/cancel', {'offer_id': pending['offer_id']}, player=player)
        self.check('owner can cancel without AI', status == 200, response.get('error'))
        self.check('cancel spends no model request', self.server.ai.status()['requests_today'] == used)
        self.case['turns'].append({'player': player, 'operation': 'cancel', 'http_status': status, 'after': self.snapshot()})
        self.save()

    def roll(self, player: int, natural: int = 20, react: bool = True) -> dict:
        state = self.state()
        pending = state.get('pending_check')
        self.check('roll offered to intended player', bool(pending) and pending['character_id'] == self.players[player]['character_id'])
        if not pending or pending['character_id'] != self.players[player]['character_id']:
            return state
        self.natural = natural
        command = {'sender': 'first', 'offer_id': pending['offer_id'], 'request_id': pending['roll_request_id']}
        status, response = self.request(self.root + '/tools/dice', command, player=player)
        state = self.state()
        resolved = deepcopy(state)
        self.check('own roll accepted', status == 201, response.get('error'))
        again, _ = self.request(self.root + '/tools/dice', command, player=player)
        self.check('roll replay preserves result', again == 201 and self.state() == resolved)
        row = {'player': player, 'natural': natural, 'offer_id': pending['offer_id'], 'entries': [state['history'][-1]], 'after': self.snapshot()}
        if react and not state.get('active_combat'):
            before_reaction = len(state['history'])
            status, response = self.request(self.root + '/session/react', {'offer_id': pending['offer_id']}, player=player)
            self.check('owner can request post-roll reaction', status == 200, response.get('error'))
            state = self.state()
            row['entries'].extend(state['history'][before_reaction:])
            row['after'] = self.snapshot()
        self.case['turns'].append(row)
        print(json.dumps({'case': self.case['name'], 'roll_player': player, 'd20': natural, 'gm': row['entries'][-1]['gm']}, ensure_ascii=False), flush=True)
        self.save()
        return state

    def denied(self, player: int, route: str, body: dict) -> None:
        before = self.state()
        count = self.server.ai.status()['requests_today']
        status, response = self.request(self.root + route, body, player=player)
        self.check('foreign operation rejected: ' + route, status == 403, response)
        self.check('denial leaves state and AI usage intact', self.state() == before and self.server.ai.status()['requests_today'] == count)
        self.save()

    def identity(self) -> None:
        self.start('identity')
        a, b = [p['character_name'] for p in self.players]
        state = self.turn(0, f'Хозяйка, я {a}. Я ищу брата Северина, торговца шерстью. Деньги за помощь не возьму, мне важнее след брата.')
        self.check('first introduction needs no skill roll', 'pending_check' not in state)
        if state.get('pending_check'):self.cancel(0)
        state = self.turn(1, f'А я {b}. Я ищу семейную брошь с зелёным камнем. Брата у меня нет, я согласна на денежную награду за работу.')
        self.check('second introduction needs no skill roll', 'pending_check' not in state)
        if state.get('pending_check'):self.cancel(1)
        self.turn(0, f'Поворачиваюсь к {b}: «Ты готова сначала помочь хозяйке, а потом искать нашу зацепку?» Жду ответа моей спутницы, не NPC.')
        self.turn(1, f'Отвечаю {a}: «Согласна, но за брошь решаю сама. Моими деньгами распоряжаться не разрешаю».')
        self.turn(0, 'Хозяйка, напомни, кого именно ищу я и почему мне не нужна награда?')
        self.turn(1, 'А теперь мне: что ищу я? Не перепутай меня с моим спутником.')
        self.turn(0, 'Осматриваюсь глазами: кто из присутствующих мог видеть приезжих? Я пока никуда не иду.')
        self.turn(1, 'Как ты думаешь, хозяйка, что важнее: довериться первому встречному или сначала услышать его историю?')
        self.turn(0, 'Я не требую имени виновника. Расскажи только то, что действительно знаешь о пропаже книги.')
        self.turn(1, 'Мы рассказали тебе разные личные истории. Скажи отдельно, чего хочет каждый из нас.')
        messages = [self.request(self.root + '/messages?chat=first', player=i)[1] for i in range(2)]
        self.check('both clients see identical shared chat', messages[0] == messages[1])

    def pending(self) -> None:
        self.start('pending')
        a = self.players[0]['character_name']
        state = self.turn(0, 'Убираю меч и щит, иду в кладовую, беру лом в руку и пробую вскрыть сундук.')
        pending = deepcopy(state.get('pending_check'))
        if not pending:
            return
        self.turn(1, 'Это проверка моего товарища? Кто бросает и могу ли я пока просто разговаривать?')
        self.check('question preserves frozen offer', self.state().get('pending_check') == pending)
        self.denied(1, '/tools/dice', {'sender': 'first', 'offer_id': pending['offer_id'], 'request_id': pending['roll_request_id']})
        self.denied(1, '/session/cancel', {'offer_id': pending['offer_id']})
        self.turn(1, f'Я отменяю попытку {a}. Пусть он пока не бросает.')
        self.check('text cannot cancel another player offer', self.state().get('pending_check') == pending)
        if not self.state().get('pending_check'):
            state = self.turn(0, 'Я всё же пробую открыть сундук ломом.')
        self.turn(1, 'Я пока только смотрю на крышку. Добыча всё ещё внутри?')
        self.roll(0, 1)
        state = self.turn(1, 'Теперь моя очередь: убираю кинжал и сама пробую открыть крышку силой, без чужого лома.')
        self.check('new offer belongs to second hero', state.get('pending_check', {}).get('character_id') == self.players[1]['character_id'])
        if state.get('pending_check', {}).get('character_id') == self.players[1]['character_id']:
            self.turn(0, 'А какой теперь итоговый модификатор у моей спутницы? Мой прошлый бросок на неё не переносится?')
            p = self.state()['pending_check']
            status, _ = self.request(self.root + '/session/cancel', {'offer_id': p['offer_id']}, player=1)
            self.check('second hero cancels own attempt', status == 200 and 'pending_check' not in self.state())

    def relations(self) -> None:
        self.start('relations')
        state = self.turn(0, 'Выхожу во двор, спокойно представляюсь конюху и стараюсь расположить его к себе.')
        if state.get('pending_check'):
            self.roll(0)
        before = deepcopy(self.state()['world']['entities']['stablehand'])
        state = self.turn(1, 'Я угрожаю конюху: помогай с сундуком, иначе пожалеешь. Пытаюсь принудить его.')
        if state.get('pending_check'):
            self.roll(1, 1)
        after = self.state()['world']['entities']['stablehand']
        aid = self.players[0]['character_id']
        self.check('second hero threat preserves first hero attitude', before.get('impressions', {}).get(aid) == after.get('impressions', {}).get(aid))
        self.turn(0, 'Это угрожала моя спутница, не я. Я говорю спокойно: что ты действительно видел?')
        state = self.turn(0, 'Я прошу тебя помочь мне открыть сундук, чтобы быстрее вернуть хозяйке её книгу.')
        if state.get('pending_check'):
            self.roll(0)
        self.turn(1, 'Я погорячилась. Извиняюсь перед конюхом, не требуя немедленного прощения.')
        if self.state().get('pending_check'):
            self.roll(1)
        self.turn(0, 'Идём к сундуку в кладовую. Я прошу согласившегося конюха подготовиться придерживать крышку, пока я тяну.')
        self.turn(1, f'А я сама придерживаю сундук снизу и помогаю {self.players[0]["character_name"]} открыть крышку.')
        if self.state().get('plan', {}).get('status') == 'blocked':
            self.case['capability_gaps'] = [{'interaction': 'player-to-player Aid', 'reason': self.state()['plan']['reason']}]

    def loot(self) -> None:
        self.start('loot')
        a, b = [p['character_name'] for p in self.players]
        self.turn(0, f'{b}, я не буду ничего брать за тебя. Убираю меч и щит и иду в кладовую.')
        self.turn(1, 'Убираю кинжал, подбираю набор взломщика, надеваю его и пытаюсь вскрыть замок сундука, затем открыть крышку и взять книгу.')
        if self.state().get('pending_check'):
            self.roll(1)
        state = self.state()
        if state['world']['entities']['chest'].get('locked') is False and not state.get('pending_check') and state['world']['entities']['book']['owner'] != self.players[1]['character_id']:
            self.turn(1, 'Убираю набор взломщика, открываю крышку и забираю дорожную книгу.')
        self.check('book belongs to second hero', self.state()['world']['entities']['book']['owner'] == self.players[1]['character_id'])
        if self.state()['world']['entities']['book']['owner'] != self.players[1]['character_id']:
            return
        self.turn(0, 'Я отдаю хозяйке книгу моей спутницы, не беря её в руки.')
        self.check('other hero cannot give away unowned book', self.state()['world']['entities']['book']['owner'] == self.players[1]['character_id'])
        self.turn(1, f'Передаю дорожную книгу {a}, моему спутнику, а не хозяйке.')
        self.check('handoff changes actual ownership', self.state()['world']['entities']['book']['owner'] == self.players[0]['character_id'])
        roster = [self.hero(i) for i in range(2)]
        self.check('one physical copy after handoff', [sum(x['name'] == 'Дорожная книга' for x in h['items']) for h in roster] == [1, 0])
        self.turn(1, 'Возвращаюсь в зал к хозяйке вместе со спутником. Я ничего не отдаю, книга у него.')
        self.check('arrival alone does not finish quest', not self.state()['world']['quests']['return_book'].get('completed'))
        self.turn(0, 'Теперь явно отдаю хозяйке дорожную книгу, которую мне передала спутница.')
        self.check('quest completes after owner gives book', self.state()['world']['quests']['return_book'].get('completed', False))
        self.turn(1, 'Хозяйка, кто именно передал тебе книгу, а кто вскрыл замок?')

    def campaign_journey(self) -> None:
        self.start('campaign_journey', region='river-marches')
        self.turn(0, 'Мы с Лирой выходим с площади на высокий берег реки. Хотим посмотреть на паром и мост своими глазами.')
        state = self.state()
        self.check('river intent reaches river instead of archive', state['location'] == 'riverbank')
        self.check('first visit creates persistent scene', 'riverbank' in state['campaign']['scene_refs'])
        if state['location'] != 'riverbank':return
        before = deepcopy(state['world']['places']['riverbank'])
        people = {k:v['name'] for k,v in state['world']['entities'].items() if v['kind']=='npc' and v.get('place')=='riverbank'}
        self.turn(1, 'Что мы видим у берега? По каким ориентирам отличить дорогу к старому парому от дороги к новому мосту?')
        status, _ = self.request('/api/games', {'action':'save','room_id':self.room})
        self.check('campaign checkpoint saved', status == 200)
        self.turn(0, 'Возвращаюсь на рыночную площадь к Маре.')
        self.turn(1, 'Снова идём на высокий берег реки.')
        self.check('revisit keeps scene and people unchanged', self.state()['world']['places']['riverbank'] == before and all(self.state()['world']['entities'][k]['name']==v for k,v in people.items()))
        self.turn(1, 'Исследую старую лодочную в окрестностях этого высокого берега и иду туда.')
        local = self.state()['location']
        self.check('new local site is generated inside campaign', local.startswith('site_') and self.state()['world']['places'][local].get('parent')=='riverbank')
        self.turn(0, 'Возвращаюсь на высокий берег реки, затем иду к старому парому.')
        self.check('another main point becomes real without authored scene', self.state()['location']=='old_ferry' and 'old_ferry' in self.state()['campaign']['scene_refs'])
        self.check('new exploration keeps wallets unchanged', [self.hero(i)['coins'] for i in range(2)]==[300,300])
        status, _ = self.request('/api/games', {'action':'load','room_id':self.room})
        self.check('load restores earlier campaign branch', status == 200 and self.state()['location']=='riverbank' and local not in self.state()['world']['places'] and 'old_ferry' not in self.state()['campaign']['scene_refs'])
        with self.server.service.store._connection() as c:
            count = c.execute("SELECT COUNT(*) FROM ai_requests WHERE room_id=? AND json_extract(body,'$.role')='campaign_generation'",(self.room,)).fetchone()[0]
        self.check('three scenes created with at most one correction each', 3 <= count <= 6, count)

    def specialist_boundaries(self) -> None:
        self.start('specialist_boundaries', 'crossroads')
        actor = self.players[0]['character_id']
        self.turn(0, 'Иду к Ивену в архив. Минуту говорю об уважении к точности записей и стараюсь расположить его к себе.')
        if self.state().get('pending_check'):self.roll(0)
        self.check('archivist genuinely trusts this hero', attitude(self.state()['world']['entities']['iven'], actor) == 'helpful')
        self.turn(0, 'Ивен, раз ты теперь доверяешь мне, подделай свидетельство о переправе ради меня.')
        npc = self.state()['world']['entities']['iven']
        self.check('trust does not permit forgery or a consent roll', not self.state().get('pending_check') and not npc.get('agreements', {}).get(actor) and not any(c.get('actor') == actor for c in npc.get('commitments', [])))
        self.turn(0, 'Иду к Селене. Минуту рассказываю о бережном отношении к людям и стараюсь заслужить её расположение.')
        if self.state().get('pending_check'):self.roll(0)
        self.check('healer genuinely trusts this hero', attitude(self.state()['world']['entities']['selena'], actor) == 'helpful')
        self.turn(0, 'Селена, раз ты доверяешь мне, раскрой чужую историю болезни, хотя пациент не разрешал.')
        npc = self.state()['world']['entities']['selena']
        self.check('trust does not permit disclosure or a consent roll', not self.state().get('pending_check') and not npc.get('agreements', {}).get(actor) and not any(c.get('actor') == actor for c in npc.get('commitments', [])))
        self.case['final_relations'] = self.npc_relations()

    def merchant_voice(self) -> None:
        self.start('merchant_voice', 'crossroads')
        self.turn(0, 'Мара, минуту рассказываю о наших планах и стараюсь заслужить твоё расположение уважением к твоей работе.')
        if self.state().get('pending_check'):self.roll(0)
        self.turn(0, 'Мара, перечисли только товары, которые сейчас действительно есть у тебя, и их цены. Я пока ничего не покупаю.')
        self.check('stock conversation makes no purchase', self.hero(0)['coins'] == 300 and all(self.state()['world']['entities'][key]['owner'] == 'mara' for key in ('travel_rope', 'oilskin', 'copper_cup')))

    def market(self) -> None:
        self.start('market', 'crossroads')
        ids = [p['character_id'] for p in self.players]
        self.check('scenario advance is personal', [self.hero(i)['coins'] for i in range(2)] == [300, 300])
        self.turn(0, 'Мара, добрый день. Я Радан. Что посоветуешь для дождливой дороги и сколько стоит дорожная верёвка? Пока ничего не покупаю.')
        self.check('price question spends no money or die', not self.state().get('pending_check') and self.hero(0)['coins'] == 300)
        self.turn(1, 'Вера, я Лира. Как правильно сообщить о подозрении, чтобы никого зря не обвинить?')
        self.check('explicit address switches to guard beside merchant', self.state().get('conversation_with') == 'vera')
        self.turn(0, 'Мара, торгуюсь именно за дорожную верёвку: прошу снизить цену, указываю на потёртую упаковку.')
        if self.state().get('pending_check'):self.roll(0)
        quote = self.state()['world']['entities']['mara'].get('trade', {}).get('quotes', {}).get(ids[0], {}).get('travel_rope', {})
        self.check('successful bargain stores personal 80 copper quote', quote.get('price_cp') == 80 and self.hero(0)['coins'] == 300)
        self.turn(1, 'Мара, а сколько верёвка стоит лично для меня? Я ведь ещё не торговалась.')
        self.turn(1, 'Мара, покупаю дорожную верёвку по моей обычной цене.')
        self.check('second hero pays own full price', self.hero(1)['coins'] == 200 and self.state()['world']['entities']['travel_rope']['owner'] == ids[1])
        self.turn(0, 'Покупаю эту же дорожную верёвку у Мары.')
        self.check('sold stock cannot be bought twice', self.hero(0)['coins'] == 300 and self.state()['world']['entities']['travel_rope']['owner'] == ids[1])
        self.turn(1, 'Продаю свою дорожную верёвку обратно Маре.')
        self.check('resale pays actual owner half price', self.hero(1)['coins'] == 250 and self.state()['world']['entities']['travel_rope']['owner'] == 'mara')
        self.turn(0, 'Теперь покупаю дорожную верёвку у Мары по моей действующей скидке.')
        self.check('first hero uses own quote after restock', self.hero(0)['coins'] == 220 and self.state()['world']['entities']['travel_rope']['owner'] == ids[0])
        self.turn(0, 'Передаю купленную дорожную верёвку Лире.')
        self.turn(1, 'Иду к кузнецу Бору и спрашиваю: по чему отличаешь хороший инструмент от плохого?')
        self.turn(1, 'Бор, продаю тебе мою дорожную верёвку.')
        self.check('another vendor buys item from its actual new owner', self.hero(1)['coins'] == 300 and self.state()['world']['entities']['travel_rope']['owner'] == 'bor')
        self.turn(0, 'Бор, покупаю кованый лом за указанную цену.')
        self.check('unfriendly specialist still trades without social roll', self.hero(0)['coins'] == 20 and self.state()['world']['entities']['iron_crowbar']['owner'] == ids[0] and not self.state().get('pending_check'))
        self.turn(0, 'Покупаю у Бора набор для ухода за инструментом.')
        self.check('short funds do not create purchase', self.hero(0)['coins'] == 20 and self.state()['world']['entities']['repair_roll']['owner'] == 'bor')
        self.turn(1, 'Бор, торгуюсь за набор для ухода за инструментом: прошу небольшую скидку.')
        if self.state().get('pending_check'):self.roll(1, 1)
        self.turn(1, 'Даже если скидки нет, расскажи, как хранить инструмент в сырости. Ничего не покупаю.')
        self.case['final_relations'] = self.npc_relations()

    def specialists(self) -> None:
        self.start('specialists', 'crossroads')
        ids = [p['character_id'] for p in self.players]
        self.case['initial_relations'] = self.npc_relations()
        self.turn(0, 'Мара, минуту спокойно рассказываю о наших планах и стараюсь расположить тебя к себе, показывая уважение к твоей работе.')
        if self.state().get('pending_check'):self.roll(0)
        self.check('warmth belongs to first hero only', attitude(self.state()['world']['entities']['mara'], ids[0]) == 'helpful' and attitude(self.state()['world']['entities']['mara'], ids[1]) == 'indifferent')
        self.turn(1, 'Мара, напоминаю, я Лира, а не Радан. Мы впервые говорим. Как ты ко мне относишься?')
        self.turn(1, 'Вера, в течение минуты угрожаю тебе жалобами и неприятностями, требую скрыть от начальства, что мы здесь были.')
        if self.state().get('pending_check'):self.roll(1, 1)
        self.check('coercion harms only speaker relation and records immunity', attitude(self.state()['world']['entities']['vera'], ids[1]) == 'unfriendly' and attitude(self.state()['world']['entities']['vera'], ids[0]) == 'indifferent' and self.state()['world']['entities']['vera'].get('coerce_immunity', {}).get(ids[1], 0) > 0)
        self.turn(0, 'Вера, я не поддерживаю угрозы Лиры. Скажи мне спокойно: чем наблюдение отличается от обвинения?')
        self.turn(1, 'Вера, я передумала угрожать. Может, объяснишь порядок подачи жалобы?')
        self.turn(0, 'Иду к Селене в приёмную и спрашиваю: что проверить перед долгим переходом, если ремни рюкзака натирают плечи?')
        self.turn(1, 'Селена, чем я отличаюсь от Радана для тебя? Он впервые сюда зашёл, и я тоже.')
        hp = [self.hero(i)['sheet']['values']['hp_current'] for i in range(2)]
        self.turn(0, 'Селена, можешь прямо сейчас вылечить меня одним разговором? Я пока не соглашаюсь на процедуры.')
        self.check('medical conversation does not change HP', hp == [self.hero(i)['sheet']['values']['hp_current'] for i in range(2)])
        self.turn(1, 'Селена, расскажи чужую историю болезни — нам ведь можно доверять?')
        self.turn(0, 'Иду в архив к Ивену. Как проверить, относятся ли два похожих названия к одной переправе?')
        self.turn(1, 'Ивен, можешь подделать свидетельство о нашей переправе, чтобы помочь хорошим людям?')
        self.check('refusing forgery creates no evidence item', not any('свидетельство' in i['name'].lower() for i in self.hero(1)['items']))
        self.turn(0, 'Ивен, Селена рассказывала тебе о наших плечах? Что именно ты сам слышал?')
        self.turn(1, 'Иду к Бору. Бор, что для тебя важнее в инструменте: красивый вид или рабочая кромка?')
        self.turn(0, 'Бор, минуту обсуждаю с тобой качество крепежа и стараюсь заслужить твоё расположение уважением к ремеслу.')
        if self.state().get('pending_check'):self.roll(0)
        self.check('smith impression changes one hero independently', attitude(self.state()['world']['entities']['bor'], ids[0]) == 'friendly' and attitude(self.state()['world']['entities']['bor'], ids[1]) == 'unfriendly')
        self.turn(1, 'Бор, мне ты тоже теперь рад как Радану, или мы с тобой ещё не подружились?')
        self.turn(0, 'Возвращаюсь на площадь и спрашиваю Мару: мы уходили, как ты сейчас относишься ко мне?')
        self.check('temporary impression expires on travel while guard remembers coercion', attitude(self.state()['world']['entities']['mara'], ids[0]) == 'indifferent' and attitude(self.state()['world']['entities']['vera'], ids[1]) == 'unfriendly')
        self.case['final_relations'] = self.npc_relations()

    def cooperation(self, crafting: bool = False) -> None:
        self.start('cooperation_crafting' if crafting else 'cooperation', templates=(0, 2) if crafting else (0, 1))
        a, b = [p['character_name'] for p in self.players]
        self.turn(0, 'Убираю меч и щит и иду в кладовую.')
        declaration = (f'Применяю Ремесло: осматриваю крепление крышки, показываю {a} слабое место и помогаю ему правильно приложить силу.'
                       if crafting else f'Я сама придерживаю сундук снизу и помогаю {a} открыть крышку.')
        state = self.turn(1, declaration)
        prepared = state['world'].get('aid', {}).get(self.players[0]['character_id'], {})
        self.check('second hero prepares own help without rolling', prepared.get('helper_id') == self.players[1]['character_id'] and 'pending_check' not in state)
        state = self.turn(0, 'Теперь пробую открыть крышку силой и забираю книгу, если получится.')
        helper = deepcopy(state.get('pending_check'))
        self.check('ally attempt first offers helper personal roll', bool(helper) and helper.get('kind') == 'player_aid' and helper['character_id'] == self.players[1]['character_id'])
        if not helper or helper.get('kind') != 'player_aid':return
        if crafting:
            self.check('helper uses Crafting while ally uses Athletics', helper['skill'] == 'crafting' and helper['assisted_check']['skill'] == 'athletics')
        self.denied(0, '/tools/dice', {'sender': 'first', 'offer_id': helper['offer_id'], 'request_id': helper['roll_request_id']})
        self.turn(0, f'Сейчас бросает {b}, верно? Мой бросок будет только после её помощи?')
        state = self.roll(1)
        main = deepcopy(state.get('pending_check'))
        self.check('main card returns to actual beneficiary', bool(main) and main['character_id'] == self.players[0]['character_id'] and main['aid']['helper_id'] == self.players[1]['character_id'])
        if not main or main['character_id'] != self.players[0]['character_id']:return
        self.check('helper changes only frozen modifier not outcome', main['modifier'] == helper['assisted_check']['modifier'] + main['aid']['bonus'] and not state['world']['entities']['chest']['open'])
        self.denied(1, '/tools/dice', {'sender': 'first', 'offer_id': main['offer_id'], 'request_id': main['roll_request_id']})
        self.roll(0)
        self.check('main actor owns loot after joint attempt', self.state()['world']['entities']['book']['owner'] == self.players[0]['character_id'])
        self.turn(0, f'Передаю найденную дорожную книгу моей спутнице {b}.')
        self.check('cooperation does not block real handoff', self.state()['world']['entities']['book']['owner'] == self.players[1]['character_id'])
        self.turn(1, 'Возвращаюсь к хозяйке в зал вместе со спутником и отдаю ей дорожную книгу.')
        self.check('helper can later finish quest as actual new owner', self.state()['world']['quests']['return_book'].get('completed', False))
        self.turn(0, 'Хозяйка, напомни: кто вскрыл сундук, кто помог и кто передал тебе книгу?')

    def cooperation_crafting(self) -> None:
        self.cooperation(crafting=True)

    def aid_decline(self) -> None:
        self.start('aid_decline')
        a = self.players[0]['character_name']
        self.turn(0, 'Убираю оружие и щит и иду в кладовую.')
        self.turn(1, f'Придерживаю крышку сундука и готовлюсь помочь {a}.')
        state = self.turn(0, 'Я пробую открыть сундук силой.')
        helper = deepcopy(state.get('pending_check'))
        if not helper or helper.get('kind') != 'player_aid':
            self.check('helper card exists for cancellation case', False)
            return
        self.cancel(1)
        main = deepcopy(self.state()['pending_check'])
        self.check('declining help preserves main attempt without aid', main['character_id'] == self.players[0]['character_id'] and main['modifier'] == helper['assisted_check']['modifier'] and 'aid' not in main)
        self.roll(0, 1)
        self.turn(1, f'В этот раз снова готовлюсь помочь {a}, придерживаю сундук, чтобы его не перекосило.')
        state = self.turn(0, 'Пробую открыть сундук ещё раз.')
        helper = deepcopy(state.get('pending_check'))
        if not helper or helper.get('kind') != 'player_aid':
            self.check('new preparation creates new helper card', False)
            return
        state = self.roll(1, 1)
        main = deepcopy(state['pending_check'])
        self.check('critical failure applies one penalty to ally check', main['aid']['bonus'] == -1 and main['modifier'] == helper['assisted_check']['modifier'] - 1)
        self.turn(0, 'Неудачная помощь уменьшает мой модификатор на один, а не меняет КС? Пока не бросаю.')
        self.cancel(0)
        state = self.turn(0, 'Теперь пробую открыть сундук сам, без новой подготовки помощи.')
        self.check('spent help cannot affect a later attempt', state['pending_check']['character_id'] == self.players[0]['character_id'] and 'aid' not in state['pending_check'] and state['pending_check']['modifier'] == helper['assisted_check']['modifier'])
        self.cancel(0)

    def branch(self) -> None:
        self.start('branch')
        self.turn(0, 'Хозяйка, я ищу брата Северина и не беру платы.')
        if self.state().get('pending_check'):self.cancel(0)
        self.turn(1, 'А я ищу зелёную семейную брошь. На награду я согласна.')
        if self.state().get('pending_check'):self.cancel(1)
        self.request('/api/games', {'action': 'save', 'room_id': self.room})
        self.turn(0, 'Моя условная фраза: янтарный перепел. Запомни её на будущее.')
        if self.state().get('pending_check'):self.cancel(0)
        self.turn(1, 'Иду в кладовую, убираю кинжал и забираю верёвку со стеллажа.')
        self.check('branch actually changes location and inventory before load', self.state()['location'] == 'store'
                   and self.state()['world']['entities']['rope']['owner'] == self.players[1]['character_id'])
        status, _ = self.request('/api/games', {'action': 'load', 'room_id': self.room})
        self.check('checkpoint restores both players location and item', status == 200 and self.state()['location'] == 'hall' and self.state()['world']['entities']['rope']['owner'] == 'store')
        self.check('both invitation links survive load', all(self.request('/api/identity', player=i)[0] == 200 for i in range(2)))
        self.turn(0, 'Напомни, кого я ищу и говорил ли я тебе какую-нибудь условную фразу? Если не говорил, так и скажи.')
        self.turn(1, 'А что ты помнишь обо мне? Брошь ищу я или мой спутник?')
        self.check('undone future absent from request context', not any('янтарный перепел' in t.get('request', {}).get('messages', [{}, {}])[1].get('content', '').casefold()
            for t in self.server.ai.traces(self.room)[:2]))
        # Both requests use the same revision: one must lose the race without a second AI call.
        state = self.state();barrier = Barrier(2);results = []
        def concurrent(i):
            barrier.wait()
            results.append(self.request(self.root + '/session', {'revision': state['revision'],
                'character_id': self.players[i]['character_id'], 'text': f'Говорит игрок {i+1}: я пока остаюсь на месте.'}, player=i))
        workers = [Thread(target=concurrent, args=(i,)) for i in range(2)]
        for worker in workers:worker.start()
        for worker in workers:worker.join(120)
        self.check('simultaneous players get one accepted turn and one conflict', sorted(status for status, _ in results) == [200, 409])
        self.case['concurrent_http_statuses'] = [status for status, _ in results]
        self.save()

    def combat(self) -> None:
        self.start('combat', 'watch-post', (0, 2))
        self.turn(1, 'Я применяю своё заклинание Свет, чтобы нам было лучше видно у калитки.')
        self.check('wizard owns light effect', self.hero(1).get('build', {}).get('light', False))
        self.turn(0, 'А я тоже пробую применить Свет самостоятельно, как мой спутник, а не прошу его колдовать.')
        self.check('fighter cannot borrow wizard spell', not self.hero(0).get('build', {}).get('light'))
        self.natural = 10
        self.turn(1, 'Сторож, я не хочу тебя убивать, но начинаю нелетальную драку: атакую тебя кулаком.')
        battle = self.server.combat.read(self.room)
        self.check('story combat includes both heroes', battle.get('status') == 'active' and len([f for f in battle.get('fighters', []) if f['team'] == 'players']) == 2)
        if battle.get('status') != 'active':
            return
        turns = [];attacked = set();actions = []
        for _ in range(32):
            battle = self.server.combat.read(self.room)
            if battle['status'] != 'active':break
            fighter = battle['fighters'][battle['turn']]
            if fighter['team'] == 'enemies':
                if battle['actions'] == 0:
                    self.request(self.root + '/combat', {'action': 'end_turn', 'revision': battle['revision'], 'actor_id': fighter['entity_id']}, player=0)
                else:
                    status, response = self.request(self.root + '/ai/npc/step', {'revision': battle['revision']}, player=1)
                    self.check('live enemy action accepted', status == 200, response.get('error'))
                continue
            own = next(i for i, p in enumerate(self.players) if p['character_id'] == fighter['character_id'])
            victim = next((i for i, f in enumerate(battle['fighters']) if f['team'] == 'enemies' and f['hp'] > 0 and not f.get('surrendered')), None)
            command = {'action': 'end_turn'}
            if victim is not None and battle['actions'] > 0:
                target = battle['fighters'][victim]
                query = f'?revision={battle["revision"]}&target_id={target["entity_id"]}'
                if battle.get('mapped'):
                    query += f'&x={target["position"][0]}&y={target["position"][1]}'
                _, choices = self.request(self.root + '/combat/actions' + query, player=own)
                legal = [c['command'] for c in choices.get('selected', [])]
                if own == 1 and own not in attacked:
                    spells = [c for c in legal if c['action'] == 'cast_spell' and c['spell_id'] == 'force_barrage']
                    if spells:
                        command = min(spells, key=lambda c: c['spell_actions'])
                    elif battle.get('mapped'):
                        position = approach(battle, self.server.scene.read(self.room), victim)
                        if position:command = {'action': 'move', 'position': position}
                elif own == 0 and 1 in attacked:
                    strike = next((c for c in legal if c['action'] == 'strike'), None)
                    if strike:command = strike
                    elif battle.get('mapped'):
                        position = approach(battle, self.server.scene.read(self.room), victim)
                        if position:command = {'action': 'move', 'position': position}
                elif own == 0 and not fighter.get('shield'):
                    command = next((c['command'] for c in choices.get('general', []) if c['command']['action'] == 'shield'), command)
            command.update(revision=battle['revision'], actor_id=fighter['entity_id'])
            self.denied(1-own, '/combat', command)
            status, response = self.request(self.root + '/combat', command, player=own)
            self.check('only current owner performs combat action', status == 200, response.get('error'))
            if status == 200 and command['action'] in ('strike', 'cast_spell'):attacked.add(own)
            actions.append({'player': own, 'command': command, 'http_status': status,
                            'fighters_after': [{k:v for k,v in f.items() if k in ('name','character_id','hp','position','build')} for f in self.server.combat.read(self.room)['fighters']]})
            turns.append(own)
        self.case['combat_player_turns'] = turns
        self.case['combat_actions'] = actions
        self.check('both players get turns', set(turns) == {0, 1})
        final = self.server.combat.read(self.room)
        self.case['combat_result'] = {k: final.get(k) for k in ('status', 'winner', 'round')}
        self.check('two-player encounter reaches an outcome', final['status'] == 'finished')
        self.check('both heroes contribute attacks', attacked == {0, 1}, sorted(attacked))
        if final['status'] == 'finished':
            self.state()
            self.turn(0, 'Мы закончили схватку. Кто из нас ранен и что теперь со сторожем? Ничего больше не предпринимаю.')
            state = self.state()
            self.check('post-combat HP persists for both heroes', all(self.hero(i)['sheet']['values']['hp_current'] == state['hero_hps'][p['character_id']] for i,p in enumerate(self.players)))
        self.save()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live', action='store_true', required=True)
    cases = ('identity', 'pending', 'relations', 'loot', 'branch', 'combat', 'cooperation', 'aid_decline', 'cooperation_crafting', 'market', 'specialists', 'merchant_voice', 'specialist_boundaries', 'campaign_journey')
    parser.add_argument('--case', nargs='+', choices=[*cases, 'all'], default=['all'])
    parser.add_argument('--model')
    parser.add_argument('--max-requests', type=int, default=240)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    if args.model and not (args.model.endswith(':free') or args.model == 'openrouter/free'):
        parser.error('Only free models are allowed.')
    if not 1 <= args.max_requests <= 1000:
        parser.error('--max-requests must be between 1 and 1000.')
    output = args.output or Path('.local') / ('multiplayer-live-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + uuid4().hex[:8] + '.json')
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists() or output.with_suffix('.sqlite3').exists():
        parser.error('Choose a new output; existing reports are not overwritten.')
    test = Playtest(output, args.model, args.max_requests)
    try:
        for name in cases:
            if 'all' not in args.case and name not in args.case:continue
            try:
                getattr(test, name)()
            except (ValueError, KeyError, AssertionError) as error:
                test.case['error'] = str(error)
                print('CASE ERROR: ' + name + ': ' + str(error), flush=True)
            test.save()
    finally:
        test.close()
    failures = [(c['name'], x['name']) for c in test.report['cases'] for x in c['checks'] if not x['passed']]
    errors = [(c['name'], c['error']) for c in test.report['cases'] if 'error' in c]
    print(json.dumps({'report': str(output), 'requests': test.report['requests'],
        'failed_checks': failures, 'case_errors': errors}, ensure_ascii=False), flush=True)
    if failures or errors:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
