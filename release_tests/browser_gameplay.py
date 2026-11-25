"""Opt-in real free-model playthrough using browser controls on an isolated game server."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
from threading import Thread
from uuid import uuid4

from iirpg.server import ChatHTTPServer
from iirpg.dice import DiceTool


def main() -> None:
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live',action='store_true',required=True)
    parser.add_argument('--playwright-module',type=Path,required=True)
    parser.add_argument('--widths',type=int,nargs='+',default=[1280,390])
    parser.add_argument('--model',help='Override both configured models with this free model')
    parser.add_argument('--max-requests',type=int,default=80)
    args=parser.parse_args()
    if args.model and not(args.model.endswith(':free') or args.model=='openrouter/free'):parser.error('Only free models allowed')
    if not 1<=args.max_requests<=100:parser.error('Use 1..100 requests')
    module=args.playwright_module.resolve()
    if not module.is_file():parser.error('Specify installed Playwright index.mjs')
    output=Path('.local')/('gameplay-browser-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'-'+uuid4().hex[:8])
    output.parent.mkdir(parents=True,exist_ok=True)
    server=ChatHTTPServer(('127.0.0.1',0),output.with_suffix('.sqlite3'),dice=DiceTool(roll_die=lambda sides:min(sides,16)))
    if args.model:server.ai.config.update(model=args.model,session_model=args.model)
    for model in (server.ai.config['model'],server.ai.config['session_model']):
        if not(model.endswith(':free') or model=='openrouter/free'):
            server.server_close();parser.error('Configured models must be free')
    server.ai.config['daily_request_limit']=args.max_requests
    if not server.ai.client.ready():server.server_close();parser.error('Free AI key not configured')
    thread=Thread(target=lambda:server.serve_forever(poll_interval=.02),daemon=True);thread.start()
    report={'model':server.ai.config['session_model'],'npc_model':server.ai.config['model'],'limitations':['Chromium desktop and phone emulation; no physical phone.','Fixed d20=16 and maximum damage dice; checks behavior, not combat balance.','All mutations use browser controls. API reads verify canonical state.'],'widths':args.widths}
    report['source_hashes']={name:hashlib.sha256((Path('src/iirpg')/name).read_bytes()).hexdigest() for name in ('adventure.py','session_context.py','story_narration.py','combat.py','combat_equipment.py','rules/combat_equipment_pf2e.json','progression.py','web/chat.js','web/battle.js','web/battle.css','web/inventory.js')}
    try:
        env={**os.environ,'IIRPG_TEST_URL':f'http://127.0.0.1:{server.server_port}',
             'IIRPG_PLAYWRIGHT_MODULE':str(module),'IIRPG_GAMEPLAY_OUTPUT':str(output),'IIRPG_TEST_WIDTHS':json.dumps(args.widths)}
        result=subprocess.run(['node',str(Path(__file__).with_name('gameplay_browser.mjs'))],env=env,timeout=900)
        report['exit_code']=result.returncode
    finally:
        with server.service.store._connection() as c:traces=[json.loads(r[0]) for r in c.execute('SELECT body FROM ai_requests ORDER BY started')]
        report['traces']=[{k:t.get(k) for k in ('role','status','duration_ms','error','request','response')} for t in traces]
        report['requests']=server.ai.status()['requests_today']
        report['reported_cost']=sum((t.get('response',{}).get('usage') or {}).get('cost',0) or 0 for t in traces)
        output.with_suffix('.ai.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
        server.shutdown();server.server_close();thread.join()
    print('Report:',output,'Requests:',report['requests'],'Cost:',report['reported_cost'],flush=True)
    if report['exit_code']:raise SystemExit(report['exit_code'])


if __name__=='__main__':main()
