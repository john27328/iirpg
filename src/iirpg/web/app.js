import { MultiplayerMenu } from "/multiplayer.js";
import { HeroMenu } from "/hero-menu.js";
import { createChatClass } from "/chat.js";
import { setupScene } from "/scene.js";
import { InventoryPanel } from "/inventory.js";
import { DebugPanel } from "/debug.js";

const $ = (selector, root = document) => root.querySelector(selector);
const notice = $("#notice");
const connection = $(".connection");
const copyButton = $("#copy-link");
const newButton = $("#new-room");
const isDebug = document.body.dataset.view === "debug";
const roomPattern = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
let room = null;
let generation = 0;
let timer;
let ready = false;
let switching = false;
let accessToken = "";
let identity = {role:"owner"};
let gmBusy=false;
let diceCatalog = null;
const accessGate = $("#access-gate");

function createRequestId() {
  if (typeof crypto.randomUUID === "function") return crypto.randomUUID();
  const bytes = crypto.getRandomValues(new Uint8Array(16));
  bytes[6] = (bytes[6] & 0x0f) | 0x40;
  bytes[8] = (bytes[8] & 0x3f) | 0x80;
  const hex = Array.from(bytes, (byte) => byte.toString(16).padStart(2, "0")).join("");
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}

function readFragment() {
  const hash = location.hash.slice(1);
  return roomPattern.test(hash) ? new URLSearchParams({ room: hash }) : new URLSearchParams(hash);
}

function savedToken() {
  try { return sessionStorage.getItem("iirpg-access-token") || ""; } catch { return ""; }
}

function setToken(token) {
  accessToken = token;
  try { sessionStorage.setItem("iirpg-access-token", token); } catch { /* Optional storage. */ }
}

function roomFragment() {
  const params = new URLSearchParams();
  if (room) params.set("room", room);
  if (accessToken) params.set("access", accessToken);
  return params.toString();
}

function updatePageLinks() {
  if(isDebug&&room)$("#combat-debug-frame").src="/?view=combat&developer=1#"+roomFragment();
  document.querySelectorAll(".page-link, .brand").forEach((link) => {
    link.hash = roomFragment();
  });
}

setToken(readFragment().get("access") || savedToken());

async function request(path, options = {}) {
  const { timeoutMs = 12000, ...fetchOptions } = options;
  const response = await fetch(path, {
    ...fetchOptions,
    headers: {
      "Content-Type": "application/json",
      ...(accessToken ? { Authorization: `Bearer ${accessToken}` } : {}),
      ...options.headers,
    },
    signal: AbortSignal.timeout(timeoutMs),
  });
  const data = await response.json();
  if (!response.ok) {
    const error = new Error(data.error || "Не удалось выполнить запрос.");
    error.status = response.status;
    throw error;
  }
  return data;
}

function setConnection(online) {
  connection.classList.toggle("online", online);
  connection.classList.toggle("offline", !online);
  $("#connection-label").textContent = online ? "На связи" : "Переподключение…";
}

function rememberRoom(value) {
  try { localStorage.setItem("iirpg-room", value); } catch { /* Storage may be disabled. */ }
}

function storedRoom() {
  try { return localStorage.getItem("iirpg-room"); } catch { return null; }
}


class JsonStub {
  constructor() {
    this.key = "second";
    this.element = $("#json-stub-template").content.firstElementChild.cloneNode(true);
    this.list = $(".json-log", this.element);
    this.input = $(".json-input", this.element);
    this.button = $(".json-send", this.element);
    this.newIdButton = $(".json-renew-id", this.element);
    this.diceExampleButton = $(".json-dice-example", this.element);
    this.diceOfferButton = $(".json-dice-offer", this.element);
    this.modeSelect = $(".json-mode", this.element);
    this.mode = this.modeSelect.value;
    this.error = $(".form-error", this.element);
    this.status = $(".json-status", this.element);
    this.reset();
    this.input.addEventListener("input", () => this.update());
    this.modeSelect.addEventListener("change", () => this.switchMode());
    const diceExample = (offer) => {
      const system = diceCatalog.systems.d20 ? "d20" : Object.keys(diceCatalog.systems)[0];
      const definition = diceCatalog.systems[system];
      const payload = this.mode === "command" ? {
        action: offer ? "offer_roll" : "roll",
        ...(offer ? { text: "Чтобы незаметно пройти мимо стражи, проверьте скрытность.", label: "Скрытность" } : {}),
        roll: definition.roll, target: definition.success.target,
        ...(definition.success.operator !== ">=" ? { operator: definition.success.operator } : {}),
        ...(system !== "d20" ? { system } : {}),
      } : { sender: "second", type: offer ? "tool.offer" : "tool.call", tool: "dice.roll",
        ...(offer ? { recipient: "first", text: "Чтобы незаметно пройти мимо стражи, проверьте скрытность." } : {}),
        request_id: createRequestId(), arguments: { system, roll: definition.roll,
          success: definition.success, ...(offer ? { label: "Скрытность" } : {}) } };
      this.input.value = JSON.stringify(payload, null, 2);
      this.commandRetry = null;
      this.initialRaw = "";
      this.error.hidden = true;
      this.status.hidden = true;
      this.update();
    };
    this.diceExampleButton.addEventListener("click", () => diceExample(false));
    this.diceOfferButton.addEventListener("click", () => diceExample(true));
    this.newIdButton.addEventListener("click", () => {
      try {
        if (this.mode === "command") {
          this.parse();
          this.commandRetry = null;
          this.initialRaw = "";
        } else {
          const payload = this.parse();
          payload.request_id = createRequestId();
          this.input.value = JSON.stringify(payload, null, 2);
        }
        this.error.hidden = true;
        this.status.hidden = true;
        this.update();
      } catch (error) { this.showError(error); }
    });
    $("form", this.element).addEventListener("submit", (event) => {
      event.preventDefault();
      this.send();
    });
    $("#chats").append(this.element);
  }

  update() {
    this.input.disabled = !ready;
    this.newIdButton.disabled = !ready || this.busy;
    this.diceExampleButton.disabled = !ready || this.busy || !diceCatalog;
    this.diceOfferButton.disabled = !ready || this.busy || !diceCatalog;
    this.modeSelect.disabled = !ready || this.busy;
    this.newIdButton.textContent = this.mode === "command" ? "Новая отправка" : "Новый request_id";
    this.newIdButton.title = this.mode === "command" ? "Отправить текущую команду как новую" : "Изменить только request_id";
    $("#json-hint", this.element).textContent = this.mode === "command"
      ? "Служебные ID добавляются автоматически. Для новой записи нажмите «Новая отправка»."
      : "Объект отправляется как введён. Повтор с тем же ID не создаёт дубль.";
    this.button.disabled = !ready || this.busy || !this.input.value.trim();
  }

  hasDraft() {
    return this.input.value !== this.initialRaw || Object.entries(this.drafts).some(
      ([mode, draft]) => mode !== this.mode && draft.raw !== draft.initialRaw,
    );
  }

  switchMode() {
    this.drafts[this.mode] = { raw: this.input.value, initialRaw: this.initialRaw, retry: this.commandRetry };
    this.mode = this.modeSelect.value;
    this.loadDraft();
    this.error.hidden = true;
    this.status.hidden = true;
  }

  loadDraft() {
    const draft = this.drafts[this.mode];
    this.input.value = draft.raw;
    this.initialRaw = draft.initialRaw;
    this.commandRetry = draft.retry;
    this.update();
  }

  reset() {
    this.cursor = 0;
    this.hasMore = false;
    this.ids = new Set();
    this.busy = false;
    const command = { action: "say", text: "Перед вами закрытая дверь. Что будете делать?",
      choices: ["Осмотреться", "Открыть дверь", "Уйти"] };
    const raw = JSON.stringify({
      sender: "second", type: "chat.message", request_id: createRequestId(),
      text: "Перед вами закрытая дверь. Что будете делать?",
      choices: ["Осмотреться", "Открыть дверь", "Уйти"],
      visibility: { mode: "all", chat_ids: [] }, metadata: {},
    }, null, 2);
    this.drafts = Object.fromEntries(Object.entries({ command: JSON.stringify(command, null, 2), raw }).map(
      ([mode, value]) => [mode, { raw: value, initialRaw: value, retry: null }],
    ));
    this.loadDraft();
    this.error.hidden = true;
    this.status.hidden = true;
    this.list.replaceChildren($(".empty-state", $("#json-stub-template").content).cloneNode(true));
    this.update();
  }

  parse() {
    let payload;
    try { payload = JSON.parse(this.input.value); } catch {
      throw new Error("Некорректный JSON. Проверьте кавычки, запятые и скобки.");
    }
    if (!payload || typeof payload !== "object" || Array.isArray(payload)) {
      throw new Error("Ожидается один JSON-объект сообщения.");
    }
    return payload;
  }

  showError(error) {
    this.error.textContent = error.message;
    this.error.hidden = false;
  }

  receive(message) {
    if (this.ids.has(message.id)) return;
    this.ids.add(message.id);
    $(".empty-state", this.list)?.remove();
    const nearBottom = this.list.scrollHeight - this.list.scrollTop - this.list.clientHeight < 90;
    const entry = document.createElement("pre");
    entry.className = "json-entry";
    entry.dataset.messageId = message.id;
    entry.textContent = JSON.stringify(message, null, 2);
    const following = Array.from(this.list.children).find((child) => Number(child.dataset.messageId) > message.id);
    this.list.insertBefore(entry, following || null);
    if (nearBottom) this.list.scrollTop = this.list.scrollHeight;
  }

  read(roomId, after = this.cursor) {
    return request(`/api/rooms/${roomId}/messages?chat=${this.key}&after=${after}`);
  }

  applyPage(page) {
    page.messages.forEach((message) => this.receive(message));
    this.cursor = Math.max(this.cursor, page.next_cursor);
    this.hasMore = page.has_more;
  }

  async send() {
    if (!ready || this.busy) return;
    this.error.hidden = true;
    this.status.hidden = true;
    let payload;
    try { payload = this.parse(); } catch (error) { this.showError(error); return; }
    const raw = this.input.value;
    const epoch = generation;
    const targetRoom = room;
    const commandMode = this.mode === "command";
    if (commandMode) {
      const signature = JSON.stringify(payload);
      if (!this.commandRetry || this.commandRetry.room !== targetRoom || this.commandRetry.signature !== signature) {
        this.commandRetry = { room: targetRoom, signature, key: createRequestId() };
      }
    }
    this.busy = true;
    this.update();
    try {
      const path = commandMode ? "gm/commands" : "messages";
      const result = await request(`/api/rooms/${targetRoom}/${path}`, { method: "POST", body: raw,
        ...(commandMode ? { headers: { "Idempotency-Key": this.commandRetry.key } } : {}),
      });
      if (epoch !== generation) return;
      this.initialRaw = raw;
      this.status.textContent = commandMode
        ? `Запись #${result.message.id} принята. Повтор безопасен; для новой записи нажмите «Новая отправка».`
        : `Запись #${result.message.id} принята. Для нового сообщения смените request_id.`;
      this.status.hidden = false;
      setConnection(true);
      try { await refreshChats(targetRoom, epoch); } catch {
        if (epoch === generation) setConnection(false);
      }
    } catch (error) {
      if (epoch !== generation) return;
      this.showError(error);
      if (!error.status || error.status >= 500) setConnection(false);
    } finally {
      if (epoch === generation) { this.busy = false; this.update(); }
    }
  }
}

const context = () => ({room,generation,ready,role:identity.role,characterId:identity.character_id,playerId:identity.player_id,maxPlayers:identity.max_players,gmBusy});
if (isDebug) setupScene($("#scene"), request, context, createRequestId);
const Chat = createChatClass({context,request,createRequestId,diceCatalog:()=>diceCatalog,refreshChats,setConnection,showBattle,roomFragment});
const first = isDebug ? null : new Chat("first", "01", "Ваши действия и ответы ведущего");
const stub = isDebug ? new JsonStub() : null;
const debug = isDebug ? new DebugPanel($("#debug-inspector"), $("#debug-log"), request, context) : null;
const chats = [first, stub, debug].filter(Boolean);
const inventory = isDebug ? null : new InventoryPanel($("#inventory"), request, context, createRequestId);

const heroes = isDebug ? null : new HeroMenu({request,Chat,createId:createRequestId,locked:()=>gameLocked()});
const multiplayer = isDebug ? null : new MultiplayerMenu({request,context,createId:createRequestId,heroes:()=>heroes?.data,locked:()=>gameLocked()});

for (const [id, showInventory] of [["#show-chat", false], ["#show-inventory", true]]) {
  $(id)?.addEventListener("click", () => {
    $("#chats").hidden = showInventory;
    $("#inventory").hidden = !showInventory;
    $("#battle-pane").hidden=true;$("#combat-frame").src="about:blank";$("#show-battle").setAttribute("aria-pressed","false");
    $("#show-chat").setAttribute("aria-pressed", String(!showInventory));
    $("#show-inventory").setAttribute("aria-pressed", String(showInventory));
    if (showInventory) inventory.refresh(true).catch((error) => inventory.showError(error));
  });
}

function showBattle(){
  if(isDebug||!ready)return;
  $('#chats').hidden=true;$('#inventory').hidden=true;$('#battle-pane').hidden=false;
  $('#show-chat').setAttribute('aria-pressed','false');$('#show-inventory').setAttribute('aria-pressed','false');$('#show-battle').setAttribute('aria-pressed','true');
  $('#combat-frame').src='/?view=combat#'+roomFragment();
}
$('#show-battle')?.addEventListener('click',showBattle);

async function refreshChats(roomId, epoch) {
  const pages = await Promise.all(chats.map((chat) => chat.read(roomId)));
  if (epoch !== generation || roomId !== room) return false;
  pages.forEach((page, index) => chats[index].applyPage(page));
  return chats.some((chat) => chat.hasMore);
}

async function poll(epoch) {
  if (epoch !== generation) return;
  let next = document.hidden ? 5000 : 900;
  try {
    const hasMore = await refreshChats(room, epoch);
    if(!isDebug){
      const activity=await request(`/api/rooms/${room}/session/status`);
      if(epoch!==generation)return;
      gmBusy=activity.thinking;
      $('#group-status').hidden=!gmBusy||first.busy;
      $('#group-status').textContent=gmBusy?`Мастер отвечает${activity.actor_name?' персонажу '+activity.actor_name:''}. Следующее сообщение можно пока написать.`:'';
      chats.forEach(chat=>chat.update());
    }
    if (inventory && !$("#inventory").hidden) await inventory.refresh();
    if(heroes?.draftId&&!$("#hero-creator").hidden)await heroes.refreshDraft();
    if (epoch !== generation) return;
    if (hasMore) next = 0;
    setConnection(true);
  } catch {
    if (epoch !== generation) return;
    setConnection(false);
    next = 3000;
  }
  if (epoch === generation) timer = setTimeout(() => poll(epoch), next);
}

async function openRoom(fresh = false) {
  if (switching) return;
  switching = true;
  clearTimeout(timer);
  const epoch = ++generation;
  const previousRoom = room;
  ready = false;
  copyButton.disabled = true;
  newButton.disabled = true;
  chats.forEach((chat) => chat.update());
  inventory?.update();
  notice.hidden = true;
  try {
    const fragment = readFragment();
    if (fragment.get("access")) setToken(fragment.get("access"));
    gmBusy=false;
    identity=await request('/api/identity');
    document.body.dataset.role=identity.role;
    if(!isDebug){$('#player-identity').hidden=identity.role!=='player';$('#player-identity').textContent=identity.role==='player'?`${identity.player_name} · Ваш герой: ${identity.character_name}. Группа путешествует вместе.`:'';}
    let selected = identity.role==='player'?identity.room_id:fresh ? null : fragment.get("room") || storedRoom();
    if (!selected || !roomPattern.test(selected)) {
      selected = (await request("/api/rooms", { method: "POST", body: "{}" })).room_id;
    }
    let initial;
    try {
      initial = await Promise.all(chats.map((chat) => chat.read(selected, 0)));
    } catch (error) {
      if (error.status === 404 && !fresh && !fragment.get("room")) {
        selected = (await request("/api/rooms", { method: "POST", body: "{}" })).room_id;
        initial = await Promise.all(chats.map((chat) => chat.read(selected, 0)));
      } else throw error;
    }
    diceCatalog = await request("/api/tools/dice");
    const roster = inventory ? await request(`/api/rooms/${selected}/roster`) : null;
    first?.configureDice(diceCatalog);
    room = selected;
    if(!isDebug){$('#battle-pane').hidden=true;$('#combat-frame').src='about:blank';$('#chats').hidden=false;$('#inventory').hidden=true;$('#show-chat').setAttribute('aria-pressed','true');$('#show-inventory').setAttribute('aria-pressed','false');$('#show-battle').setAttribute('aria-pressed','false');}
    chats.forEach((chat) => chat.reset());
    initial.forEach((page, index) => chats[index].applyPage(page));
    history.replaceState(null, "", `#${roomFragment()}`);
    updatePageLinks();
    rememberRoom(room);
    if(!isDebug){await request(`/api/rooms/${room}/session`);if(identity.role!=='player')await request('/api/games',{method:'POST',headers:{'Idempotency-Key':createRequestId()},body:JSON.stringify({action:'register',room_id:room})});}
    ready = true;
    if(new URLSearchParams(location.search).get('battle')==='1')showBattle();
    inventory?.reset(roster);
    accessGate.hidden = true;
    copyButton.disabled = false;
    setConnection(true);
    chats.forEach((chat) => chat.update());
    poll(epoch);
  } catch (error) {
    notice.textContent = error.status === 401 ? error.message : `${error.message} Можно попробовать начать новый диалог кнопкой «+».`;
    notice.hidden = false;
    accessGate.hidden = error.status !== 401;
    setConnection(false);
    if (previousRoom && error.status !== 401) {
      room = previousRoom;
      ready = true;
      inventory?.update();
      copyButton.disabled = false;
      chats.forEach((chat) => chat.update());
      poll(epoch);
    }
  } finally {
    switching = false;
    newButton.disabled = false;
  }
}

newButton.addEventListener("click", () => {
  if (inventory?.busy || chats.some((chat) => chat.busy)) return;
  if ((inventory?.hasDraft() || chats.some((chat) => chat.hasDraft())) && !confirm("Начать новый диалог? Неотправленные данные будут очищены.")) return;
  openRoom(true);
});

copyButton.addEventListener("click", async () => {
  try {
    await navigator.clipboard.writeText(location.href);
    copyButton.textContent = "Ссылка скопирована ✓";
    setTimeout(() => { copyButton.textContent = "Скопировать ссылку"; }, 2000);
  } catch {
    notice.textContent = `Ссылка на диалог: ${location.href}`;
    notice.hidden = false;
  }
});

window.addEventListener("hashchange", () => openRoom());
accessGate.addEventListener("submit", (event) => {
  event.preventDefault();
  const token = $("#access-key").value.trim();
  const fragment = readFragment();
  fragment.delete("access");
  history.replaceState(null, "", `#${fragment}`);
  setToken(token);
  $("#access-key").value = "";
  openRoom();
});
openRoom();

let gameOperation=null,gameBusy=false;
function gameLocked(){return switching||gameBusy||inventory?.busy||heroes?.isBusy()||chats.some(c=>c.busy);}
async function showGames(){
 if(isDebug)return;
 try{
  const data=await request('/api/games');
  if(identity.role!=='player'){await heroes.refresh();await multiplayer.refresh();}
  $('#game-save').disabled=!ready||gameLocked();
  $('#game-scenario').replaceChildren(...(data.campaigns||[]).map(c=>new Option(c.name+' · кампания','campaign:'+c.id)),...data.scenarios.map(s=>new Option(s.name,s.id)));
  $('#games-list').replaceChildren(...data.games.map(game=>{
   const row=document.createElement('article');row.className='saved-game';
   const title=document.createElement('strong');title.textContent=game.name+(game.room_id===room?' · текущая':'');
   const info=document.createElement('p');info.textContent=game.saved_at?'Точка сохранения: '+new Date(game.saved_at).toLocaleString('ru-RU'):'Прогресс сохраняется автоматически';row.append(title,info);
   const button=(text,fn)=>{const b=document.createElement('button');b.type='button';b.textContent=text;b.disabled=gameLocked();b.addEventListener('click',fn);row.append(b);return b;};
   button('Продолжить',()=>switchGame(game.room_id));
   if(identity.role!=='player'&&game.has_save)button('Загрузить сохранение',()=>{if(confirm('Вернуться к точке сохранения «'+game.name+'»? Последующие действия будут отменены.'))runGame({action:'load',room_id:game.room_id});});
   if(identity.role!=='player')button('Удалить',()=>{if(confirm('Удалить игру «'+game.name+'» и её историю?'))runGame({action:'delete',room_id:game.room_id});});
   return row;
  }));
 }catch(e){$('#games-status').textContent=e.message;}
}
function switchGame(id,discardConfirmed=false){
 if(gameLocked())return;
 if(!discardConfirmed&&(inventory?.hasDraft()||chats.some(c=>c.hasDraft()))&&!confirm('Переключить игру? Неотправленный черновик будет очищен.'))return;
 $('#games-menu').hidden=true;$('#combat-frame').src='about:blank';
 location.href='/#'+new URLSearchParams({room:id,...(accessToken?{access:accessToken}:{})});
 if(id===room)location.reload();
}
async function runGame(command){
 if(gameLocked()){$('#games-status').textContent='Дождитесь завершения текущего действия.';return;}
 if((command.action==='create'||command.action==='load'||command.action==='delete'&&command.room_id===room)&&(inventory?.hasDraft()||chats.some(c=>c.hasDraft()))&&!confirm('Неотправленный черновик будет очищен. Продолжить?'))return;
 gameBusy=true;$('#games-status').textContent='Выполняю…';
 if(!gameOperation||JSON.stringify(gameOperation.command)!==JSON.stringify(command))gameOperation={command,id:createRequestId()};
 try{
  const result=await request('/api/games',{method:'POST',body:JSON.stringify(command),headers:{'Idempotency-Key':gameOperation.id}});
  gameOperation=null;$('#games-status').textContent=command.action==='save'?'Игра сохранена.':'Готово.';
  if(command.action==='create'||command.action==='load'){gameBusy=false;switchGame(result.room_id,true);return;}
  if(command.action==='delete'&&command.room_id===room){
   const list=await request('/api/games');
   if(list.games.length){gameBusy=false;switchGame(list.games[0].room_id,true);return;}
   ready=false;generation++;clearTimeout(timer);chats.forEach(c=>c.update());$('#combat-frame').src='about:blank';$('#chats').hidden=true;$('#inventory').hidden=true;$('#battle-pane').hidden=true;$('#games-status').textContent='Игра удалена. Начните новую игру.';
  }
 }catch(e){$('#games-status').textContent=e.message;if(e.status)gameOperation=null;}
 finally{gameBusy=false;await showGames();}
}
$('#games-toggle')?.addEventListener('click',()=>{$('#games-menu').hidden=!$('#games-menu').hidden;if(!$('#games-menu').hidden){$('#combat-frame').contentWindow?.postMessage({type:'pause-for-game-menu'},location.origin);showGames();}});
$('#game-save')?.addEventListener('click',()=>{if(ready)runGame({action:'save',room_id:room});});
$('#game-create')?.addEventListener('submit',e=>{e.preventDefault();runGame({action:'create',name:$('#game-name').value,...($('#game-scenario').value.startsWith('campaign:')?{campaign:$('#game-scenario').value.slice(9)}:{scenario:$('#game-scenario').value}),hero_id:$('#game-hero').value,...(multiplayer.party()?{party:multiplayer.party()}:{})});});
