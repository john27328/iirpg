"""Optional Chromium UI check against an isolated local server; AI is stubbed."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import tempfile
from threading import Thread

from iirpg.server import ChatHTTPServer


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--playwright-module',type=Path,required=True)
    parser.add_argument('--check',choices=['creator','mobile'],default='creator')
    args=parser.parse_args()
    module=args.playwright_module.resolve()
    if not module.is_file():parser.error('Install Playwright and specify its index.mjs')
    with tempfile.TemporaryDirectory() as directory:
        server=ChatHTTPServer(('127.0.0.1',0),Path(directory)/'browser.sqlite3')
        class Client:
            def ready(self):return True
            def complete(self,payload,timeout):
                name=payload['response_format']['json_schema']['name']
                if name=='session_verification':return {'content':json.dumps({'ok':True,'issues':[]})}
                if name=='action_plan':return {'content':json.dumps({'intent':'question','voice':'gm','interlocutor':'','reply':'Вы можете исследовать мир и выбирать свои действия. Сообщение получено.','steps':[]})}
                current=json.loads(payload['messages'][1]['content'])['current_proposal']
                return {'content':json.dumps({'reply':'Выбранный перк увеличивает скорость на 5 футов. Характеристики назначаются правилами, а не произвольными числами.','choices':[],'proposal':current})}
        server.ai.client=Client()
        thread=Thread(target=lambda:server.serve_forever(poll_interval=.02),daemon=True);thread.start()
        try:
            environment={**os.environ,'IIRPG_TEST_URL':f'http://127.0.0.1:{server.server_port}','IIRPG_PLAYWRIGHT_MODULE':str(module)}
            script='mobile_ui_browser.mjs' if args.check=='mobile' else 'character_creation_browser.mjs'
            subprocess.run(['node',str(Path(__file__).with_name(script))],env=environment,check=True,timeout=120)
        finally:server.shutdown();server.server_close();thread.join()


if __name__=='__main__':main()
