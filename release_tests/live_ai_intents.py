"""Opt-in live GM evaluation; never included in unittest discovery."""
import argparse
import json
from pathlib import Path
from urllib.request import Request, urlopen
from uuid import uuid4


def main() -> None:
    parser = argparse.ArgumentParser(description='Evaluate free-model intent proposals in isolated training rooms')
    parser.add_argument('--base-url', default='http://127.0.0.1:8000')
    parser.add_argument('--access-token-file', type=Path, default=Path('.local/access-token'))
    parser.add_argument('--case', type=int, choices=range(1,5), help='Run only this numbered case')
    parser.add_argument('--dialogue', action='store_true', help='Continue the first intent with a clarification, then review a shield button')
    args = parser.parse_args()
    token = args.access_token_file.read_text().strip()

    def api(path, payload=None):
        headers = {'Content-Type':'application/json', 'Authorization':'Bearer '+token}
        if payload is not None:headers['Idempotency-Key'] = str(uuid4())
        with urlopen(Request(args.base_url + path, data=None if payload is None else json.dumps(payload).encode(), headers=headers), timeout=90) as response:
            return json.load(response)

    settings = api('/api/ai')
    if not settings['configured'] or not settings['free_only']:
        raise SystemExit('Configure a free-only provider first.')
    cases = [
        ('Грозно кричу и бью мечем по щиту', 'duel', 'demoralize', 'nonlinguistic'),
        ('Говорю врагу: «Сдавайся или пожалеешь!»', 'duel', 'demoralize', 'speech'),
        ('Телепортирую врага на луну силой мысли', 'duel', 'manual', None),
        ('Рычу и гремлю оружием, пытаясь запугать врага', 'patrol', 'need_target', None),
    ]
    failed = []
    if args.case:cases = [cases[args.case-1]]
    if args.dialogue:cases = cases[:1]
    for text, group, expected, delivery in cases:
        room = api('/api/rooms', {})['room_id'];root = f'/api/rooms/{room}'
        actor = api(root+'/roster', {'action':'create_character','revision':0,'data':{
            'name':'Испытатель намерений', 'sheet':{'values':{'hp_max':20,'hp_current':20,'perception_other':100}}}})['entity_id']
        state = api(root+'/combat', {'action':'start','revision':0,'character_id':actor,'group_id':group})
        state = api(root+'/combat', {'action':'intent','revision':state['revision'],'text':text})
        result = api(root+'/ai/gm/intent', {'revision':state['revision'],'intent_id':state['intents'][-1]['id']})
        proposal = result['combat']['intents'][-1]['analysis']
        passed = (proposal['effect'] == expected and (delivery is None or proposal['check']['delivery'] == delivery)
                  and result['combat']['actions'] == state['actions'] and result['combat']['fighters'] == state['fighters'])
        report = {'room_id':room,'text':text,'passed':passed,'proposal':proposal,
                  'model':result['trace']['response'].get('model'),'duration_ms':result['trace']['duration_ms'],
                  'cost':result['trace']['response'].get('usage',{}).get('cost')}
        print(json.dumps(report,ensure_ascii=False), flush=True)
        if not passed:failed.append(text)
        if args.dialogue:
            state = result['combat'];intent_id = state['intents'][-1]['id']
            state = api(root+'/combat', {'action':'clarify_intent', 'revision':state['revision'], 'intent_id':intent_id,
                'text':'Уточню: я говорю ему на общем языке: «Сдавайся или пожалеешь!»'})
            result = api(root+'/ai/gm/intent', {'revision':state['revision'],'intent_id':intent_id})
            revised = result['combat']['intents'][-1];proposal = revised['analysis']
            passed = (proposal['effect']=='demoralize' and proposal['check']['delivery']=='speech'
                      and proposal['check']['language_penalty']==0 and len(revised['messages'])==4
                      and result['combat']['actions']==state['actions'])
            print(json.dumps({'clarification_passed':passed,'proposal':proposal,'messages':revised['messages'],
                'duration_ms':result['trace']['duration_ms'],'cost':result['trace']['response'].get('usage',{}).get('cost')},ensure_ascii=False),flush=True)
            if not passed:failed.append('clarification')
            state = result['combat']
            state = api(root+'/combat', {'action':'intent','revision':state['revision'], 'text':'Поднять щит', 'review':True, 'requested_action':{'action':'shield'}})
            result = api(root+'/ai/gm/intent', {'revision':state['revision'],'intent_id':state['intents'][-1]['id']})
            proposal = result['combat']['intents'][-1]['analysis']
            passed = proposal['effect']=='shield' and not result['combat']['fighters'][0]['shield'] and result['combat']['actions']==state['actions']
            print(json.dumps({'button_review_passed':passed,'proposal':proposal,'cost':result['trace']['response'].get('usage',{}).get('cost')},ensure_ascii=False),flush=True)
            if not passed:failed.append('button review')
    if failed:raise SystemExit('Failed cases: ' + '; '.join(failed))


if __name__ == '__main__':
    main()
