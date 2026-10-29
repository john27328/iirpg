/* Administrative room inspector and unfiltered queue log. */
const $ = (selector, root) => root.querySelector(selector);
const element = (tag, className = "", text = "") => {
  const node = document.createElement(tag);
  node.className = className;
  node.textContent = text;
  return node;
};
const ENTITY_LABELS = {
  scene: "Сцена и намерения", room: "Комната", players: "Игроки", characters: "Персонажи", items: "Предметы",
  rules: "Правила инвентаря", subscribers: "Подписчики", tools: "Инструменты и команды",
  character_rules: "Правила листа персонажа", encounter: "Текущее столкновение",
};

export class DebugPanel {
  constructor(root, logRoot, request, context) {
    Object.assign(this, { root, logRoot, request, context, busy: false, cursor: 0, hasMore: false,
      snapshot: null, entries: new Map(), selected: "room", entityFingerprint: "" });
    root.innerHTML = `
      <header class="debug-panel-header"><div><div class="eyebrow">СОСТОЯНИЕ КОМНАТЫ</div><h2>Внутренние сущности</h2></div><button type="button" class="button-secondary debug-refresh" disabled>Обновить</button></header>
      <p class="debug-room-id muted"></p><div class="debug-stats"></div>
      <label class="debug-entity-label">Сущность<select class="debug-entity-select" disabled></select></label>
      <p class="debug-revision muted"></p><pre class="debug-entity-json" tabindex="0">Подключение к комнате…</pre>
      <p class="debug-error form-error" role="alert" hidden></p>`;
    logRoot.innerHTML = `
      <header class="debug-panel-header"><div><div class="eyebrow">ОБЩАЯ ОЧЕРЕДЬ</div><h2>Полный лог чата</h2><p class="muted">Сообщения, предложения бросков, вызовы инструментов и результаты, включая записи с ограниченной видимостью.</p></div><span class="debug-log-count muted"></span></header>
      <div class="debug-log-filters"><label>Поиск<input type="search" class="debug-log-search" placeholder="Текст, UUID, metadata…" disabled></label>
      <label>Тип события<select class="debug-type-filter" disabled><option value="all">Все типы</option></select></label>
      <label>Представление подписчика<select class="debug-subscriber-filter" disabled><option value="all">Вся очередь</option></select></label>
      <label class="debug-follow-label"><input type="checkbox" class="debug-follow" checked disabled>Следить за новыми</label></div>
      <div class="debug-event-list" role="log" aria-live="polite" aria-relevant="additions" tabindex="0"><p class="debug-log-empty muted">Событий пока нет.</p></div>`;
    this.entitySelect = $(".debug-entity-select", root);
    for (const [key, label] of Object.entries(ENTITY_LABELS)) this.entitySelect.add(new Option(label, key));
    this.entitySelect.addEventListener("change", () => { this.selected = this.entitySelect.value; this.renderEntity(); });
    this.error = $(".debug-error", root);
    this.list = $(".debug-event-list", logRoot);
    $(".debug-refresh", root).addEventListener("click", () => this.refresh());
    for (const [selector, event] of [[".debug-log-search", "input"], [".debug-type-filter", "change"], [".debug-subscriber-filter", "change"]]) {
      $(selector, logRoot).addEventListener(event, () => this.filter());
    }
  }

  update() {
    for (const root of [this.root, this.logRoot]) root.querySelectorAll("input, select, button").forEach((control) => {
      control.disabled = !this.context().ready;
    });
  }

  hasDraft() { return false; }

  reset() {
    this.cursor = 0;
    this.hasMore = false;
    this.snapshot = null;
    this.entries.clear();
    this.entityFingerprint = "";
    this.error.hidden = true;
    this.list.replaceChildren(element("p", "debug-log-empty muted", "Событий пока нет."));
    $(".debug-log-search", this.logRoot).value = "";
    $(".debug-type-filter", this.logRoot).replaceChildren(new Option("Все типы", "all"));
    $(".debug-subscriber-filter", this.logRoot).replaceChildren(new Option("Вся очередь", "all"));
    this.update();
  }

  read(roomId, after = this.cursor) {
    return this.request(`/api/rooms/${roomId}/debug?after=${after}`);
  }

  async refresh() {
    const context = this.context();
    if (!context.ready) return;
    try {
      const page = await this.read(context.room);
      if (context.generation !== this.context().generation) return;
      this.error.hidden = true;
      this.applyPage(page);
    } catch (error) {
      if (context.generation !== this.context().generation) return;
      this.error.textContent = error.message;
      this.error.hidden = false;
    }
  }

  applyPage(page) {
    this.error.hidden = true;
    this.snapshot = page;
    this.cursor = Math.max(this.cursor, page.queue.next_cursor);
    this.hasMore = page.queue.has_more;
    $(".debug-room-id", this.root).textContent = `room_id: ${page.room.id}`;
    $(".debug-revision", this.root).textContent = `Ревизия реестра: ${page.revision} · Курсор очереди: ${this.cursor}`;
    const stats = $(".debug-stats", this.root);
    const entities = page.entities;
    stats.replaceChildren();
    const total = Object.values(page.queue_counts).reduce((sum, count) => sum + count, 0);
    for (const [label, value] of [["Игроки", entities.players.length], ["PC / NPC", `${entities.characters.filter((entry) => entry.kind === "pc").length} / ${entities.characters.filter((entry) => entry.kind === "npc").length}`], ["Предметы", entities.items.length], ["События", total]]) {
      const stat = element("div");
      stat.append(element("span", "muted", label), element("strong", "", String(value)));
      stats.append(stat);
    }
    for (const option of this.entitySelect.options) {
      const value = entities[option.value];
      option.textContent = `${ENTITY_LABELS[option.value]}${Array.isArray(value) ? ` (${value.length})` : ""}`;
    }
    this.renderEntity();
    const types = $(".debug-type-filter", this.logRoot);
    for (const type of Object.keys(page.queue_counts)) {
      if (!Array.from(types.options).some((option) => option.value === type)) types.add(new Option(type, type));
    }
    const subscribers = $(".debug-subscriber-filter", this.logRoot);
    for (const chat of entities.subscribers) {
      if (!Array.from(subscribers.options).some((option) => option.value === chat.id)) subscribers.add(new Option(chat.id, chat.id));
    }
    const follow = $(".debug-follow", this.logRoot).checked;
    const nearBottom = this.list.scrollHeight - this.list.scrollTop - this.list.clientHeight < 90;
    let added = false;
    for (const event of page.queue.messages) {
      if (this.entries.has(event.id)) continue;
      added = true;
      $(".debug-log-empty", this.list)?.remove();
      const row = this.eventNode(event);
      const following = Array.from(this.list.children).find((entry) => Number(entry.dataset.eventId) > event.id);
      this.list.insertBefore(row, following || null);
      this.entries.set(event.id, { event, row, search: JSON.stringify(event).toLocaleLowerCase() });
    }
    this.filter();
    if (added && follow && nearBottom) this.list.scrollTop = this.list.scrollHeight;
  }

  renderEntity() {
    if (!this.snapshot) return;
    const raw = JSON.stringify(this.snapshot.entities[this.selected], null, 2);
    if (raw === this.entityFingerprint) return;
    this.entityFingerprint = raw;
    $(".debug-entity-json", this.root).textContent = raw;
  }

  eventNode(event) {
    const row = element("article", "debug-event");
    row.dataset.eventId = event.id;
    const header = element("div", "debug-event-header");
    header.append(element("span", "debug-event-id", `#${event.id}`), element("strong", "", event.type), element("span", "muted", event.sender));
    const time = element("time", "muted");
    time.dateTime = event.created_at;
    time.textContent = new Date(event.created_at).toLocaleString("ru-RU");
    header.append(time);
    const audience = event.visibility.mode === "all" ? "all" : event.visibility.chat_ids.join(", ");
    const metadata = element("p", "debug-event-meta", `request_id: ${event.request_id} · Видимость: ${audience}`);
    row.append(header, metadata);
    if (event.combat) {
      const c = event.combat;
      const labels = { move: "Перемещение", take_cover: "Укрытие", wait: "Ожидание", start: "Начало боя", strike: "Удар", shield: "Поднять щит", end_turn: "Конец хода", enemy_turn: "Ход противника" };
      row.append(element("p", "debug-combat-action", `Действие: ${labels[c.action] || c.action} · ${c.actor || event.sender} · Раунд ${c.round}${c.before && c.after ? ` · Действия: ${c.before.actions ?? "—"} → ${c.after.actions}` : ""}`));
    }
    if (event.text) row.append(element("p", "debug-event-text", event.text));
    const details = element("details");
    details.append(element("summary", "", "Исходный JSON"), element("pre", "debug-event-json", JSON.stringify(event, null, 2)));
    row.append(details);
    return row;
  }

  filter() {
    const search = $(".debug-log-search", this.logRoot).value.toLocaleLowerCase();
    const type = $(".debug-type-filter", this.logRoot).value;
    const subscriber = $(".debug-subscriber-filter", this.logRoot).value;
    let visible = 0;
    for (const { event, row, search: text } of this.entries.values()) {
      const audience = event.visibility;
      row.hidden = !(text.includes(search) && (type === "all" || event.type === type)
        && (subscriber === "all" || audience.mode === "all" || audience.chat_ids.includes(subscriber)));
      if (!row.hidden) visible++;
    }
    const total = this.snapshot ? Object.values(this.snapshot.queue_counts).reduce((sum, count) => sum + count, 0) : 0;
    $(".debug-log-count", this.logRoot).textContent = `Показано: ${visible} · Загружено: ${this.entries.size} / ${total}`;
    $(".debug-filter-empty", this.list)?.remove();
    if (!visible && this.entries.size) this.list.append(element("p", "debug-filter-empty muted", "Событий по этому фильтру нет."));
  }
}
