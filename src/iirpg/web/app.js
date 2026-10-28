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
let diceCatalog = null;
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
    this.toolCalls = new Map();
    this.element = $("#chat-template").content.firstElementChild.cloneNode(true);
    this.element.classList.add(key);
    $("h2", this.element).textContent = `Чат ${number}`;
    $(".avatar", this.element).textContent = number;
    $(".chat-number", this.element).textContent = "CHAT";
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
    this.error = $(".composer > .form-error", this.element);
    this.dicePanel = $(".dice-panel", this.element);
    this.diceSystem = $(".dice-system", this.element);
    this.diceError = $(".dice-error", this.element);
    this.diceRetry = null;
    this.diceSystem.addEventListener("change", () => this.applyDiceSystem());
    $(".dice-roll", this.element).addEventListener("click", () => this.rollDice());
    this.dicePanel.addEventListener("keydown", (event) => {
      if (event.key === "Enter" && !event.isComposing && ["INPUT", "SELECT"].includes(event.target.tagName)) {
        event.preventDefault();
        this.rollDice();
      }
    });
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
    this.element.querySelectorAll(".reply-choice").forEach((button) => {
      button.disabled = !ready || this.busy;
    });
    this.dicePanel.querySelectorAll("input, select, button").forEach((control) => {
      control.disabled = !ready || this.busy || !diceCatalog;
    });
    $(".character-count", this.element).textContent = `${Array.from(this.input.value).length} / 2000`;
  }

  hasDraft() { return Boolean(this.input.value.trim()); }

  reset() {
    this.ids.clear();
    this.toolCalls.clear();
    this.cursor = 0;
    this.hasMore = false;
    this.retry = null;
    this.diceRetry = null;
    this.diceError.hidden = true;
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
    if (message.tool === "dice.roll" && ["tool.call", "tool.result"].includes(message.type)) {
      this.receiveDice(message);
      return;
    }
    const nearBottom = this.list.scrollHeight - this.list.scrollTop - this.list.clientHeight < 90;
    const outgoing = message.sender === this.key;
    const item = document.createElement("div");
    item.className = `message${outgoing ? " outgoing" : ""}`;
    item.dataset.messageId = message.id;
    item.id = `message-${this.key}-${message.id}`;
    const label = document.createElement("span");
    label.className = "message-label";
    label.textContent = outgoing ? "Вы" : ({ first: "Чат", second: "JSON-заглушка" }[message.sender] || message.sender);
    if (message.visibility.mode === "selected") label.textContent += " · Ограниченная видимость";
    const text = document.createElement("p");
    text.className = "message-text";
    text.textContent = message.text;
    const time = document.createElement("time");
    time.className = "message-time";
    time.dateTime = message.created_at;
    time.textContent = new Date(message.created_at).toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit" });
    item.append(label, text);
    if (!outgoing && message.choices?.length) {
      const choices = document.createElement("div");
      choices.className = "reply-choices";
      choices.setAttribute("role", "group");
      choices.setAttribute("aria-label", "Варианты ответа");
      for (const choice of message.choices) {
        const button = document.createElement("button");
        button.type = "button";
        button.className = "reply-choice";
        button.textContent = choice;
        button.disabled = !ready || this.busy;
        button.addEventListener("click", () => this.send(choice));
        choices.append(button);
      }
      const hint = document.createElement("span");
      hint.className = "reply-hint";
      hint.textContent = "Нажмите на вариант для отправки или напишите свой ответ ниже.";
      item.append(choices, hint);
    }
    item.append(time);
    const following = Array.from(this.list.children).find(
      (child) => Number(child.dataset.messageId) > message.id,
    );
    this.list.insertBefore(item, following || null);
    if (nearBottom || outgoing) this.list.scrollTop = this.list.scrollHeight;
  }

  receiveDice(message) {
    const isResult = message.type === "tool.result";
    const key = isResult ? (message.tool_call_id || message.request_id) : message.request_id;
    if (!this.toolCalls.has(key)) this.toolCalls.set(key, { item: document.createElement("div") });
    const entry = this.toolCalls.get(key);
    entry[isResult ? "result" : "call"] = message;
    const { call, result, item } = entry;
    const source = call || result;
    const owner = call?.sender || result.requested_by;
    const outgoing = owner === this.key;
    const arguments_ = source.arguments;
    const roll = result?.tool_result;
    const nearBottom = this.list.scrollHeight - this.list.scrollTop - this.list.clientHeight < 90;
    const detailsOpen = $("details", item)?.open || false;
    item.className = `message dice-card${outgoing ? " outgoing" : ""}${roll ? (roll.success ? " dice-success" : " dice-failure") : ""}`;
    item.dataset.messageId = source.id;
    item.id = `message-${this.key}-${source.id}`;

    const body = document.createElement("div");
    body.className = "dice-card-body";
    const header = document.createElement("div");
    header.className = "dice-card-header";
    const label = document.createElement("span");
    label.className = "message-label";
    const ownerName = outgoing ? "Вы" : ({ first: "Чат", second: "JSON-заглушка" }[owner] || owner || "Кости");
    label.textContent = `🎲 ${ownerName} · ${arguments_.label}`;
    if (source.visibility.mode === "selected") label.textContent += " · Ограниченная видимость";
    const time = document.createElement("time");
    time.className = "message-time";
    time.dateTime = source.created_at;
    time.textContent = new Date(source.created_at).toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit" });
    header.append(label, time);

    const line = document.createElement("div");
    line.className = "dice-card-line";
    const formula = document.createElement("span");
    const symbols = { ">=": "≥", ">": ">", "<=": "≤", "<": "<", "==": "=", "!=": "≠" };
    const criterion = arguments_.success;
    const score = roll ? `${roll.aggregation === "count" ? "успехов: " : ""}${roll.result}` : "…";
    formula.textContent = `${arguments_.roll} → ${score} · ${symbols[criterion.operator]} ${criterion.target}`;
    const outcome = document.createElement("span");
    outcome.className = "dice-outcome";
    outcome.textContent = roll ? (roll.success ? "Успех" : "Неудача") : "Бросок…";
    line.append(formula, outcome);
    body.append(header, line);
    if (result) {
      const details = document.createElement("details");
      details.className = "dice-details";
      details.open = detailsOpen;
      const summary = document.createElement("summary");
      summary.textContent = "Подробности";
      const text = document.createElement("p");
      text.textContent = result.text;
      details.append(summary, text);
      body.append(details);
    }
    item.replaceChildren(body);
    const following = Array.from(this.list.children).find(
      (child) => child !== item && Number(child.dataset.messageId) > source.id,
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

  async send(choice = null) {
    const text = choice ?? this.input.value.trim();
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
      if (choice === null) this.input.value = "";
      this.retry = null;
      setConnection(true);
      try { await refreshChats(targetRoom, epoch); } catch {
        if (epoch === generation) setConnection(false);
      }
    } catch (error) {
      if (epoch !== generation) return;
      this.error.textContent = choice === null
        ? `${error.message} Текст сохранён в поле ввода.`
        : `${error.message} Отправка не подтверждена. Нажмите вариант ещё раз для повтора.`;
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

  configureDice(catalog) {
    this.diceSystem.replaceChildren();
    for (const [key, definition] of Object.entries(catalog.systems)) {
      const option = document.createElement("option");
      option.value = key;
      option.textContent = definition.label;
      this.diceSystem.append(option);
    }
    this.applyDiceSystem();
  }

  applyDiceSystem() {
    const definition = diceCatalog?.systems[this.diceSystem.value];
    if (!definition) return;
    $(".dice-formula", this.element).value = definition.roll;
    $(".dice-operator", this.element).value = definition.success.operator;
    $(".dice-target", this.element).value = definition.success.target;
    $(".dice-pool-fields", this.element).hidden = definition.aggregation !== "count";
    if (definition.success.die) {
      $(".dice-die-operator", this.element).value = definition.success.die.operator;
      $(".dice-die-target", this.element).value = definition.success.die.target;
    }
    this.diceError.hidden = true;
  }

  async rollDice() {
    if (!ready || this.busy || !diceCatalog) return;
    this.diceError.hidden = true;
    const integer = (selector) => {
      const value = $(selector, this.element).value;
      if (!String(value).trim() || !Number.isInteger(Number(value))) throw new Error("Порог должен быть целым числом.");
      return Number(value);
    };
    let arguments_;
    try {
      const success = { operator: $(".dice-operator", this.element).value, target: integer(".dice-target") };
      if (diceCatalog.systems[this.diceSystem.value].aggregation === "count") {
        success.die = { operator: $(".dice-die-operator", this.element).value, target: integer(".dice-die-target") };
      }
      arguments_ = { system: this.diceSystem.value, roll: $(".dice-formula", this.element).value,
        success, label: $(".dice-label", this.element).value.trim() || "Бросок костей" };
    } catch (error) {
      this.diceError.textContent = error.message;
      this.diceError.hidden = false;
      return;
    }
    const visibility = this.audience.value === "self" ? { mode: "selected", chat_ids: [this.key] } : { mode: "all", chat_ids: [] };
    const signature = JSON.stringify({ arguments: arguments_, visibility });
    if (this.diceRetry?.signature !== signature) {
      this.diceRetry = { signature, payload: { sender: this.key, request_id: crypto.randomUUID(), arguments: arguments_, visibility } };
    }
    const epoch = generation;
    const targetRoom = room;
    this.busy = true;
    this.update();
    try {
      await request(`/api/rooms/${targetRoom}/tools/dice`, { method: "POST", body: JSON.stringify(this.diceRetry.payload) });
      if (epoch !== generation) return;
      this.diceRetry = null;
      setConnection(true);
      try { await refreshChats(targetRoom, epoch); } catch {
        if (epoch === generation) setConnection(false);
      }
    } catch (error) {
      if (epoch !== generation) return;
      this.diceError.textContent = `${error.message} Повтор с теми же параметрами не перебросит кости.`;
      this.diceError.hidden = false;
      if (!error.status || error.status >= 500) setConnection(false);
    } finally {
      if (epoch === generation) { this.busy = false; this.update(); }
    }
  }
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
    this.error = $(".form-error", this.element);
    this.status = $(".json-status", this.element);
    this.reset();
    this.input.addEventListener("input", () => this.update());
    this.diceExampleButton.addEventListener("click", () => {
      const system = diceCatalog.systems.d20 ? "d20" : Object.keys(diceCatalog.systems)[0];
      const definition = diceCatalog.systems[system];
      this.input.value = JSON.stringify({ sender: "second", type: "tool.call", tool: "dice.roll",
        request_id: crypto.randomUUID(), arguments: { system, roll: definition.roll,
          success: definition.success } }, null, 2);
      this.error.hidden = true;
      this.status.hidden = true;
      this.update();
    });
    this.newIdButton.addEventListener("click", () => {
      try {
        const payload = this.parse();
        payload.request_id = crypto.randomUUID();
        this.input.value = JSON.stringify(payload, null, 2);
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
    this.input.disabled = !ready || this.busy;
    this.newIdButton.disabled = !ready || this.busy;
    this.diceExampleButton.disabled = !ready || this.busy || !diceCatalog;
    this.button.disabled = !ready || this.busy || !this.input.value.trim();
  }

  hasDraft() { return this.input.value !== this.initialRaw; }

  reset() {
    this.cursor = 0;
    this.hasMore = false;
    this.ids = new Set();
    this.busy = false;
    this.initialRaw = JSON.stringify({
      sender: "second", type: "chat.message", request_id: crypto.randomUUID(),
      text: "Перед вами закрытая дверь. Что будете делать?",
      choices: ["Осмотреться", "Открыть дверь", "Уйти"],
      visibility: { mode: "all", chat_ids: [] }, metadata: {},
    }, null, 2);
    this.input.value = this.initialRaw;
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
    try { this.parse(); } catch (error) { this.showError(error); return; }
    const raw = this.input.value;
    const epoch = generation;
    const targetRoom = room;
    this.busy = true;
    this.update();
    try {
      const result = await request(`/api/rooms/${targetRoom}/messages`, { method: "POST", body: raw });
      if (epoch !== generation) return;
      this.initialRaw = raw;
      this.status.textContent = `Запись #${result.message.id} принята. Для нового сообщения смените request_id.`;
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

const first = new Chat("first", "01", "Читает доступные сообщения общей ленты");
const stub = new JsonStub();
const chats = [first, stub];

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
    diceCatalog = await request("/api/tools/dice");
    first.configureDice(diceCatalog);
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
  if (chats.some((chat) => chat.hasDraft()) && !confirm("Начать новый диалог? Неотправленный текст будет очищен.")) return;
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
