"""Opt-in browser fixture: real stores and HTTP, deterministic dice, no external AI.

PYTHONPATH=src python3 release_tests/multiplayer_browser_server.py --output .local/multiplayer-browser.json
Run browser_multiplayer.cjs against that file, then stop this fixture with Ctrl-C.
"""
import argparse
import json
from pathlib import Path
import secrets
import tempfile
import time
from uuid import uuid4
from urllib.parse import urlencode
from iirpg.server import ChatHTTPServer
from iirpg.dice import DiceTool


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--port',type=int,default=0);parser.add_argument('--output',type=Path,required=True);args=parser.parse_args()
    with tempfile.TemporaryDirectory() as temporary:
        owner=secrets.token_urlsafe(32)
        server=ChatHTTPServer(('127.0.0.1',args.port),Path(temporary)/'game.sqlite3',access_token=owner,dice=DiceTool(roll_die=lambda sides:min(20,sides)))
        class Client:
            def ready(self):return True
            def complete(self,payload,timeout):
                data=json.loads(payload['messages'][1]['content']);time.sleep(1.2)
                if data.get('role')=='gm':
                    option=next((key for key,value in data['options'].items() if value.get('effect')=='end_turn'),'manual')
                    return {'content':json.dumps({'choice':option,'explanation':'Ход завершается по команде игрока.'})}
                if 'available_actions' in data:
                    return {'content':json.dumps({'choice':next(iter(data['available_actions']))})}
                player=data.get('player','');steps=[]
                if player=='Беру верёвку':steps=[{'rule':'move','target':'store'},{'rule':'take','target':'rope'}]
                elif player=='Пробую открыть сундук':steps=[{'rule':'force_open','target':'chest'}]
                elif player=='Нападаю на хозяйку':steps=[{'rule':'move','target':'hall'},{'rule':'combat','target':'hostess'}]
                reply='Проверка завершена.' if data.get('phase')=='AFTER_ROLL' else 'Мастер услышал: '+player
                return {'content':json.dumps({'intent':'act' if steps else 'question','reply':reply,'steps':steps})}
        server.ai.client=Client()
        heroes=server.games.heroes.list()['heroes']
        game=server.games.execute({'action':'create','scenario':'missing-book','name':'Совместная проверка','party':[{'name':'Первый игрок','hero_id':heroes[0]['id']},{'name':'Второй игрок','hero_id':heroes[1]['id']}]},str(uuid4()))
        fixture={'base':f'http://127.0.0.1:{server.server_port}','room':game['room_id'],'owner_fragment':urlencode({'room':game['room_id'],'access':owner}),'players':game['players']}
        args.output.parent.mkdir(parents=True,exist_ok=True);args.output.write_text(json.dumps(fixture,ensure_ascii=False))
        print('Browser fixture ready at '+fixture['base'],flush=True)
        try:server.serve_forever(poll_interval=.05)
        except KeyboardInterrupt:pass
        finally:server.server_close()

if __name__=='__main__':main()
