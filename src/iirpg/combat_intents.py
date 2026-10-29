"""Small explicit text-command grammar; other prose remains an unexecuted intent."""
import re


def parse_intent(text: str, state: dict, scene: dict | None, selected_target: str | None = None) -> dict | None:
    if not isinstance(text, str) or not text.strip() or len(text) > 1000:
        raise ValueError('Опишите действие текстом до 1000 символов.')
    normalized = ' '.join(text.strip().casefold().split())
    aliases = {
        'щит': 'shield', 'поднять щит': 'shield', 'поднимаю щит': 'shield',
        'конец хода': 'end_turn', 'закончить ход': 'end_turn', 'заканчиваю ход': 'end_turn',
        'укрытие': 'take_cover', 'укрыться': 'take_cover', 'занять укрытие': 'take_cover',
        'опрокинуть': 'tip_over', 'опрокидываю': 'tip_over',
        'удар': 'strike', 'атаковать': 'strike', 'атакую': 'strike', 'ударить': 'strike',
    }
    if normalized in aliases:
        action = aliases[normalized]
        return {'action': action, **({'target_id': selected_target} if action in ('strike', 'take_cover', 'tip_over') else {})}
    move = re.fullmatch(r'(?:идти|иду|переместиться|перемещаюсь)\s+\[?\s*(\d{1,2})\s*[, ]\s*(\d{1,2})\s*\]?', normalized)
    if move:
        return {'action': 'move', 'position': [int(move[1]), int(move[2])]}
    for prefix in ('удар по ', 'атаковать ', 'атакую ', 'ударить '):
        if normalized.startswith(prefix):
            name = normalized[len(prefix):]
            matches = [f for f in state['fighters'] if f['name'].casefold() == name]
            if len(matches) == 1:
                return {'action': 'strike', 'target_id': matches[0]['entity_id']}
    for prefix, action in (('опрокинуть ', 'tip_over'), ('опрокидываю ', 'tip_over'), ('укрыться за ', 'take_cover')):
        if normalized.startswith(prefix) and scene:
            matches = [e for e in scene['entities'] if e['kind'] == 'object' and e['name'].casefold() == normalized[len(prefix):]]
            if len(matches) == 1:
                return {'action': action, 'target_id': matches[0]['id']}
    return None
