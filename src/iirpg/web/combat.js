export function setupCombat(root, request, context, createId) {
  let state=null, busy=false, retry=null;
  root.innerHTML='<details><summary>Учебный бой PF2e</summary><p>Меч и щит, экспертное владение. ОЗ листа сохраняются; при 0 ОЗ участник выбывает. На загруженной карте учитываются позиции, стены, движение и укрытие. Реакции пока не моделируются.</p><select aria-label="Участник боя"></select><button type="button" data-action="start">Начать бой</button><div class="combat-state"></div><button type="button" data-action="strike">Удар мечом</button><button type="button" data-action="shield">Поднять щит</button><button type="button" data-action="end_turn">Закончить ход</button><button type="button" data-action="enemy_turn">Ход противника</button><pre class="combat-log"></pre><p class="combat-error" role="alert"></p></details>';
  const select=root.querySelector('select'), error=root.querySelector('.combat-error');
  function render(){
    root.querySelectorAll('button').forEach(b=>b.disabled=busy||!state||(b.dataset.action==='start'?state.status==='active':state.status!=='active'||(b.dataset.action==='enemy_turn'?state.turn!==1:state.turn!==0)));
    root.querySelector('.combat-state').textContent=state?.fighters?state.fighters.map(f=>`${f.name}: ${f.hp}/${f.max_hp} ОЗ`).join(' · ')+(state.status==='finished'?` · Победитель: ${state.winner}`:` · Раунд ${state.round}, ход ${state.fighters[state.turn].name}, действий ${state.actions}`):'Выберите героя для спарринга.';
    root.querySelector('.combat-log').textContent=state?.log?.join('\n')||'';
  }
  let loadedRoom=null;
  async function refresh(){
    const c=context();if(!c.ready||busy||(retry&&loadedRoom===c.room))return;
    try{
      const [battle,roster]=await Promise.all([request(`/api/rooms/${c.room}/combat`),request(`/api/rooms/${c.room}/roster`)]);
      if(c.generation!==context().generation)return;
      const choice=select.value;
      select.replaceChildren(...roster.characters.filter(a=>a.kind==='pc').map(a=>new Option(a.name,a.id)));
      if([...select.options].some(o=>o.value===choice))select.value=choice;
      if(loadedRoom!==c.room){retry=null;error.textContent='';loadedRoom=c.room;}
      state=battle;render();
    }catch(e){error.textContent=e.message;}
  }
  root.addEventListener('click',async e=>{
    const action=e.target.dataset.action;if(!action||busy||!state)return;
    const c=context(),body=JSON.stringify({action,revision:state.revision,...(action==='start'?{character_id:select.value}:{})});
    if(retry?.body!==body)retry={body,id:createId()};
    busy=true;render();error.textContent='';
    try{
      const result=await request(`/api/rooms/${c.room}/combat`,{method:'POST',body,headers:{'Idempotency-Key':retry.id}});
      if(c.generation!==context().generation)return;
      state=result;retry=null;
    }catch(e){error.textContent=e.message;if(e.status&&e.status<500)retry=null;}
    finally{busy=false;render();await refresh();window.dispatchEvent(new Event('iirpg-combat'));}
  });
  window.addEventListener('iirpg-combat',refresh);setInterval(refresh,2500);refresh();
}
