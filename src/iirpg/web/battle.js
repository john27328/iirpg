import {createRoomSession,createId} from '/room-session.js';
const q=s=>document.querySelector(s);
const node=(tag,className='',text='')=>{const el=document.createElement(tag);el.className=className;el.textContent=text;return el;};
let battle=null,scene=null,roster=null,ai=null,groups={},busy=false,retry=null,refreshing=false;
let presets=[],selectedPreset=null;
let refreshEpoch=null;
let lastRoom=null,selectedPosition=null,targetId='',objectId='',cellSize=44,draftActor=null;
let chosenHeroes=new Set(),partyKey='',mapKey='',queueKey='',journalKey='',intentsKey='';
let lastActiveId=null;
const side=(f,i)=>f.team||(i===0?'players':'enemies');
const living=f=>f.hp>0&&!f.surrendered;
const active=()=>battle?.fighters?.[battle.turn];
const actorId=()=>active()?.entity_id||`fighter:${battle?.turn}`;
const playerTurn=()=>battle?.status==='active'&&side(active(),battle.turn)==='players';
const enemyTurn=()=>battle?.status==='active'&&!playerTurn();
const canAct=()=>session.ready&&!busy&&!retry&&playerTurn();
const session=createRoomSession(async()=>{
  if(lastRoom!==session.room){lastRoom=session.room;retry=null;chosenHeroes=new Set();battle=null;scene=null;roster=null;selectedPosition=null;selectedPreset=null;targetId='';objectId='';draftActor=null;q('#intent-text').value='';partyKey=mapKey=queueKey=journalKey=intentsKey='';q('#action-error').hidden=true;q('#battle-preparation').open=true;}
  await refresh();
});
const actionNames={start:'Начало боя',strike:'Удар',move:'Перемещение',shield:'Щит',take_cover:'Укрытие',tip_over:'Объект',end_turn:'Конец хода',communicate:'Переговоры',surrender:'Сдача',intent:'Намерение',reject_intent:'Решение ведущего',wait:'Ожидание'};
function showError(message){q('#action-error').textContent=message;q('#action-error').hidden=!message;}
function options(select,entries,desired){
  const fingerprint=JSON.stringify(entries);
  if(select.dataset.options!==fingerprint){select.replaceChildren(...entries.map(([id,label])=>new Option(label,id)));select.dataset.options=fingerprint;}
  if(entries.some(([id])=>id===desired))select.value=desired;
  return select.value;
}
function renderPreparation(){
  if(!roster)return;
  const fingerprint=JSON.stringify([roster.revision,roster.characters.map(c=>c.id)]);
  if(fingerprint!==partyKey){
    const heroes=roster.characters.filter(c=>c.kind==='pc');
    chosenHeroes=new Set([...chosenHeroes].filter(id=>heroes.some(c=>c.id===id)));
    if(!partyKey&&!chosenHeroes.size){const first=heroes.find(c=>c.sheet.values.hp_current>0);if(first)chosenHeroes.add(first.id);}
    q('#party-options').replaceChildren(...heroes.map(hero=>{
      const label=node('label'),check=document.createElement('input');check.type='checkbox';check.value=hero.id;check.checked=chosenHeroes.has(hero.id);check.disabled=hero.sheet.values.hp_current<=0;
      const text=node('span','',hero.name),owner=roster.players.find(p=>p.id===hero.player_id);
      text.append(node('small','',`${owner?.name||'Ваша группа'} · ${hero.sheet.values.hp_current} ОЗ`));
      check.addEventListener('change',()=>{check.checked?chosenHeroes.add(hero.id):chosenHeroes.delete(hero.id);renderControls();});label.append(check,text);return label;
    }));
    if(!heroes.length)q('#party-options').append(node('p','muted','Добавьте персонажей на странице игры.'));
    partyKey=fingerprint;
  }
  if(selectedPreset===null)selectedPreset=battle?.preset_id||(!scene?.map?presets[0]?.id:'')||'';
  selectedPreset=options(q('#battle-preset'),[['','Текущая сцена · свой бой'],...presets.map(p=>[p.id,p.name])],selectedPreset);
  const preset=presets.find(p=>p.id===selectedPreset);
  q('#preset-description').textContent=preset?`${preset.description} При начале боя карта и объекты заменятся заготовкой.`:'Текущая карта и её объекты сохранятся; состав противников выберите ниже.';
  const old=battle?.status==='active'?battle.group_id:preset?.group_id||q('#enemy-group').value;options(q('#enemy-group'),Object.entries(groups).map(([id,g])=>[id,g.name]),old);
  const locked=busy||!!retry||battle?.status==='active';
  q('#party-options').closest('fieldset').disabled=locked;
  q('#enemy-group').disabled=locked||!!preset;q('#battle-preset').disabled=locked;
  q('#start-battle').disabled=locked||!session.ready||!chosenHeroes.size||chosenHeroes.size>4;
}
function renderQueue(){
  const fingerprint=JSON.stringify([battle?.revision,battle?.id]);if(fingerprint===queueKey)return;queueKey=fingerprint;
  q('#round-label').textContent=battle?.fighters?`Раунд ${battle.round}`:'—';
  if(!battle?.fighters){q('#initiative-queue').replaceChildren(node('li','muted','Бой ещё не начат'));return;}
  const order=battle.initiative_order||battle.fighters.map((f,i)=>i).sort((a,b)=>battle.fighters[b].initiative-battle.fighters[a].initiative);
  q('#initiative-queue').replaceChildren(...order.map(i=>{
    const f=battle.fighters[i],current=battle.status==='active'&&i===battle.turn;
    const li=node('li',`${side(f,i)==='enemies'?'enemy':''} ${current?'current':''} ${!living(f)?'defeated':''}`);li.dataset.fighterId=f.entity_id||`fighter:${i}`;if(current)li.setAttribute('aria-current','step');
    const top=node('div','initiative-top');top.append(node('b','',current?'СЕЙЧАС':side(f,i)==='players'?'ГЕРОЙ':'ПРОТИВНИК'),node('b','',String(f.initiative)));
    const name=node('strong','',f.name);name.title=f.name;
    const owner=f.player_name?`${f.player_name} · `:'';
    li.append(top,name,node('span','',`${owner}${f.hp}/${f.max_hp} ОЗ${f.surrendered?' · сдался':!f.hp?' · выбыл':''}`));
    const hp=node('div','hp-track'),fill=document.createElement('i');fill.style.width=`${Math.max(0,100*f.hp/f.max_hp)}%`;hp.append(fill);li.append(hp);return li;
  }));
  const queue=q('#initiative-queue'),current=queue.querySelector('[aria-current=step]');
  if(current&&queue.dataset.current!==current.dataset.fighterId){
    const left=current.offsetLeft-queue.offsetLeft;
    if(left<queue.scrollLeft)queue.scrollLeft=left;
    else if(left+current.offsetWidth>queue.scrollLeft+queue.clientWidth)queue.scrollLeft=left+current.offsetWidth-queue.clientWidth;
    queue.dataset.current=current.dataset.fighterId;
  }
}
function renderActive(){
  const f=active();
  if(lastActiveId!==actorId()){selectedPosition=null;targetId='';objectId='';q('#map-selection').textContent='Выберите цель, объект или клетку для текущего участника.';lastActiveId=actorId();}
  q('#battle-title').textContent=scene?.map?.name||'Поле боя';
  q('#battle-status').textContent=battle?.status==='active'?`${battle.group_name||'Учебный бой'} · ${playerTurn()?'ход вашей группы':'ход противников'}`:battle?.status==='finished'?`Бой окончен · Победитель: ${battle.winner}`:'Выберите героев и противников, чтобы начать.';
  q('#turn-kind').textContent=battle?.status==='finished'?'БОЙ ЗАВЕРШЁН':playerTurn()?'ХОД ВАШЕЙ ГРУППЫ':enemyTurn()?'ХОД ПРОТИВНИКА':'ТЕКУЩИЙ УЧАСТНИК';
  q('#active-name').textContent=f?.name||'Ожидаем начала';
  q('#active-controller').textContent=f?(side(f,battle.turn)==='players'?`Управляет: ${f.player_name||'вы · вся группа'}`:'Управление: ИИ или обычная тактика'):'';
  q('#active-vitals').replaceChildren();q('#action-points').replaceChildren();
  if(!f)return;
  q('#active-vitals').append(node('div','vitals-main',`${f.hp} / ${f.max_hp} ОЗ · КБ ${f.ac}`));
  const tags=node('div','vital-tags');if(f.shield)tags.append(node('span','','Щит поднят'));if(f.cover)tags.append(node('span','',`Укрытие +${f.cover.bonus}`));if(f.surrendered)tags.append(node('span','','Сдался'));q('#active-vitals').append(tags);
  if(battle.status==='active'){
    for(let i=0;i<3;i++)q('#action-points').append(node('span',i<battle.actions?'':'spent','◆'));
    q('#action-points').append(node('span','action-caption',`${battle.actions} действия · штраф атаки ${battle.rules.map[Math.min(battle.attacks,2)]}`));
  }
}
function renderMap(){
  const fingerprint=JSON.stringify([scene?.revision,battle?.revision,battle?.id,selectedPosition,targetId,objectId,cellSize]);if(fingerprint===mapKey)return;mapKey=fingerprint;
  const wrapper=q('#battle-map');
  const scroll={left:wrapper.scrollLeft,top:wrapper.scrollTop};
  const focus=document.activeElement?.closest('.battle-cell')?.dataset.position;
  wrapper.replaceChildren();
  if(!scene?.map){wrapper.append(node('p','map-empty','Карта ещё не загружена. Подготовьте её на странице игры или проведите бой без карты.'));return;}
  q('#map-description').textContent=scene.map.description||'Выберите клетку или цель.';
  const grid=node('div','battle-grid');grid.style.setProperty('--map-cols',scene.map.rows[0].length);grid.style.setProperty('--cell-size',cellSize+'px');
  scene.map.rows.forEach((row,y)=>[...row].forEach((tile,x)=>{
    const here=battle?.fighters?.map((f,i)=>({...f,index:i})).filter(f=>f.position?.[0]===x&&f.position?.[1]===y)||[];
    const object=scene.entities.find(e=>e.kind==='object'&&e.position?.[0]===x&&e.position?.[1]===y);
    const f=here.find(living)||here[0];
    const cell=node('button',`battle-cell ${tile==='#'?'wall':tile==='~'?'difficult':''}`);cell.type='button';cell.dataset.position=`${x},${y}`;
    const parts=[`[${x},${y}]`,...(here.map(f=>`${f.name} · ${f.hp} ОЗ${f.surrendered?' · сдался':''}`)),...(object?[object.name]:[])];
    cell.title=parts.join(' ');cell.setAttribute('aria-label',cell.title);cell.disabled=tile==='#'||busy||!!retry;
    if(selectedPosition?.[0]===x&&selectedPosition?.[1]===y)cell.classList.add('selected');
    if(f?.entity_id===targetId&&living(f))cell.classList.add('targeted');
    if(f?.entity_id===actorId()&&battle?.status==='active')cell.classList.add('active');
    if(f){const token=node('span',`token ${side(f,f.index)==='enemies'?'enemy':''} ${!living(f)?'down':''}`,f.name.split(/\s+/).map(s=>s[0]).slice(0,2).join(''));token.append(node('span','token-hp',String(f.hp)));cell.append(token);}
    else if(object)cell.append(node('span','map-object',object.posture==='overturned'?'▰':'▣'));
    else if(tile==='~')cell.append(node('span','','≈'));
    cell.append(node('span','map-coord',`${x},${y}`));
    cell.addEventListener('click',()=>{
      const current=active();selectedPosition=null;
      if(f&&living(f)){
        if(current&&side(f,f.index)!==side(current,battle.turn)){targetId=f.entity_id;q('#battle-target').value=targetId;q('#map-selection').textContent=`Цель: ${f.name} · ${f.hp}/${f.max_hp} ОЗ. Для атаки нажмите «Удар».`;}
        else q('#map-selection').textContent=`${f.name} · ${side(f,f.index)==='players'?'ваша группа':'противник'}. Текущий участник определяется очередью ходов.`;
      }else if(object){objectId=object.id;q('#battle-object').value=objectId;q('#map-selection').textContent=`Объект: ${object.name}${object.provides_cover?' · предоставляет укрытие':''}.`;}
      else{selectedPosition=[x,y];q('#map-selection').textContent=`Назначение: [${x},${y}] · перемещение стоит одно действие. Доступность пути проверит движок.`;}
      renderMap();renderControls();
    });grid.append(cell);
  }));wrapper.append(grid);wrapper.scrollLeft=scroll.left;wrapper.scrollTop=scroll.top;
  if(focus)grid.querySelector(`[data-position="${focus}"]`)?.focus({preventScroll:true});
  q('#zoom-label').textContent=Math.round(cellSize/44*100)+'%';
}
function renderJournal(){
  const fingerprint=JSON.stringify([battle?.id,battle?.revision]);if(fingerprint===journalKey)return;journalKey=fingerprint;
  const log=q('#battle-log'),scroll=log.scrollTop;const following=q('#follow-log').checked;
  const typed=battle?.journal||[],oldCount=Math.max(0,(battle?.log?.length||0)-typed.length);
  const entries=[...(battle?.log||[]).slice(0,oldCount).map((text,i)=>({id:'old:'+i,text})),...typed];
  const existing=new Set([...log.children].map(el=>el.dataset.entryId).filter(Boolean));
  if(log.dataset.encounter!==(battle?.id||'')){log.replaceChildren();log.dataset.encounter=battle?.id||'';existing.clear();}
  if(entries.length){log.querySelector('.muted')?.remove();}
  for(const e of entries){if(existing.has(e.id))continue;const row=node('article',`battle-log-entry ${e.action||''}`);row.dataset.entryId=e.id;
    const meta=node('div','battle-log-meta');meta.append(node('strong','',actionNames[e.action]||'Событие'),node('span','',e.actor||''));
    if(e.created_at){const time=node('time','',new Date(e.created_at).toLocaleTimeString('ru-RU',{hour:'2-digit',minute:'2-digit',second:'2-digit'}));time.dateTime=e.created_at;meta.append(time);}
    row.append(meta,node('p','',e.text));if(e.source_text&&e.action!=='intent')row.append(node('p','input-help','Из ввода: '+e.source_text));log.append(row);
  }
  if(!entries.length&&!log.children.length)log.append(node('p','muted','Здесь появятся действия, броски и переговоры участников.'));
  log.scrollTop=following?log.scrollHeight:scroll;
}
function renderIntents(){
  const intents=battle?.intents||[],fingerprint=JSON.stringify(intents);
  if(fingerprint!==intentsKey){intentsKey=fingerprint;const root=q('#pending-intents');root.replaceChildren(...intents.slice(-12).reverse().map(i=>{
    const card=node('article',`intent-card ${i.status}`);card.append(node('span','',`${i.actor_name} · раунд ${i.round}`),node('p','',i.text),node('span','',i.status==='pending'?'Ожидает решения · действия не потрачены':i.status==='rejected'?'Отклонено':'Исполнено: '+(actionNames[i.effect]||i.effect)));return card;
  }));if(!intents.length)root.append(node('p','muted','Свободные описания действий сохраняются здесь до решения ведущего.'));}
  const pending=intents.filter(i=>i.status==='pending');q('#intentions-count').textContent=String(pending.length);
  options(q('#resolve-intent'),pending.filter(i=>i.actor_id===actorId()).map(i=>[i.id,`${i.actor_name}: ${i.text.slice(0,70)}`]),q('#resolve-intent').value);
}
function renderControls(){
  renderPreparation();const f=active();const allowed=canAct();
  q('#hero-actions').disabled=!allowed;
  q('#hero-actions').hidden=enemyTurn();q('#npc-actions').hidden=!enemyTurn();
  const targets=(battle?.fighters||[]).map((f,i)=>[f,i]).filter(([f,i])=>living(f)&&active()&&side(f,i)!==side(active(),battle.turn));
  targetId=options(q('#battle-target'),targets.map(([f])=>[f.entity_id||'',f.name]),targetId);
  objectId=options(q('#battle-object'),(scene?.entities||[]).filter(e=>e.kind==='object').map(e=>[e.id,e.name]),objectId);
  document.querySelectorAll('#hero-actions [data-command]').forEach(button=>button.disabled=!allowed||(button.dataset.command!=='end_turn'&&(!battle?.actions||(button.dataset.command==='move'&&!selectedPosition)||(button.dataset.command==='strike'&&!targetId)||(['take_cover','tip_over'].includes(button.dataset.command)&&!objectId))));
  q('#intent-text').disabled=!allowed;
  q('#battle-intent button').disabled=!allowed||!!draftActor&&draftActor!==actorId();
  q('#resolve-action').disabled=!allowed||!q('#resolve-intent').value;
  q('#resolve-intent').disabled=!allowed;q('#resolve-effect').disabled=!allowed;
  const turnNpc=enemyTurn()&&!busy&&!retry&&session.ready;
  q('#ai-step').disabled=!turnNpc||(battle.actions>0&&(!ai?.configured||ai.requests_today>=ai.daily_request_limit));
  q('#ai-step').textContent=battle?.actions===0?'Закончить ход NPC':'Одно действие ИИ';
  q('#npc-actions [data-command]').disabled=!turnNpc;
  q('#ai-status').textContent=ai?`${ai.model} · бесплатно · сегодня ${ai.requests_today}/${ai.daily_request_limit}`+(ai.configured?'':' · ключ OpenRouter не добавлен'):'';
  q('#retry-action').hidden=!retry;q('#retry-action').disabled=busy;
  if(draftActor&&draftActor!==actorId()&&q('#intent-text').value){const name=battle?.fighters?.find(f=>f.entity_id===draftActor)?.name||'предыдущего героя';q('#draft-owner').textContent=`Черновик для ${name}. Дождитесь его хода или очистите поле.`;}else q('#draft-owner').textContent='';
}
function render(){renderActive();renderQueue();renderIntents();renderControls();renderMap();renderJournal();}
async function refresh(){
  if(!session.ready||busy||(refreshing&&refreshEpoch===session.generation))return;const epoch=session.generation,room=session.room;refreshing=true;refreshEpoch=epoch;
  try{const [b,s,r,a,g,templates]=await Promise.all([session.request(`/api/rooms/${room}/combat`),session.request(`/api/rooms/${room}/scene`),session.request(`/api/rooms/${room}/roster`),session.request('/api/ai'),session.request('/api/combat/groups'),session.request('/api/combat/presets')]);
    if(epoch!==session.generation||busy)return;
    const previous=battle?.id;battle=b;scene=s;roster=r;ai=a;groups=g.groups;presets=templates.presets;
    if(!previous&&b.status==='active'){q('#battle-preparation').open=false;chosenHeroes=new Set(b.fighters.filter((f,i)=>side(f,i)==='players').map(f=>f.character_id));}
    if(previous&&previous!==b.id){selectedPosition=null;mapKey=queueKey=journalKey=intentsKey='';}
    render();session.connection(true);q('#notice').hidden=true;
  }catch(e){if(epoch===session.generation){session.connection(false);q('#notice').textContent=e.message;q('#notice').hidden=false;if(e.status===401)q('#access-gate').hidden=false;}}
  finally{if(refreshEpoch===epoch)refreshing=false;}
}
async function submitOperation(operation){
  if(busy||!session.ready)return;busy=true;retry=operation;showError('');renderControls();mapKey='';renderMap();
  try{const result=await session.request(operation.path,{method:'POST',body:operation.body,headers:{'Idempotency-Key':operation.id},timeoutMs:operation.ai?90000:12000});
    if(operation.epoch!==session.generation||operation.room!==session.room)return;
    retry=null;
    if(operation.clearIntent){q('#intent-text').value='';draftActor=null;}
    if(result.combat)battle=result.combat;else if(result.fighters)battle=result;
    if(operation.start)q('#battle-preparation').open=false;
  }catch(e){if(operation.epoch===session.generation){showError(e.message);if(e.status&&e.status<500&&!(operation.ai&&e.status===409))retry=null;}}
  finally{busy=false;if(operation.epoch===session.generation){render();await refresh();}}
}
function send(action,extra={},clearIntent=false){
  if(busy||retry||!session.ready||!battle)return;
  const body={action,revision:battle.revision,...(action==='start'?{}:{actor_id:actorId()}),...extra};
  submitOperation({path:`/api/rooms/${session.room}/combat`,body:JSON.stringify(body),id:createId(),epoch:session.generation,room:session.room,clearIntent,start:action==='start'});
}
function parameters(action){return action==='move'?{position:selectedPosition}:action==='strike'?{target_id:targetId}:['take_cover','tip_over'].includes(action)?{target_id:objectId}:{};}
q('#battle-preset').addEventListener('change',()=>{selectedPreset=q('#battle-preset').value;renderControls();});
q('#start-battle').addEventListener('click',()=>send('start',{character_ids:[...chosenHeroes],group_id:q('#enemy-group').value,...(selectedPreset?{preset_id:selectedPreset}:{})}));
q('#battle-target').addEventListener('change',()=>{targetId=q('#battle-target').value;renderMap();});q('#battle-object').addEventListener('change',()=>{objectId=q('#battle-object').value;});
document.querySelectorAll('[data-command]').forEach(button=>button.addEventListener('click',()=>send(button.dataset.command,parameters(button.dataset.command))));
const draftLabel=node('p','input-help');draftLabel.id='draft-owner';q('#intent-text').after(draftLabel);
q('#intent-text').addEventListener('input',()=>{draftActor=q('#intent-text').value?(draftActor||actorId()):null;renderControls();});
q('#battle-intent').addEventListener('submit',e=>{e.preventDefault();if(!canAct()||draftActor&&draftActor!==actorId())return;const text=q('#intent-text').value;if(!text.trim())return;
  const objectCommand=/^(укры|занять укрытие|опрокин)/i.test(text.trim());send('intent',{text,target_id:objectCommand?objectId:targetId},true);
});
q('#resolve-action').addEventListener('click',()=>{const effect=q('#resolve-effect').value;send('resolve_intent',{intent_id:q('#resolve-intent').value,effect,...parameters(effect)});});
q('#ai-step').addEventListener('click',()=>{
  if(!enemyTurn()||busy||retry)return;if(battle.actions===0){send('end_turn');return;}
  submitOperation({path:`/api/rooms/${session.room}/ai/npc/step`,body:JSON.stringify({revision:battle.revision}),id:createId(),epoch:session.generation,room:session.room,ai:true});
});
q('#retry-action').addEventListener('click',()=>{if(retry)submitOperation(retry);});
q('#zoom-in').addEventListener('click',()=>{cellSize=Math.min(68,cellSize+6);renderMap();});q('#zoom-out').addEventListener('click',()=>{cellSize=Math.max(26,cellSize-6);renderMap();});
q('#resolve-intent').addEventListener('change',renderControls);
setInterval(refresh,1500);session.connect();
