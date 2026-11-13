"""Configured starter builds and daily resources, independent of HTTP and AI."""
from copy import deepcopy
import json
from pathlib import Path

RULES = json.loads((Path(__file__).with_name('rules') / 'progression_pf2e.json').read_text())


def normalize_build(value: object) -> dict:
    if not isinstance(value, dict) or set(value) - {'class_id', 'general_feat', 'spent', 'light'}:
        raise ValueError('Некорректная сборка персонажа.')
    if not isinstance(value.get('class_id'), str) or value['class_id'] not in RULES['classes']:
        raise ValueError('Неизвестный класс.')
    if not isinstance(value.get('general_feat'), str) or value['general_feat'] not in RULES['feats']:
        raise ValueError('Неизвестный общий перк.')
    spent = value.get('spent', 0)
    if type(spent) is not int or not 0 <= spent <= 2 or type(value.get('light', False)) is not bool:
        raise ValueError('Некорректные ресурсы персонажа.')
    return {**value, 'spent': spent, 'light': value.get('light', False)}


def configure(character: dict, data: dict, sheet_rules) -> None:
    if set(data) != {'class_id', 'general_feat'}:
        raise ValueError('Выберите класс и один общий перк.')
    build = normalize_build({**character.get('build', {}), **data})
    kit = RULES['classes'][build['class_id']]
    old = character['sheet']['values']
    if old.get('level', 1) != 1:
        raise ValueError('Стартовая сборка рассчитана на первый уровень.')
    missing = max(0, old.get('hp_max', 0) - old.get('hp_current', 0))
    values = {**{k:v for k,v in old.items() if k in sheet_rules.config['profiles']['pc']['fields']},
              **deepcopy(kit['sheet']), 'level': 1, 'class_name': kit['name'],
              'ancestry': 'Человек · универсальное наследие', 'speed': 25,
              'hp_max': 8 + kit['hp'] + kit['attributes']['constitution']}
    values['hp_current'] = max(0, values['hp_max'] - missing)
    character['attributes'] = deepcopy(kit['attributes'])
    character['sheet'] = sheet_rules.normalize({'profile': 'pc', 'values': values}, character['kind'])
    character['build'] = build


def describe(character: dict) -> dict | None:
    build = character.get('build')
    if not build:
        return None
    kit = RULES['classes'][build['class_id']]
    feat = RULES['feats'][build['general_feat']]
    return {'class_id': build['class_id'], 'name': kit['name'], 'features': kit['description'],
            'feat': feat, 'slots_remaining': max(0, kit.get('slots', 0) - build['spent']),
            'slots_max': kit.get('slots', 0), 'light': build['light'],
            'spells': {key: RULES['spells'][key] for key in kit.get('spells', [])}}


def combat_profile(character: dict) -> dict:
    build = character.get('build')
    if not build:
        return {}
    kit = RULES['classes'][build['class_id']]
    feat = RULES['feats'][build['general_feat']]
    level = character['sheet']['values']['level']
    if level != 1:
        raise ValueError('Автоматические классовые сборки пока поддерживают первый уровень.')
    return {**deepcopy(kit['combat']), 'build': deepcopy(build),
            'attack': character['attributes'][kit['attack_attribute']] + level + kit['weapon_proficiency'],
            'damage_bonus': character['attributes']['strength'],
            'spell_dc': 10 + level + 2 + character['attributes']['intelligence'],
            'speed': character['sheet']['values']['speed'] + feat.get('speed', 0),
            'initiative_bonus': feat.get('initiative', 0)}


def rest(character: dict) -> str:
    """Eight hours of rest, then daily preparation; safe-place policy belongs to the scenario."""
    if character.get('build'):
        character['build'].update(spent=0, light=False)
    values = character['sheet']['values']
    healed = max(1, character['attributes']['constitution']) * values.get('level', 1)
    values['hp_current'] = min(values['hp_max'], values['hp_current'] + healed)
    return 'Прошли сутки, включая 8 часов отдыха и час дневной подготовки. Восстановлены дневные ячейки; ОЗ: ' + str(values['hp_current']) + '/' + str(values['hp_max']) + '.'
