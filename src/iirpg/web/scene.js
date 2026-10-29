export function setupScene(root, request, context, createId) {
  let battle=null, state, busy=false, retry=null, loadedRoom=null, mapTemplates=[];
  root.innerHTML=`<details><summary>Сцена и действия</summary><p>Во время боя клики по карте расходуют действия активного героя. Подойдите к противнику на соседнюю клетку для удара. Укрытие применяется к КБ.</p><label>Персонаж <select class="scene-actor"></select></label><button type="button" data-action="join">Войти в сцену</button><div class="scene-zones"></div><label>Куда идти <select class="scene-zone"></select></label><button type="button" data-action="move">Переместиться</button><label>Объект <select class="scene-target"></select></label><button type="button" data-action="tip_over">Опрокинуть</button><button type="button" data-action="take_cover">Занять укрытие</button><form class="scene-intent"><label>Нестандартное действие<textarea maxlength="1000" placeholder="Опрокину стол и спрячусь за ним" required></textarea></label><button>Предложить действие</button></form><details><summary>Решение ведущего · общий тестовый стенд</summary><p>Разберите намерение на действия. Для «опрокинуть и укрыться» сначала разрешите опрокидывание, затем выполните укрытие.</p><label>Намерение<select class="scene-proposal"></select></label><label>Действие<select class="scene-effect"><option value="tip_over">Опрокинуть выбранный объект</option><option value="take_cover">Занять укрытие у объекта</option><option value="move">Перейти в выбранную зону</option><option value="reject">Отклонить</option></select></label><label>КС Атлетики<input class="scene-dc" type="number" min="0" max="100" value="15"></label><button type="button" data-action="resolve">Применить решение</button></details><p class="scene-error" role="alert"></p><p class="scene-note"></p></details>`;
  const q=s=>root.querySelector(s);
  const tools=document.createElement('div');tools.className='map-tools';
  tools.innerHTML='<h3>Карта</h3><label>Заготовка<select class="map-template"></select></label><button type="button" class="map-load">Загрузить заготовку</button><button type="button" class="map-download">Скачать JSON заготовки</button><button type="button" class="map-schema">Скачать JSON Schema</button><details><summary>Карта от ИИ / импорт JSON</summary><p>Формат iirpg-map-v1: # стена, . пол, ~ трудная местность. Примеры доступны для скачивания.</p><input class="map-file" type="file" accept=".json,application/json"><textarea class="map-json" rows="8" spellcheck="false" placeholder="Вставьте JSON карты"></textarea><button type="button" class="map-import">Проверить и загрузить JSON</button></details><p>Клетка — 5 футов. Нажмите свободную клетку для перемещения, объект для выбора цели, противника для удара в бою.</p><div class="scene-map" aria-label="Карта сцены"></div><p class="map-status"></p>';
  q('.scene-zones').before(tools);
  const battleLink=document.createElement('a');battleLink.href='/battle';battleLink.className='page-link scene-battle-link';battleLink.textContent='Открыть бой →';root.querySelector('details>summary').after(battleLink);
  function renderMap(){
    const wrapper=q('.scene-map');wrapper.replaceChildren();
    if(!state.map){q('.map-status').textContent='Загрузите карту из заготовки или JSON.';return;}
    q('.map-status').textContent=state.map.name+' · '+state.map.description;
    const grid=document.createElement('div');grid.className='map-grid';grid.style.gridTemplateColumns=`repeat(${state.map.rows[0].length}, 38px)`;
    state.map.rows.forEach((row,y)=>[...row].forEach((tile,x)=>{
      const cell=document.createElement('button');cell.type='button';cell.className='map-cell '+(tile==='#'?'map-wall':tile==='~'?'map-difficult':'map-floor');
      const entities=state.entities.filter(e=>e.position?.[0]===x&&e.position?.[1]===y);
      cell.textContent=entities.map(e=>['actor','combatant'].includes(e.kind)?e.name.slice(0,1):e.posture==='overturned'?'▰':'▣').join('')||(tile==='~'?'≈':'');
      cell.title=`[${x},${y}] `+entities.map(e=>e.name+(e.training_hp!==undefined?' · ОЗ '+e.training_hp:'')+(e.cover?' · укрытие':'')+(e.surrendered?' · сдался':'')).join(', ');cell.setAttribute('aria-label',cell.title);cell.disabled=tile==='#'||busy||battle?.status==='active';
      cell.addEventListener('click',()=>{const obj=entities.find(e=>e.kind==='object');const enemy=entities.find(e=>e.kind==='combatant'&&e.training_hp>0&&!e.surrendered);if(obj){q('.scene-target').value=obj.id;q('.map-status').textContent='Выбран объект: '+obj.name;}else send('move_cell',{position:[x,y]});});grid.append(cell);
    }));wrapper.append(grid);
  }
  async function load(document){if(confirm('Заменить карту? Объекты будут сброшены, персонажи перемещены ко входу, ожидающие намерения отменены.'))await send('load_map',document);}
  q('.map-load').addEventListener('click',()=>load({template_id:q('.map-template').value}));
  q('.map-import').addEventListener('click',()=>{try{load({map:JSON.parse(q('.map-json').value)});}catch(e){q('.scene-error').textContent='Неверный JSON: '+e.message;}});
  q('.map-file').addEventListener('change',async e=>{if(e.target.files[0]){if(e.target.files[0].size>60000){q('.scene-error').textContent='Файл слишком большой.';return;}q('.map-json').value=await e.target.files[0].text();}});
  q('.map-schema').addEventListener('click',async()=>{try{const schema=await request('/api/maps/schema');const url=URL.createObjectURL(new Blob([JSON.stringify(schema,null,2)],{type:'application/json'}));const a=document.createElement('a');a.href=url;a.download='iirpg-map-schema.json';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);}catch(e){q('.scene-error').textContent=e.message;}});
  q('.map-download').addEventListener('click',()=>{const doc=mapTemplates.find(m=>m.id===q('.map-template').value);if(!doc)return;const url=URL.createObjectURL(new Blob([JSON.stringify(doc,null,2)],{type:'application/json'}));const a=document.createElement('a');a.href=url;a.download=doc.id+'.json';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);});
  function options(select, entries){const old=select.value;select.replaceChildren(...entries.map(([id,label])=>new Option(label,id)));if([...select.options].some(o=>o.value===old))select.value=old;}
  function render(roster){
    options(q('.scene-actor'),roster.characters.map(a=>[a.id,a.name]));
    options(q('.scene-zone'),state.zones.map(z=>[z.id,z.name]));
    options(q('.scene-target'),state.entities.filter(e=>e.kind==='object').map(e=>[e.id,e.name]));
    options(q('.scene-proposal'),state.proposals.filter(p=>p.status==='pending').map(p=>[p.id,p.text]));
    q('.scene-note').textContent=state.rules.note;
    renderMap();
    q('[data-action=move]').disabled=Boolean(state.map);
    q('.scene-effect option[value=move]').disabled=Boolean(state.map);
    q('.scene-zones').replaceChildren(...state.zones.map(zone=>{const card=document.createElement('article');const title=document.createElement('strong');title.textContent=zone.name;card.append(title);for(const entity of state.entities.filter(e=>e.zone_id===zone.id)){const row=document.createElement('p');row.textContent=entity.name+(entity.posture==='overturned'?' · опрокинут':'')+(entity.cover?' · укрытие +'+entity.cover.bonus:'');card.append(row);}return card;}));
    root.querySelectorAll('button:not(.map-cell)').forEach(b=>b.disabled=busy||battle?.status==='active');q('[data-action=move]').disabled=busy||Boolean(state.map)||battle?.status==='active';
  }
  async function refresh(){const c=context();if(!c.ready||busy||(retry&&loadedRoom===c.room))return;try{const [next,roster,maps,encounter]=await Promise.all([request(`/api/rooms/${c.room}/scene`),request(`/api/rooms/${c.room}/roster`),request("/api/maps"),request(`/api/rooms/${c.room}/combat`)]);battle=encounter;mapTemplates=maps.templates;options(q(".map-template"),mapTemplates.map(m=>[m.id,m.name]));if(c.generation!==context().generation)return;state=next;loadedRoom=c.room;retry=null;battleLink.hash=new URLSearchParams({room:c.room,...(new URLSearchParams(location.hash.slice(1)).get('access')?{access:new URLSearchParams(location.hash.slice(1)).get('access')}:{})}).toString();render(roster);}catch(e){q('.scene-error').textContent=e.message;}}
  async function send(action,extra={}){const c=context();if(!c.ready||busy||!state||loadedRoom!==c.room)return;
    if(battle?.status==='active'){q('.scene-error').textContent='Бой идёт на отдельном экране. Перейдите по ссылке «Открыть бой».';return;}
    const payload={action,revision:state.revision,actor_id:q('.scene-actor').value};
    if(action==='load_map'||action==='move_cell')payload.parameters=extra;
    if(action==='move')payload.target_id=q('.scene-zone').value;
    if(['tip_over','take_cover','resolve'].includes(action))payload.target_id=q('.scene-target').value;
    if(action==='propose')payload.text=q('.scene-intent textarea').value;
    if(action==='resolve'){payload.proposal_id=q('.scene-proposal').value;payload.parameters={effect:q('.scene-effect').value,dc:Number(q('.scene-dc').value)};if(payload.parameters.effect==='move')payload.target_id=q('.scene-zone').value;}
    const body=JSON.stringify(payload);if(retry?.body!==body)retry={body,id:createId()};busy=true;q('.scene-error').textContent='';root.querySelectorAll('button').forEach(b=>b.disabled=true);
    try{await request(`/api/rooms/${c.room}/scene`,{method:'POST',body,headers:{'Idempotency-Key':retry.id}});if(c.generation!==context().generation)return;retry=null;if(action==='propose')q('.scene-intent textarea').value='';}catch(e){if(c.generation===context().generation){q('.scene-error').textContent=e.message;if(e.status&&e.status<500)retry=null;}}finally{busy=false;root.querySelectorAll('button').forEach(b=>b.disabled=false);await refresh();}
  }
  root.addEventListener('click',e=>{if(e.target.dataset.action)send(e.target.dataset.action);});q('.scene-intent').addEventListener('submit',e=>{e.preventDefault();send('propose');});window.addEventListener('iirpg-combat',refresh);setInterval(refresh,2500);refresh();
}
