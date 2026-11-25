// Real free-model playthrough. State reads verify the outcome; all actions use the UI.
import fs from 'node:fs';
const {chromium}=await import(process.env.IIRPG_PLAYWRIGHT_MODULE);
const browser=await chromium.launch({headless:true});
const base=process.env.IIRPG_TEST_URL,out=process.env.IIRPG_GAMEPLAY_OUTPUT;
const report={cases:[]};const save=()=>fs.writeFileSync(out+'.json',JSON.stringify(report,null,2));
function assert(ok,message){if(!ok)throw Error(message);}
try{
 for(const width of JSON.parse(process.env.IIRPG_TEST_WIDTHS||'[1280,390]')){
  const context=await browser.newContext({viewport:{width,height:900},isMobile:width<600,hasTouch:width<600,reducedMotion:'reduce'});
  const page=await context.newPage();page.setDefaultTimeout(150000);
  const entry={width,turns:[],checks:[],errors:[]};report.cases.push(entry);save();
  page.on('pageerror',error=>entry.errors.push(error.message));
  let room,root;
  const press=async locator=>width<600?locator.tap():locator.click();
  const check=(name,ok)=>{entry.checks.push({name,passed:!!ok});save();assert(ok,name);};
  const get=async path=>page.evaluate(async path=>{const response=await fetch(path);if(!response.ok)throw Error('Read '+path+' HTTP '+response.status);return response.json();},path);
  const state=()=>get(root+'/session');
  const hero=async()=>{const s=await state(),r=await get(root+'/roster');return r.characters.find(a=>a.id===s.character_id);};
  const shot=async name=>page.screenshot({path:out+'-'+width+'-'+name+'.png'});
  async function create(scenario,name){
   if(!await page.locator('#games-menu').isVisible())await press(page.locator(width<600?'#show-games':'#games-toggle'));
   await page.locator('#game-scenario option[value="'+scenario+'"]').waitFor({state:'attached'});
   await page.locator('#game-scenario').selectOption(scenario);
   const id=await page.locator('#game-hero option').evaluateAll(options=>options.find(o=>o.textContent.includes('Радан')).value);
   await page.locator('#game-hero').selectOption(id);await page.locator('#game-name').fill(name+' '+width);
   const response=page.waitForResponse(r=>r.url().endsWith('/api/games')&&r.request().method()==='POST'&&r.request().postDataJSON()?.action==='create');
   await press(page.locator('#game-create-button'));const result=await response;assert(result.status()===200,'create game');
   room=(await result.json()).room_id;root='/api/rooms/'+room;
   await page.waitForFunction(room=>new URLSearchParams(location.hash.slice(1)).get('room')===room,room);
   await page.waitForFunction(()=>!document.querySelector('#chats textarea').disabled);
   check('created '+scenario,(await state()).scenario_id===scenario);await shot(scenario);
  }
  async function say(text){
   await press(page.locator('#show-chat'));const input=page.locator('#chats textarea');await input.fill(text);
   const pending=page.waitForResponse(r=>r.url().endsWith(root+'/session')&&r.request().method()==='POST');
   await press(page.locator('#chats .send-button'));const response=await pending;
   const body=await response.json();entry.turns.push({text,http_status:response.status(),history:body.history?.slice(-2),error:body.error});save();
   assert(response.status()===200,'chat: '+JSON.stringify(body.error));
   await page.waitForFunction(()=>!document.querySelector('#chats .gm-thinking'));
   console.log(JSON.stringify({width,text,gm:body.history?.at(-1)?.gm},null,0));
   return state();
  }
  async function setItem(name,value){
   await press(page.locator('#show-inventory'));const select=page.getByRole('combobox',{name:'Расположение: '+name,exact:true});
   const pending=page.waitForResponse(r=>r.url().endsWith(root+'/roster')&&r.request().method()==='POST');
   await select.selectOption(value);const response=await pending;const data=await response.json();
   await page.waitForFunction(()=>!document.querySelector('.roster-search').disabled);
   return {status:response.status(),data};
  }
  try{
   await page.goto(base);await create('crossroads','Торговля через браузер');
   await say('Мара, добрый день. Что у тебя есть для дороги? Сколько стоят дорожная верёвка и медная кружка? Пока ничего не покупаю.');
   check('conversation does not spend coins',(await hero()).coins===300);
   await say('Мара, покупаю одну дорожную верёвку за указанную цену 100 медных. Больше ничего не беру.');
   check('purchase changes actual wallet and inventory',(await hero()).coins===200&&(await hero()).items.some(i=>i.name==='Дорожная верёвка'));
   await say('Мара, расскажи, что знаешь о дороге дальше. Ничего не покупаю и не продаю.');
   await press(page.locator('#show-inventory'));await page.getByText('Дорожная верёвка',{exact:true}).waitFor();
   check('purchase visible in inventory',await page.getByText('Дорожная верёвка',{exact:true}).isVisible());await shot('purchased');
   await say('Мара, продаю тебе только купленную дорожную верёвку.');
   check('sale returns half price',(await hero()).coins===250&&!(await hero()).items.some(i=>i.name==='Дорожная верёвка'));

   await create('watch-post','Дверь и бой через браузер');
   await say('Сторож, здравствуй. Почему ты здесь стоишь и что случилось с калиткой? Пока никуда не иду.');
   await say('Беру железный рычаг, но меч и щит пока остаются у меня в руках.');
   check('full hands cannot pick up another object',(await state()).world.entities.lever.owner==='road'&&!(await state()).pending_check);
   check('stow sword in inventory',(await setItem('Длинный меч','stowed')).status===200);
   check('stow shield in inventory',(await setItem('Деревянный щит','stowed')).status===200);
   await say('Теперь руки свободны. Забираю железный рычаг.');
   check('take actual crowbar',(await hero()).items.some(i=>i.name==='Железный рычаг'));
   check('ready crowbar',(await setItem('Железный рычаг','held')).status===200);
   await say('Силой открываю заевшую калитку железным рычагом, который держу в руке.');
   check('physical attempt offers an actual roll',!!(await state()).pending_check);
   let diceRequests=0;const counter=request=>{if(request.method()==='POST'&&request.url().endsWith('/tools/dice'))diceRequests++;};page.on('request',counter);
   const roll=page.waitForResponse(r=>r.url().endsWith(root+'/tools/dice')&&r.request().method()==='POST');
   const reaction=page.waitForResponse(r=>r.url().endsWith(root+'/session/react')&&r.request().method()==='POST');
   const button=page.getByRole('button',{name:'Бросить кости',exact:true}).last();await button.waitFor({state:'visible'});
   await button.evaluate(button=>{button.click();button.click();});const dice=await roll;assert(dice.status()===201,'dice roll');
   await page.waitForFunction(()=>!document.querySelector('#chats .dice-offer-roll:not(:disabled)'));
   check('double click rolls only once',diceRequests===1);page.off('request',counter);
   check('roll opens persistent gate',(await state()).world.entities.gate.open===true);await shot('gate-open');
   assert((await reaction).status()===200,'narration after roll');
   await page.waitForFunction(()=>!document.querySelector('#chats .gm-thinking'));
   check('stow crowbar',(await setItem('Железный рычаг','stowed')).status===200);
   check('equip sword',(await setItem('Длинный меч','held')).status===200);
   check('equip shield',(await setItem('Деревянный щит','held')).status===200);
   const expectsShield=width>=600;
   if(!expectsShield){check('stow sword before unarmed fight',(await setItem('Длинный меч','stowed')).status===200);check('stow shield before unarmed fight',(await setItem('Деревянный щит','stowed')).status===200);}
   await say(expectsShield?'Начинаю поединок со сторожем: нападаю на него с мечом.':'Начинаю поединок со сторожем: атакую кулаком, меч и щит убраны.');
   check('story starts actual tactical battle',(await get(root+'/combat')).status==='active');
   const first=(await get(root+'/combat')).fighters.find(f=>f.team==='players');
   check('combat uses actual ready shield',first.has_shield===expectsShield);
   check('combat uses actual ready weapon',first.weapon_name===(expectsShield?'Длинный меч':'Безоружная атака'));
   const before=await hero();const during=await setItem('Длинный меч',expectsShield?'stowed':'held');
   check('unsupported mid-combat gear change is clearly rejected',during.status===409&&JSON.stringify((await hero()).items)===JSON.stringify(before.items));
   await press(page.locator('#show-battle'));const frame=page.frameLocator('#combat-frame');
   await frame.locator('.connection.online').waitFor({state:'attached'});
   check('battle chat heading fits its panel',await frame.locator('#battle-chat .chat-header').evaluate(header=>{const title=header.querySelector('h2').getBoundingClientRect(),box=header.getBoundingClientRect();return title.top>=box.top&&title.bottom<=box.bottom;}));
   await shot('battle-active');
   await press(frame.locator('#zoom-out'));await press(frame.locator('#zoom-in'));
   await frame.locator('#auto-npc').check();
   let protectedOnce=!expectsShield,enemyActed=false;
   let gaveEnemyTurn=false;
   for(let step=0;step<24;step++){
    let battle=await get(root+'/combat');if(battle.status!=='active')break;
    const actor=battle.fighters[battle.turn];
    if(actor.team==='enemies'){
     enemyActed=true;
     const deadline=Date.now()+180000;
     // Poll in Node: a promise-returning waitForFunction predicate can be truthy
     // before its asynchronous fetch resolves in some Playwright versions.
     while(battle.status==='active'&&battle.fighters[battle.turn].team==='enemies'){
      assert(Date.now()<deadline,'enemy turn stalled: '+await frame.locator('#auto-npc-status').textContent());
      const error=frame.locator('#action-error');
      assert(!await error.isVisible(),'enemy turn failed: '+await error.textContent());
      await page.waitForTimeout(750);battle=await get(root+'/combat');
     }
     continue;
    }
    let action='end_turn',locator;
    if(!protectedOnce){action='shield';locator=frame.locator('#general-actions [data-command="shield"]');}
    else if(!gaveEnemyTurn){gaveEnemyTurn=true;locator=frame.locator('#general-actions [data-command="end_turn"]');}
    else{
     const foe=battle.fighters.find(f=>f.team==='enemies'&&f.hp>0&&!f.surrendered);
     if(foe){
      await press(frame.locator('.battle-cell[data-position="'+foe.position.join(',')+'"]'));
      await frame.locator('#cell-actions').waitFor();
      const strike=frame.locator('#cell-actions [data-command="strike"]');
      // The menu is loaded asynchronously after selecting a cell.
      await page.waitForFunction(()=>{const note=document.querySelector('#combat-frame')?.contentDocument?.querySelector('#cell-actions-note');return note&&!note.textContent.startsWith('Проверяем');});
      if(await strike.count()&&await strike.isEnabled()){action='strike';locator=strike;}
     }
    }
    if(!locator||!await locator.count()||!await locator.isEnabled()){action='end_turn';locator=frame.locator('#general-actions [data-command="end_turn"]');}
    const reviewed=page.waitForResponse(r=>r.url().endsWith(root+'/ai/gm/intent')&&r.request().method()==='POST',{timeout:180000});
    await press(locator);const result=await reviewed;const body=await result.json();
    entry.turns.push({action,http_status:result.status(),error:body.error});save();assert(result.status()===200,'combat review: '+JSON.stringify(body.error));
    if(action==='shield')protectedOnce=true;
    await page.waitForFunction(()=>!!document.querySelector('#combat-frame')?.contentDocument?.querySelector('.battle-cell:not(:disabled)'));
    const applied=await get(root+'/combat');
    check('reviewed '+action+' is executed',applied.intents.at(-1)?.status==='resolved');
   }
   const final=await get(root+'/combat');check('battle reaches an outcome',final.status==='finished');
   check(expectsShield?'shield used in battle':'stowed shield stays unavailable',expectsShield?protectedOnce:!final.fighters.find(f=>f.team==='players').has_shield);check('enemy got a turn',enemyActed);
   entry.combat_result={winner:final.winner,round:final.round,fighters:final.fighters.map(f=>({name:f.name,hp:f.hp,max_hp:f.max_hp}))};save();await shot('battle-finished');
   await say('Драка закончилась. Как теперь выглядит сторож и ранен ли я? Только спрашиваю, больше не атакую.');
   const after=await state(),h=await hero(),fighter=final.fighters.find(f=>f.character_id===after.character_id);
   check('battle damage persists into character sheet',h.sheet.values.hp_current===fighter.hp);
   await press(page.locator('#show-inventory'));await press(page.getByRole('button',{name:'Лист персонажа',exact:true}).first());await shot('final-sheet');
   check('no JavaScript errors',entry.errors.length===0);
   console.log('PASS browser gameplay '+width);save();
  }catch(error){entry.error=error.message;await shot('failure').catch(()=>{});save();console.log('FAIL '+width+': '+error.message);throw error;}
  finally{await context.close();}
 }
}finally{await browser.close();save();}
