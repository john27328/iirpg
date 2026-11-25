"""Authorization for player invitation links; owner credentials retain existing APIs."""
import re
from urllib.parse import parse_qs


class PlayerAccess:
    def __init__(self, server):
        self.server = server

    def authorize(self, principal: dict, method: str, path: str, query: str, body) -> None:
        if path == '/api/identity' and method == 'GET': return
        if method == 'GET' and path in ('/api/games','/api/tools/dice','/api/ai','/api/combat/groups','/api/combat/presets','/api/maps','/api/maps/schema'):
            return
        match = re.fullmatch(r'/api/rooms/([0-9a-f-]{36})/(.+)', path)
        if not match or match[1] != principal['room_id']:
            raise PermissionError('Эта ссылка даёт доступ только к вашему игровому сеансу.')
        route = match[2]
        if method == 'GET' and route in ('session','session/status','roster','combat','scene'):
            return
        if method == 'GET' and route == 'messages':
            if parse_qs(query).get('chat', [''])[0] != 'first':
                raise PermissionError('Доступен общий игровой чат.')
            return
        if method == 'GET' and route == 'combat/actions': return
        if method != 'POST': raise PermissionError('Это действие доступно владельцу сеанса.')
        command = body()
        if route in ('session','session/adventure'):
            if command.get('character_id') != principal['character_id']:
                raise PermissionError('Можно действовать только своим персонажем.')
            return
        if route in ('session/cancel','session/react','tools/dice'):
            offer = self.server.service.store.event(principal['room_id'], command.get('offer_id',''))
            if offer['type'] != 'tool.offer' or not offer.get('metadata',{}).get('session_check'):
                raise PermissionError('Выберите предложенную проверку своего персонажа.')
            owner = offer['metadata'].get('character_id')
            if not owner:
                with self.server.service.store._connection() as c:
                    state = self.server.adventure._load(c,principal['room_id'])
                owner = state.get('character_id')
            if owner != principal['character_id']:
                raise PermissionError('Эту проверку бросает другой игрок.')
            if route == 'tools/dice' and command.get('sender') != 'first':
                raise PermissionError('Нельзя выдавать себя за ведущего.')
            return
        if route == 'roster':
            if command.get('action') != 'update_item' or command.get('character_id') != principal['character_id'] or not isinstance(command.get('data'),dict) or set(command['data'])-{'state','hands'}:
                raise PermissionError('Можно менять только положение своего снаряжения. Созданием и изменением героев управляет владелец.')
            with self.server.service.store._connection() as c:
                found = c.execute('SELECT 1 FROM inventory_items WHERE room_id=? AND character_id=? AND id=?',
                                  (principal['room_id'],principal['character_id'],command.get('id'))).fetchone()
            if not found: raise PermissionError('Это снаряжение другого персонажа.')
            return
        if route in ('combat','ai/gm/intent','ai/npc/step'):
            battle = self.server.combat.read(principal['room_id'])
            if battle.get('status') != 'active': raise PermissionError('Сейчас нет активного боя. Начните столкновение через игровой чат.')
            fighter = battle['fighters'][battle['turn']]
            if route == 'ai/npc/step':
                if fighter.get('team') != 'enemies': raise PermissionError('Сейчас ход игрока.')
                return
            if route=='combat' and fighter.get('team')=='enemies' and battle.get('actions')==0 and command.get('action')=='end_turn' and command.get('actor_id')==fighter.get('entity_id'):
                return
            if fighter.get('character_id') != principal['character_id']:
                raise PermissionError('Сейчас ход другого участника.')
            if route == 'combat':
                if command.get('action') not in ('move','strike','shield','end_turn','demoralize','cast_spell','intent','clarify_intent','accept_intent','reject_intent'):
                    raise PermissionError('Решения ведущего недоступны игроку.')
                if command.get('actor_id') != principal['character_id']:
                    raise PermissionError('Можно действовать только своим персонажем.')
            else:
                intent = next((i for i in battle.get('intents',[]) if i['id']==command.get('intent_id')),None)
                if not intent or intent['actor_id'] != principal['character_id']:
                    raise PermissionError('Это намерение другого участника.')
            return
        raise PermissionError('Это действие доступно владельцу сеанса.')

    @staticmethod
    def session_view(state: dict) -> dict:
        """Player clients receive public progress, not GM knowledge or frozen private effects."""
        value = {key: state[key] for key in ('revision','location','location_id','place','places','character_id','party_ids','active_combat','hero_hps','history','adventure') if key in state}
        if state.get('pending_check'):
            value['pending_check'] = {key:v for key,v in state['pending_check'].items()
                                      if key in ('skill','modifier','dc','offer_id','roll_request_id','character_id','actor_name','player_id','description','label')}
        return value
