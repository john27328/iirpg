/* Character creation is a transport for the existing chat, not a separate dialogue UI. */
const q = s => document.querySelector(s);
const node = (tag, text = '', className = '') => {const el=document.createElement(tag);el.textContent=text;el.className=className;return el;};

export class HeroMenu {
  constructor({request, Chat, createId, locked}) {
    Object.assign(this,{request,createId,locked,draftId:null,state:null,data:null,busy:false,pending:null});
    this.chat=new Chat('first','02','Опишите героя своими словами. Знать правила необязательно.',{
      id:'creator',container:'#hero-creation-chat',title:'Помощник создания персонажа',badge:'ГЕРОЙ',assistantName:'Помощник',thinking:'Помощник думает…',
      enabled:()=>Boolean(this.draftId&&this.state),
      onUpdate:chat=>{
        q('#hero-creator-close').disabled=chat.busy||this.busy;
        chat.element.querySelectorAll('.save-created-hero').forEach(b=>{b.disabled=chat.busy||this.busy||Number(b.dataset.revision)!==this.state?.revision||Boolean(this.state?.saved_hero_id);});
      },
      read:async after=>{
        const id=this.draftId;
        if(!id)return {messages:[],next_cursor:0,has_more:false};
        const [draft,page]=await Promise.all([request(`/api/characters/drafts/${id}`),request(`/api/rooms/${id}/messages?chat=first&after=${after}`)]);
        return {...page,draft};
      },
      onPage:page=>{if(page.draft)this.state=page.draft;},
      send:async(text,retry)=>{
        retry.creatorCommand ||= {revision:this.state.revision,text};
        await request(`/api/characters/drafts/${this.draftId}`,{method:'POST',body:JSON.stringify(retry.creatorCommand),headers:{'Idempotency-Key':retry.request_id},timeoutMs:90000});
      },
      refresh:()=>this.refreshDraft(),
      renderExtras:(message,item)=>this.preview(message,item)
    });
    q('#hero-new').addEventListener('click',()=>this.newDraft());
    q('#hero-creator-close').addEventListener('click',()=>{
      if(this.chat.busy||this.busy)return;
      q('#hero-creator').hidden=true;this.refresh();
    });
    q('#game-hero').addEventListener('change',()=>this.summary());
  }

  isBusy(){return this.busy||this.chat.busy;}
  status(message){q('#heroes-status').textContent=message;}

  async operation(command){
    if(!this.pending||JSON.stringify(this.pending.command)!==JSON.stringify(command))this.pending={command,id:this.createId()};
    try{
      const result=await this.request('/api/characters',{method:'POST',body:JSON.stringify(command),headers:{'Idempotency-Key':this.pending.id}});
      this.pending=null;return result;
    }catch(e){if(e.status)this.pending=null;throw e;}
  }

  async refresh(selected=null){
    try{
      const data=await this.request('/api/characters');this.data=data;
      const select=q('#game-hero'),previous=selected||select.value;
      select.replaceChildren(...data.heroes.map(h=>new Option(`${h.name} · ${h.progression.name}`,h.id)));
      if(data.heroes.some(h=>h.id===previous))select.value=previous;
      if(!data.heroes.length)select.add(new Option('Создайте героя с помощником',''));
      q('#game-create-button').disabled=!data.heroes.length||this.locked();this.summary();
      q('#hero-library').replaceChildren(...data.heroes.map(hero=>{
        const card=node('article','','hero-library-card');
        card.append(node('strong',hero.name),node('p',`${hero.progression.name} · ${hero.progression.feat.name} · ${hero.sheet.values.hp_max} ОЗ`));
        const choose=node('button','Выбрать');choose.type='button';choose.addEventListener('click',()=>{select.value=hero.id;this.summary();});
        const remove=node('button','Удалить героя');remove.type='button';remove.disabled=this.locked();
        remove.addEventListener('click',async()=>{
          if(this.locked()||!confirm(`Удалить готового героя «${hero.name}»? Персонажи в начатых играх сохранятся.`))return;
          this.busy=true;
          try{await this.operation({action:'delete',id:hero.id});this.status('Герой удалён из списка готовых.');}
          catch(e){this.status(e.message);}finally{this.busy=false;await this.refresh();}
        });card.append(choose,remove);return card;
      }));
      q('#hero-drafts').replaceChildren(...data.drafts.map(d=>{
        const b=node('button','Продолжить создание: '+d.name);b.type='button';b.addEventListener('click',()=>this.openDraft(d.id));return b;
      }));
    }catch(e){this.status(e.message);}
  }

  summary(){
    const hero=this.data?.heroes.find(h=>h.id===q('#game-hero').value),root=q('#hero-choice-summary');root.replaceChildren();
    if(hero)root.append(node('p',hero.progression.features),node('p','Снаряжение: '+hero.items.map(i=>i.name).join(', ')));
  }

  async newDraft(){
    if(this.locked())return;this.busy=true;
    try{const result=await this.operation({action:'new_draft'});await this.openDraft(result.draft_id,true);}
    catch(e){this.status(e.message);}finally{this.busy=false;}
  }

  async openDraft(id,creating=false){
    if(!creating&&this.locked())return;
    if(this.draftId!==id&&this.chat.hasDraft()&&!confirm('Открыть другой разговор? Неотправленный текст будет очищен.'))return;
    try{
      this.draftId=id;this.state=null;this.chat.reset();q('#hero-creator').hidden=false;
      await this.refreshDraft();q('#hero-creator').scrollIntoView({behavior:'smooth',block:'start'});this.chat.input.focus();
    }catch(e){this.status(e.message);}
  }

  async refreshDraft(){
    if(!this.draftId)return;
    const id=this.draftId,page=await this.chat.read(id);
    if(this.draftId!==id)return;
    this.chat.applyPage(page);this.chat.update();
    q('#hero-creator-close').disabled=this.chat.busy||this.busy;
    document.querySelectorAll('.save-created-hero').forEach(b=>{b.disabled=this.chat.busy||this.busy||Number(b.dataset.revision)!==this.state.revision||Boolean(this.state.saved_hero_id);});
  }

  preview(message,item){
    const hero=message.metadata?.character_preview;if(!hero)return;
    const card=node('section','','character-preview');
    card.append(node('h4',hero.name),node('p',`${hero.progression.name} · 1 уровень · ${hero.sheet.values.hp_max} ОЗ`),node('p',hero.sheet.values.ancestry),node('p',hero.progression.feat.name+': '+hero.progression.feat.description),node('p',hero.sheet.values.description),node('p','Снаряжение: '+hero.items.map(i=>i.name).join(', ')));
    if(hero.progression.slots_max)card.append(node('p',`Ячеек I ранга: ${hero.progression.slots_max}. Заклинания: `+Object.values(hero.progression.spells).map(s=>s.name).join(', ')));
    const b=node('button','Сохранить героя','save-created-hero');b.type='button';b.dataset.revision=message.metadata.creator_revision;
    b.addEventListener('click',async()=>{
      if(this.isBusy()||Number(b.dataset.revision)!==this.state.revision)return;
      this.busy=true;b.disabled=true;
      try{const result=await this.operation({action:'save',id:this.draftId,revision:Number(b.dataset.revision)});await this.refresh(result.hero_id);this.status('Герой сохранён и выбран для новой игры.');await this.refreshDraft();}
      catch(e){this.status(e.message);}finally{this.busy=false;await this.refreshDraft();}
    });card.append(b);item.append(card);
  }
}
