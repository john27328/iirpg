"""Authored NPC assistance activity using the stored Remaster Aid outcomes."""
from .checks import degree
import json
from pathlib import Path

SUPPORT=json.loads((Path(__file__).with_name('rules')/'adventure_support.json').read_text())


def helper_location(state: dict, npc: str) -> str:
    return state.get('npc_locations',{}).get(npc,{'stablehand':'yard','hostess':'hall'}[npc])


def available_help(state: dict) -> bool:
    return (not state.get('book') and state.get('npc_outcomes',{}).get('stablehand','standing')=='standing'
            and helper_location(state,'stablehand')==state['location'])


def wants_chest_help(state: dict, text: str) -> bool:
    recent=' '.join(h.get('player','') for h in state.get('history',[])[-5:])+' '+text
    return available_help(state) and any(w in recent.lower() for w in ('помо','помог','помощ')) and 'сундук' in recent.lower()


def follow(state: dict, destination: str) -> None:
    if state.get('npc_help',{}).get('stablehand') and state.get('npc_outcomes',{}).get('stablehand','standing')=='standing':
        state.setdefault('npc_locations',{})['stablehand']=destination


def prepare_aid(state: dict, skill: str, objective: str, roll_die) -> dict | None:
    if not (skill=='athletics' and objective=='book' and state.get('npc_help',{}).get('stablehand') and available_help(state)):
        return None
    # Authored level-one trained Athletics modifier for this story NPC.
    modifier=SUPPORT['modifier'];natural=roll_die(20);total=natural+modifier;outcome=degree(natural,total,SUPPORT['aid_dc'])
    return {'npc':'stablehand','name':'Конюх','skill':'athletics','natural':natural,'modifier':modifier,'total':total,
            'dc':SUPPORT['aid_dc'],'degree':outcome,'bonus':SUPPORT['bonuses_by_degree'][outcome],
            'source_url':SUPPORT['source_url']}
