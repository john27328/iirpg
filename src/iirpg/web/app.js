const $ = (selector, root = document) => root.querySelector(selector);
const notice = $("#notice");
const connection = $(".connection");
const copyButton = $("#copy-link");
const newButton = $("#new-room");
const roomPattern = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
let room = null;
let generation = 0;
let timer;
let ready = false;
let switching = false;
let accessToken = "";
const accessGate = $("#access-gate");

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

setToken(readFragment().get("access") || savedToken());

async function request(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: {
      "Content-Type": "application/json",
      ...(accessToken ? { Authorization: `Bearer ${accessToken}` } : {}),
      ...options.headers,
    },
    signal: AbortSignal.timeout(12000),
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

class Chat {
  constructor(key, number, subtitle) {
    this.key = key;
    this.cursor = 0;
    this.hasMore = false;
    this.busy = false;
    this.retry = null;
    this.ids = new Set();
    this.element = $("#chat-template").content.firstElementChild.cloneNode(true);
    this.element.classList.add(key);
    $("h2", this.element).textContent = `Чат ${number}`;
    $(".avatar", this.element).textContent = number;
    $(".chat-number", this.element).textContent = `${number} / 02`;
    $(".chat-subtitle", this.element).textContent = subtitle;
    this.list = $(".message-list", this.element);
    this.input = $("textarea", this.element);
    this.audience = $(".visibility-select", this.element);
    this.audience.id = `visibility-${key}`;
    $(".visibility-label", this.element).htmlFor = this.audience.id;
    this.input.id = `message-${key}`;
    const label = $("label.sr-only", this.element);
    label.htmlFor = this.input.id;
    label.textContent = `Сообщение от чата ${number}`;
    this.button = $(".send-button", this.element);
    this.error = $(".form-error", this.element);
    this.input.addEventListener("input", () => this.update());
    this.input.addEventListener("keydown", (event) => {
      if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
        event.preventDefault();
        if (!this.busy && ready) this.element.querySelector("form").requestSubmit();
      }
    });
    $("form", this.element).addEventListener("submit", (event) => {
      event.preventDefault();
      this.send();
    });
    $("#chats").append(this.element);
  }

  update() {
    this.input.disabled = !ready || this.busy;
    this.audience.disabled = !ready || this.busy;
    this.button.disabled = !ready || this.busy || !this.input.value.trim();
    $(".character-count", this.element).textContent = `${Array.from(this.input.value).length} / 2000`;
  }

  reset() {
    this.ids.clear();
    this.cursor = 0;
    this.hasMore = false;
    this.retry = null;
    this.busy = false;
    this.input.value = "";
    this.audience.value = "all";
    this.error.hidden = true;
    this.list.replaceChildren($(".empty-state", $("#chat-template").content).cloneNode(true));
    this.update();
  }

  receive(message) {
    if (this.ids.has(message.id)) return;
    this.ids.add(message.id);
    $(".empty-state", this.list)?.remove();
    const nearBottom = this.list.scrollHeight - this.list.scrollTop - this.list.clientHeight < 90;
    const outgoing = message.sender === this.key;
    const item = document.createElement("div");
    item.className = `message${outgoing ? " outgoing" : ""}`;
    item.dataset.messageId = message.id;
    const label = document.createElement("span");
    label.className = "message-label";
    label.textContent = outgoing ? "Вы" : ({ first: "Чат 01", second: "Чат 02" }[message.sender] || message.sender);
    if (message.visibility.mode === "selected") label.textContent += " · Ограниченная видимость";
    const text = document.createElement("p");
    text.className = "message-text";
    text.textContent = message.text;
    const time = document.createElement("time");
    time.className = "message-time";
    time.dateTime = message.created_at;
    time.textContent = new Date(message.created_at).toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit" });
    item.append(label, text, time);
    const following = Array.from(this.list.children).find(
      (child) => Number(child.dataset.messageId) > message.id,
    );
    this.list.insertBefore(item, following || null);
    if (nearBottom || outgoing) this.list.scrollTop = this.list.scrollHeight;
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
    const text = this.input.value.trim();
    if (!ready || this.busy || !text) return;
    const epoch = generation;
    const targetRoom = room;
    const visibility = this.audience.value === "self"
      ? { mode: "selected", chat_ids: [this.key] }
      : { mode: "all", chat_ids: [] };
    if (!this.retry || this.retry.text !== text || JSON.stringify(this.retry.visibility) !== JSON.stringify(visibility)) {
      this.retry = { sender: this.key, type: "chat.message", text, visibility, metadata: {}, request_id: crypto.randomUUID() };
    }
    this.busy = true;
    this.error.hidden = true;
    this.update();
    try {
      await request(`/api/rooms/${targetRoom}/messages`, {
        method: "POST", body: JSON.stringify(this.retry),
      });
      if (epoch !== generation) return;
      this.input.value = "";
      this.retry = null;
      setConnection(true);
      try { await refreshChats(targetRoom, epoch); } catch {
        if (epoch === generation) setConnection(false);
      }
    } catch (error) {
      if (epoch !== generation) return;
      this.error.textContent = `${error.message} Текст сохранён в поле ввода.`;
      this.error.hidden = false;
      setConnection(false);
    } finally {
      if (epoch === generation) {
        this.busy = false;
        this.update();
        this.input.focus();
      }
    }
  }
}

const first = new Chat("first", "01", "Читает доступные сообщения общей ленты");
const second = new Chat("second", "02", "Читает доступные сообщения общей ленты");
const chats = [first, second];

async function refreshChats(roomId, epoch) {
  const pages = await Promise.all(chats.map((chat) => chat.read(roomId)));
  if (epoch !== generation || roomId !== room) return false;
  pages.forEach((page, index) => chats[index].applyPage(page));
  return pages.some((page) => page.has_more);
}

async function poll(epoch) {
  if (epoch !== generation) return;
  let next = document.hidden ? 5000 : 900;
  try {
    const hasMore = await refreshChats(room, epoch);
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
  notice.hidden = true;
  try {
    const fragment = readFragment();
    if (fragment.get("access")) setToken(fragment.get("access"));
    let selected = fresh ? null : fragment.get("room") || storedRoom();
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
    room = selected;
    chats.forEach((chat) => chat.reset());
    initial.forEach((page, index) => chats[index].applyPage(page));
    history.replaceState(null, "", `#${roomFragment()}`);
    rememberRoom(room);
    ready = true;
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
  if (chats.some((chat) => chat.busy)) return;
  if (chats.some((chat) => chat.input.value.trim()) && !confirm("Начать новый диалог? Неотправленный текст будет очищен.")) return;
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
