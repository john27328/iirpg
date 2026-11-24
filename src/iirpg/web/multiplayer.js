/* Invitation management stays in the owner's session menu. Each guest link is scoped. */
const q = s => document.querySelector(s);
const element = (tag,text='') => { const el=document.createElement(tag);el.textContent=text;return el; };
export class MultiplayerMenu {
  constructor({request,context,createId,heroes,locked}) {
    Object.assign(this,{request,context,createId,heroes,locked,busy:false,data:null});
    q('#game-player-count').addEventListener('input',()=>this.creationRows());
    q('#enable-multiplayer').addEventListener('click',()=>this.change({action:'enable',hero_id:q('#invite-hero').value}));
    q('#invite-player').addEventListener('submit',event=>{event.preventDefault();this.change({action:'add',name:q('#invite-name').value,hero_id:q('#invite-hero').value});});
  }
  templates(select,previous=null){
    select.replaceChildren(...(this.heroes()?.heroes||[]).map(hero=>new Option(`${hero.name} · ${hero.progression.name}`,hero.id)));
    if(previous&&[...select.options].some(o=>o.value===previous))select.value=previous;
  }
  creationRows(){
    const count=Math.max(1,Math.min(this.context().maxPlayers||8,Number(q('#game-player-count').value)||1));
    const old=[...q('#extra-players').children].map(row=>({name:row.querySelector('input').value,hero:row.querySelector('select').value}));
    q('#extra-players').replaceChildren();q('#first-player-name').hidden=count===1;q('#game-player-name').required=count>1;
    for(let i=1;i<count;i++){
      const row=element('div');row.className='player-setup';
      const name=element('label',`Игрок ${i+1}`),input=element('input');input.value=old[i-1]?.name||`Игрок ${i+1}`;input.maxLength=80;input.required=true;
      const hero=element('label','Персонаж'),select=element('select');select.required=true;
      this.templates(select,old[i-1]?.hero||(this.heroes()?.heroes||[])[i%(this.heroes()?.heroes?.length||1)]?.id);
      name.append(input);hero.append(select);row.append(name,hero);q('#extra-players').append(row);
    }
  }
  party(){
    if(Number(q('#game-player-count').value)<=1)return undefined;
    return [{name:q('#game-player-name').value,hero_id:q('#game-hero').value},...[...q('#extra-players').children].map(row=>({name:row.querySelector('input').value,hero_id:row.querySelector('select').value}))];
  }
  async refresh(){
    if(this.context().role==='player')return;
    const ctx=this.context();if(!ctx.ready)return;
    const data=await this.request(`/api/rooms/${ctx.room}/participants`);
    if(ctx.generation!==this.context().generation)return;
    this.data=data;q('#game-player-count').max=data.max_players;
    const previous=q('#invite-hero').value;this.templates(q('#invite-hero'),previous||(this.heroes()?.heroes||[])[1]?.id);
    this.creationRows();this.render();
  }
  render(){
    const list=q('#invitation-links');list.replaceChildren();
    q('#enable-multiplayer').hidden=Boolean(this.data?.players.length);
    q('#invite-player').hidden=!this.data?.players.length;
    q('#invite-name').value=`Игрок ${(this.data?.players.length||0)+1}`;
    q('#invite-add').disabled=this.busy||this.locked()||(this.data?.players.length||0)>=(this.data?.max_players||8);
    for(const player of this.data?.players||[]){
      const row=element('article');row.className='saved-game';row.append(element('strong',`${player.name} · ${player.character_name}`));
      const link=element('a','Открыть ссылку игрока');link.href=location.origin+'/#'+player.fragment;link.target='_blank';link.rel='noopener noreferrer';row.append(link);
      const input=element('input');input.readOnly=true;input.value=link.href;input.setAttribute('aria-label',`Ссылка ${player.name}`);row.append(input);
      const button=(text,fn)=>{const b=element('button',text);b.type='button';b.disabled=this.busy||this.locked();b.addEventListener('click',fn);row.append(b);};
      button('Копировать',async()=>{try{await navigator.clipboard.writeText(link.href);q('#participants-status').textContent=`Ссылка для ${player.name} скопирована.`;}catch{input.focus();input.select();q('#participants-status').textContent='Скопируйте выделенную ссылку.';}});
      button('Заменить ссылку',()=>this.change({action:'rotate',player_id:player.player_id}));
      list.append(row);
    }
  }
  async change(command){
    if(this.busy||this.locked())return;this.busy=true;q('#participants-status').textContent='Обновляю группу…';this.render();
    try{
      if(!this.pending||JSON.stringify(this.pending.command)!==JSON.stringify(command))this.pending={command,id:this.createId()};
      const ctx=this.context();this.data=await this.request(`/api/rooms/${ctx.room}/participants`,{method:'POST',body:JSON.stringify(command),headers:{'Idempotency-Key':this.pending.id}});
      this.pending=null;
      q('#participants-status').textContent='Готово. Передайте каждому его отдельную ссылку.';
    }catch(error){if(error.status)this.pending=null;q('#participants-status').textContent=error.message;}
    finally{this.busy=false;this.render();}
  }
}
