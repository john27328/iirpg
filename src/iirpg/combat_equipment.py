"""Snapshot supported ready weapons and shields when a story fight starts.

Only the starter melee profiles are implemented; armor stays part of the
starter class sheet. Equipment changes during combat are still unsupported.
"""
import json
from pathlib import Path

from .progression import RULES
from .roster import InventoryRules

EQUIPMENT = json.loads(Path(__file__).with_name('rules').joinpath('combat_equipment_pf2e.json').read_text())


def loadout(character: dict, inventory_rules: InventoryRules) -> dict:
    ready = set(inventory_rules.summary(character)['ready_item_ids'])
    items = [item for item in character['items'] if item['id'] in ready and not item.get('broken')]
    weapons = [item for item in items if item['category'] == 'weapon']
    profile = EQUIPMENT['unarmed']
    weapon_id = None
    if weapons:
        weapon = weapons[0]
        profile = next((row for row in EQUIPMENT['weapons'] if weapon['name'].strip().casefold()
                        in [name.casefold() for name in row['names']]), None)
        if profile is None:
            raise ValueError('Боевой профиль оружия «' + weapon['name'] +
                             '» пока не поддержан. Выберите меч, кинжал или уберите оружие.')
        weapon_id = weapon['id']
    class_id = character['build']['class_id']
    kit = RULES['classes'][class_id]
    strength = character['attributes']['strength']
    attribute = max(strength, character['attributes']['dexterity']) if profile['finesse'] else strength
    rank = kit['weapon_proficiency'] if class_id in profile['trained_classes'] else 0
    return {'weapon': profile['weapon'], 'weapon_name': profile['name'], 'weapon_item_id': weapon_id,
            'damage_die': profile['damage_die'], 'damage_bonus': strength,
            'attack': attribute + (character['sheet']['values']['level'] + rank if rank else 0),
            'agile': profile['agile'], 'has_shield': any(item['category'] == 'shield' for item in items),
            'sneak_attack': bool(kit['combat'].get('sneak_attack') and (profile['agile'] or profile['finesse']))}
