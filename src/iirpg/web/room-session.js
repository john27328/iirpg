/* Shared room-link conventions for the standalone encounter view. */
export function createRoomSession(onConnected) {
  const pattern=/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
  const read=()=>pattern.test(location.hash.slice(1))?new URLSearchParams({room:location.hash.slice(1)}):new URLSearchParams(location.hash.slice(1));
  const stored=(storage,key)=>{try{return window[storage].getItem(key)||'';}catch{return '';}};
  let token=read().get('access')||stored('sessionStorage','iirpg-access-token'), room=null, generation=0, ready=false;
  const remember=(storage,key,value)=>{try{window[storage].setItem(key,value);}catch{/* Optional storage. */}};
  const q=s=>document.querySelector(s);
  function setToken(value){token=value.trim();remember('sessionStorage','iirpg-access-token',token);}
  setToken(token);
  function fragment(){return new URLSearchParams({...(room?{room}:{}),...(token?{access:token}:{})}).toString();}
  function link(path){return path+'#'+fragment();}
  function updateLinks(){document.querySelectorAll('.page-link,.brand').forEach(a=>a.hash=fragment());}
  function connection(online){q('.connection').classList.toggle('online',online);q('.connection').classList.toggle('offline',!online);q('#connection-label').textContent=online?'На связи':'Нет связи';}
  async function request(path,options={}){
    const {timeoutMs=12000,...fetchOptions}=options;
    const response=await fetch(path,{...fetchOptions,headers:{'Content-Type':'application/json',...(token?{Authorization:'Bearer '+token}:{}),...options.headers},signal:AbortSignal.timeout(timeoutMs)});
    const data=await response.json();
    if(!response.ok){const error=new Error(data.error||'Не удалось выполнить запрос.');error.status=response.status;throw error;}
    return data;
  }
  async function connect(){
    const epoch=++generation;ready=false;q('#copy-link').disabled=true;q('#connection-label').textContent='Подключение…';q('.connection').classList.remove('online');
    try{
      const params=read();if(params.get('access'))setToken(params.get('access'));
      let selected=params.get('room')||stored('localStorage','iirpg-room');
      if(!pattern.test(selected))selected=(await request('/api/rooms',{method:'POST',body:'{}'})).room_id;
      await request(`/api/rooms/${selected}/combat`);
      if(epoch!==generation)return;
      room=selected;ready=true;remember('localStorage','iirpg-room',room);
      history.replaceState(null,'','#'+fragment());updateLinks();
      q('#notice').hidden=true;q('#access-gate').hidden=true;q('#access-key').value='';q('#copy-link').disabled=false;
      await onConnected();
    }catch(error){if(epoch!==generation)return;connection(false);q('#notice').textContent=error.message;q('#notice').hidden=false;q('#access-gate').hidden=error.status!==401;}
  }
  q('#access-gate').addEventListener('submit',e=>{e.preventDefault();setToken(q('#access-key').value);history.replaceState(null,'','#'+new URLSearchParams({...(read().get('room')?{room:read().get('room')}:{}),access:token}));connect();});
  q('#copy-link').addEventListener('click',async()=>{try{await navigator.clipboard.writeText(location.origin+link('/battle'));q('#copy-link').textContent='Ссылка скопирована';}catch{q('#notice').textContent='Ссылка на эту комнату находится в адресной строке.';q('#notice').hidden=false;}});
  window.addEventListener('hashchange',connect);
  return {get room(){return room;},get generation(){return generation;},get ready(){return ready;},request,connect,link,updateLinks,connection};
}

export function createId(){
  if(crypto.randomUUID)return crypto.randomUUID();
  const bytes=crypto.getRandomValues(new Uint8Array(16));bytes[6]=(bytes[6]&15)|64;bytes[8]=(bytes[8]&63)|128;
  const hex=Array.from(bytes,b=>b.toString(16).padStart(2,'0')).join('');return `${hex.slice(0,8)}-${hex.slice(8,12)}-${hex.slice(12,16)}-${hex.slice(16,20)}-${hex.slice(20)}`;
}
