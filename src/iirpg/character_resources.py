"""Write resource changes through an existing transaction, preserving character identity/items."""
import json


def save_character(connection, room: str, actor: dict) -> None:
    body = {k: v for k, v in actor.items() if k not in ('id', 'items', 'summary', 'sheet_statistics', 'progression', 'stats', 'ranks', 'abilities', 'aid_skills')}
    connection.execute('UPDATE characters SET body=? WHERE room_id=? AND id=?',
                       (json.dumps(body, ensure_ascii=False), room, actor['id']))
    connection.execute('UPDATE room_rosters SET revision=revision+1 WHERE room_id=?', (room,))


def sync_fighters(connection, room: str, fighters: list, roster: dict) -> None:
    for fighter in fighters:
        actor = next((a for a in roster['characters'] if a['id'] == fighter.get('character_id')), None)
        if not actor or not actor.get('build'):
            continue
        build = fighter.get('build', actor['build'])
        if actor['build'] != build or actor['sheet']['values']['hp_current'] != fighter['hp']:
            actor['build'] = build
            actor['sheet']['values']['hp_current'] = fighter['hp']
            save_character(connection, room, actor)
