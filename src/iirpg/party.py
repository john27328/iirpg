"""Shared-session party policy, independent of transport and storage."""
import json
from pathlib import Path

SETTINGS = json.loads((Path(__file__).with_name('rules') / 'multiplayer.json').read_text())
if type(SETTINGS['max_players']) is not int or not 2 <= SETTINGS['max_players'] <= 100:
    raise ValueError('Лимит группы должен быть от 2 до 100.')
if SETTINGS['travel_mode'] != 'together':
    raise ValueError('Пока поддержан совместный переход группы.')


def members(state: dict) -> list[str]:
    return state.get('party_ids') or ([state['character_id']] if state.get('character_id') else [])


def acting_character(state: dict) -> str | None:
    return state.get('pending_check', {}).get('character_id') or state.get('plan', {}).get('character_id') or state.get('character_id')
