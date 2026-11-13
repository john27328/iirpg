"""Opt-in free-provider magic session; never imported by the offline suites.

Run: PYTHONPATH=src python3 release_tests/live_magic.py
Creates an unlisted isolated room; retains traces for inspection and quota accounting.
"""
import json
from pathlib import Path
from uuid import uuid4
from iirpg.server import ChatHTTPServer
from iirpg.dice import DiceTool


def main():
    server = ChatHTTPServer(('127.0.0.1', 0), Path('.local/chat.sqlite3'),
                            dice=DiceTool(roll_die=lambda sides: 1 if sides == 20 else sides))
    room = server.service.store.create_room()
    report = {'room': room, 'model': server.ai.config['model'], 'turns': []}
    def roster(action, **extra):
        return server.roster.execute(room, {'action': action, 'revision': server.roster.read(room)['revision'], **extra}, str(uuid4()))
    def say(text, expected):
        state = server.adventure.read(room)
        state = server.adventure.execute(room, {'revision': state['revision'], 'character_id': actor, 'text': text}, str(uuid4()))
        entry = state['history'][-1]
        report['turns'].append({'text': text, 'entry': entry})
        print(json.dumps(report['turns'][-1], ensure_ascii=False), flush=True)
        assert entry['action'] == expected, (expected, entry)
        return state
    try:
        actor = roster('create_character', data={'name': 'Испытатель арканной магии', 'sheet': {'values': {'perception_other': 30}}})['entity_id']
        roster('configure_build', id=actor, data={'class_id': 'wizard', 'general_feat': 'fleet'})
        say('Создаю свет — применяю свой заговор Light, прикрепляю шар к себе.', 'cast')
        hero = server.roster.read(room)['characters'][0]
        assert hero['build']['light'] and hero['progression']['slots_remaining'] == 2
        say('Применяю свой заговор Detect Magic: есть ли неизвестная магия вокруг?', 'cast')
        say('Атакую хозяйку заклинанием Force Barrage.', 'combat')
        battle = server.combat.read(room)
        assert battle['turn'] == 0
        target = battle['fighters'][1]['entity_id']
        requested = {'action': 'cast_spell', 'spell_id': 'force_barrage', 'spell_actions': 1, 'target_id': target}
        battle = server.combat.execute(room, {'revision': battle['revision'], 'action': 'intent',
            'text': 'Силовой залп, один снаряд в хозяйку', 'review': True, 'target_id': target,
            'position': battle['fighters'][1]['position'], 'requested_action': requested}, str(uuid4()))
        result = server.ai.analyze_intent(room, {'revision': battle['revision'], 'intent_id': battle['intents'][-1]['id'], 'auto_execute': True}, str(uuid4()))
        battle = result['combat']
        assert battle['last_spell']['spell_id'] == 'force_barrage'
        assert server.roster.read(room)['characters'][0]['progression']['slots_remaining'] == 1
        report['button'] = battle['last_spell']
        battle = server.combat.execute(room, {'revision': battle['revision'], 'action': 'cast_spell',
            'spell_id': 'daze', 'spell_actions': 2, 'target_id': target}, str(uuid4()))
        assert battle['status'] == 'finished', battle
        server.adventure.read(room)
        state = say('Жду следующего утра, отдыхаю восемь часов и выполняю дневную подготовку заклинаний.', 'rest')
        hero = server.roster.read(room)['characters'][0]
        assert hero['progression']['slots_remaining'] == 2 and not hero['build']['light']
        assert state['elapsed_hours'] == 24
        report['passed'] = True
    finally:
        Path('.local/magic-playthrough.json').write_text(json.dumps(report, ensure_ascii=False, indent=2))
        server.server_close()
    print('PASS free-model utility magic, initiative, reviewed spell button, damage/save, daily preparation', flush=True)


if __name__ == '__main__':
    main()
