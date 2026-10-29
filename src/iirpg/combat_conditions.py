"""Condition adjustments to implemented checks and DCs."""


def status_penalty(fighter: dict) -> int:
    return max(0, fighter.get('frightened', 0))


def armor_class(state: dict, fighter: dict) -> int:
    return fighter['ac'] - status_penalty(fighter) + max(
        state['rules']['shield_bonus'] if fighter['shield'] else 0,
        (fighter.get('cover') or {}).get('bonus', 0))
