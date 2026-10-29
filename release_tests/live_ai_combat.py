"""Opt-in live AI exercise. Never discovered by the offline unittest suite."""
import argparse
from collections import Counter
import json
from pathlib import Path
import statistics
import time
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from uuid import uuid4

from iirpg.combat_map import approach, distance
from iirpg.combat_tactics import opponents, team


def main() -> None:
    parser = argparse.ArgumentParser(description='Play a live, free-model training battle through the server API')
    parser.add_argument('--base-url', default='http://127.0.0.1:8000')
    parser.add_argument('--access-token-file', type=Path, default=Path('.local/access-token'))
    parser.add_argument('--room', help='Continue an existing training battle; otherwise create an isolated room')
    parser.add_argument('--scenario', choices=['tavern', 'forest', 'cover'], default='tavern')
    parser.add_argument('--group', default='duel', help='Configured enemy group: duel, patrol, squad')
    parser.add_argument('--max-ai-requests', type=int, default=12)
    parser.add_argument('--report', type=Path, default=Path('.local/live-ai-combat.json'))
    args = parser.parse_args()
    if not 1 <= args.max_ai_requests <= 30:
        parser.error('--max-ai-requests must be between 1 and 30')
    token = args.access_token_file.read_text().strip()

    def api(path, body=None, key=None):
        headers = {'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'}
        if key:
            headers['Idempotency-Key'] = key
        request = Request(args.base_url + path, data=json.dumps(body).encode() if body is not None else None, headers=headers)
        try:
            with urlopen(request, timeout=90) as response:
                return json.load(response)
        except HTTPError as error:
            # Only our server's sanitized error, never provider headers/credentials.
            raise RuntimeError(f'HTTP {error.code}: ' + json.load(error).get('error', '')) from None

    settings = api('/api/ai')
    if not settings['configured'] or not settings['free_only']:
        raise SystemExit('Configure a free-only AI provider on the server first.')
    room = args.room or api('/api/rooms', {})['room_id']
    prefix = '/api/rooms/' + room
    if not args.room:
        actor = api(prefix + '/roster', {'action': 'create_character', 'revision': 0, 'data': {
            'name': 'Испытатель ИИ', 'attributes': {'strength': 4, 'dexterity': 2, 'constitution': 2, 'wisdom': 1},
            'sheet': {'values': {'level': 1, 'hp_max': 20, 'hp_current': 20,
                                 'armor_rank': 'trained', 'armor_item_bonus': 2}}}}, str(uuid4()))['entity_id']
        api(prefix + '/scene', {'action': 'load_map', 'revision': 0, 'parameters': {
            'template_id': 'forest_camp' if args.scenario == 'forest' else 'tavern'}}, str(uuid4()))
        if args.scenario == 'cover':
            scene = api(prefix + '/scene', {'action': 'join', 'revision': 1, 'actor_id': actor}, str(uuid4()))
            for position in ([5, 5], [9, 5], [10, 4]):
                scene = api(prefix + '/scene', {'action': 'move_cell', 'revision': scene['revision'],
                            'actor_id': actor, 'parameters': {'position': position}}, str(uuid4()))
        api(prefix + '/combat', {'action': 'start', 'revision': 0, 'character_id': actor, 'group_id': args.group}, str(uuid4()))
    report = {'room_id': room, 'scenario': args.scenario, 'model': settings['model'], 'group_id': args.group, 'decisions': []}
    original_roster = api(prefix + '/roster')
    print('Room:', room, 'model:', settings['model'], flush=True)
    last_call = 0.0
    try:
        for step in range(100):
            state = api(prefix + '/combat')
            if state['status'] == 'finished':
                break
            command = {'revision': state['revision']}
            victims = opponents(state, state['turn'])
            victim = min(victims, key=lambda i: distance(state['fighters'][state['turn']].get('position', [0, 0]), state['fighters'][i].get('position', [0, 0])))
            if state['actions'] == 0:
                command['action'] = 'end_turn'
            elif team(state, state['turn']) == 'enemies':
                if len(report['decisions']) >= args.max_ai_requests:
                    report['stop_reason'] = 'request_budget'
                    break
                time.sleep(max(0, 3.2 - (time.monotonic() - last_call)))
                last_call = time.monotonic()
                key = str(uuid4())
                result = api(prefix + '/ai/npc/step', command, key)
                trace = result['trace']
                decision = {key: trace[key] for key in ('request_id', 'choice', 'duration_ms', 'response')}
                report['decisions'].append(decision)
                if trace['response'].get('usage', {}).get('cost', 0) != 0:
                    raise RuntimeError('Unexpected nonzero reported cost; stopping live evaluation.')
                # Exercise the real replay path once; no second model invocation or game action.
                if len(report['decisions']) == 1:
                    assert api(prefix + '/ai/npc/step', command, key) == result
                    report['retry_verified'] = True
                print('AI:', trace['choice'], str(trace['duration_ms']) + 'ms', flush=True)
                continue
            elif state.get('mapped') and distance(state['fighters'][0]['position'], state['fighters'][victim]['position']) != 1:
                position = approach(state, api(prefix + '/scene'), victim)
                command.update(action='move', position=position) if position else command.update(action='end_turn')
            elif state['actions'] == 1 and not state['fighters'][0]['shield']:
                command['action'] = 'shield'
            else:
                command['action'] = 'strike'
                command['target_id'] = state['fighters'][victim].get('entity_id')
            api(prefix + '/combat', command, str(uuid4()))
        else:
            report['stop_reason'] = 'step_budget'
    except RuntimeError as error:
        report['error'] = str(error)
        print('STOP:', error, flush=True)
    finally:
        state = api(prefix + '/combat')
        report.update(status=state['status'], winner=state.get('winner'), rounds=state.get('round'),
                      fighters=state.get('fighters'), roster_unchanged=api(prefix + '/roster') == original_roster)
        if state.get('mapped'):
            scene = api(prefix + '/scene')
            report['map_synchronized'] = all(any(e['id'] == f['entity_id'] and e['position'] == f['position']
                                                  and e['training_hp'] == f['hp'] for e in scene['entities']) for f in state['fighters'])
        decisions = report['decisions']
        report['actions'] = dict(Counter(d['choice'].split('_')[0] for d in decisions))
        report['median_ms'] = round(statistics.median(d['duration_ms'] for d in decisions)) if decisions else None
        report['requests_today'] = api('/api/ai')['requests_today']
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
        print(json.dumps({k: v for k, v in report.items() if k not in ('decisions', 'fighters')}, ensure_ascii=False), flush=True)
    if report.get('error'):
        raise SystemExit(1)


if __name__ == '__main__':
    main()
