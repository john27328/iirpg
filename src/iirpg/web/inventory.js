/* Inventory UI reads states, categories, limits and attributes from room rules. */
import { CharacterSheetWindow } from "/character-sheet.js";

const $ = (selector, root) => root.querySelector(selector);
const node = (tag, className = "", text = "") => {
  const element = document.createElement(tag);
  element.className = className;
  element.textContent = text;
  return element;
};
const option = (value, label) => new Option(label, value);
const button = (label, handler, className = "button-secondary") => {
  const element = node("button", className, label);
  element.type = "button";
  element.addEventListener("click", handler);
  return element;
};

export class InventoryPanel {
  constructor(root, request, context, createId) {
    Object.assign(this, { root, request, context, createId, state: null, selected: null, busy: false,
      retry: null, filter: "all", search: "", lastRead: 0 });
    root.innerHTML = `
      <header class="inventory-header"><div><div class="eyebrow">ПЕРСОНАЖИ И СНАРЯЖЕНИЕ</div><h2>Всё, что вы несёте</h2><p class="inventory-rules"></p></div>
      <div class="inventory-actions"><button type="button" class="button-secondary manage-players">Игроки</button><button type="button" class="button-secondary new-character">+ Персонаж</button><button type="button" class="button-secondary reload-roster">Обновить</button></div></header>
      <p class="inventory-error form-error" role="alert" hidden></p>
      <div class="inventory-layout"><aside class="roster-sidebar"><label>Найти персонажа<input type="search" class="roster-search" placeholder="Имя персонажа…"></label>
      <div class="roster-filters" role="group" aria-label="Тип персонажа"></div><div class="roster-list"></div></aside>
      <div class="character-detail"></div></div>
      <p class="inventory-footnote">Учет снаряжения для маленьких и средних существ. <a href="/inventory-ORC-NOTICE.md" target="_blank" rel="noopener">Правила и лицензия ORC</a></p>
      <dialog class="sheet-dialog" aria-label="Лист персонажа"></dialog>
      <dialog class="players-dialog"><form class="player-form"><h3>Игроки комнаты</h3><p class="muted">Игрок может владеть несколькими персонажами.</p><label>Имя игрока<input name="name" required maxlength="120"></label><div class="dialog-actions"><button type="submit" class="button-secondary">Добавить игрока</button><button type="button" class="button-secondary close-dialog">Закрыть</button></div></form><div class="players-list"></div></dialog>
      <dialog class="character-dialog"><form class="character-form"><h3>Персонаж</h3><input name="id" type="hidden"><div class="inventory-form-grid">
      <label>Имя<input name="name" required maxlength="120"></label><label>Тип<select name="kind"><option value="pc">Персонаж игрока</option><option value="npc">NPC</option></select></label>
      <label>Игрок-владелец<select name="player_id"></select></label><label>Размер<select name="size"></select></label><div class="attribute-fields"></div>
      <label>Монет всего<input name="coins" type="number" min="0" max="10000000" step="1" value="0" required></label>
      <label class="wide-field">Заметки<textarea name="notes" rows="2" maxlength="2000"></textarea></label></div>
      <div class="dialog-actions"><button type="submit" class="button-secondary">Сохранить</button><button type="button" class="button-secondary close-dialog">Отмена</button></div></form></dialog>
      <dialog class="item-dialog"><form class="item-form"><h3>Предмет</h3><input name="id" type="hidden"><label>Взять за основу<select class="item-template"></select></label><div class="inventory-form-grid">
      <label>Название<input name="name" required maxlength="120"></label><label>Категория<select name="category"></select></label>
      <label>Количество<input name="quantity" type="number" min="1" max="100000" step="1" value="1" required></label><label>Bulk одного предмета<input name="bulk" maxlength="4" placeholder="1, L или —" value="—" required></label>
      <label>Рук для использования<input name="required_hands" type="number" min="1" step="1" value="1" required></label><label>Ещё свободных рук для использования<input name="free_hands" type="number" min="0" step="1" value="0" required></label>
      <label>Расположение<select name="state"></select></label><label class="grip-field">Занято рук<input name="hands" type="number" min="1" step="1" value="1" required></label>
      <label class="wide-field">Источник<input name="source_url" type="url" maxlength="1000" placeholder="https://…"></label><label class="wide-field">Заметки<textarea name="notes" rows="2" maxlength="2000"></textarea></label></div>
      <p class="muted">Bulk: число, L — лёгкий, — — пренебрежимый. Для предмета в руках количество равно 1. Броня указывается с Bulk при ношении.</p>
      <div class="dialog-actions"><button type="submit" class="button-secondary">Сохранить</button><button type="button" class="button-secondary close-dialog">Отмена</button></div></form></dialog>`;
    this.error = $(".inventory-error", root);
    this.playerForm = $(".player-form", root);
    this.characterForm = $(".character-form", root);
    this.itemForm = $(".item-form", root);
    this.sheetWindow = new CharacterSheetWindow($(".sheet-dialog", root), () => this.state,
      (id, data) => this.mutate("update_character", data, { id }), () => this.update());
    root.querySelectorAll(".close-dialog").forEach((element) => element.addEventListener("click", () => element.closest("dialog").close()));
    for (const [key, label] of [["all", "Все"], ["pc", "Игроки"], ["npc", "NPC"]]) {
      const control = button(label, () => { this.filter = key; this.renderList(); }, "roster-filter");
      control.dataset.filter = key;
      $(".roster-filters", root).append(control);
    }
    $(".roster-search", root).addEventListener("input", (event) => { this.search = event.target.value; this.renderList(); });
    $(".manage-players", root).addEventListener("click", () => { this.renderPlayers(); this.openDialog("players"); });
    $(".new-character", root).addEventListener("click", () => this.editCharacter());
    $(".reload-roster", root).addEventListener("click", () => this.refresh(true).catch((error) => this.showError(error)));
    this.characterForm.elements.kind.addEventListener("change", () => this.updateOwner());
    this.itemForm.elements.state.addEventListener("change", () => this.updateGrip());
    $(".item-template", root).addEventListener("change", (event) => {
      const template = this.state.rules.catalog.find((entry) => entry.id === event.target.value);
      if (template) for (const [key, value] of Object.entries(template)) {
        if (key !== "id" && this.itemForm.elements[key]) this.itemForm.elements[key].value = value;
      }
    });
    this.playerForm.addEventListener("submit", async (event) => {
      event.preventDefault();
      if (await this.mutate("create_player", { name: this.playerForm.elements.name.value })) {
        this.playerForm.reset(); this.renderPlayers();
      }
    });
    this.characterForm.addEventListener("submit", async (event) => {
      event.preventDefault();
      const form = this.characterForm.elements;
      const attributes = Object.fromEntries(Object.keys(this.attributeSpecs()).map((key) => [key, Number(form[`attribute-${key}`].value)]));
      const data = { name: form.name.value, kind: form.kind.value, player_id: form.kind.value === "pc" ? form.player_id.value || null : null,
        size: form.size.value, coins: Number(form.coins.value), notes: form.notes.value, attributes };
      if (await this.mutate(form.id.value ? "update_character" : "create_character", data, { id: form.id.value || undefined }, true)) {
        $(".character-dialog", root).close();
      }
    });
    this.itemForm.addEventListener("submit", async (event) => {
      event.preventDefault();
      const form = this.itemForm.elements;
      const held = this.state.rules.states[form.state.value].uses_hands;
      const data = Object.fromEntries(["name", "category", "bulk", "state", "notes", "source_url"].map((key) => [key, form[key].value]));
      for (const key of ["quantity", "required_hands", "free_hands"]) data[key] = Number(form[key].value);
      data.hands = held ? Number(form.hands.value) : 0;
      if (await this.mutate(form.id.value ? "update_item" : "add_item", data, { id: form.id.value || undefined, character_id: this.selected })) {
        $(".item-dialog", root).close();
      }
    });
    this.update();
  }

  openDialog(name) {
    this.error.hidden = true;
    const dialog = $(`.${name}-dialog`, this.root);
    if (!dialog.open) dialog.showModal();
  }

  showError(error) {
    this.error.textContent = error.message;
    this.error.hidden = false;
    // Dialogs are modal: move the alert inside the current dialog so it is visible.
    const dialog = $("dialog[open]", this.root);
    if (dialog) dialog.append(this.error);
    else this.root.insertBefore(this.error, $(".inventory-layout", this.root));
  }

  update() {
    const disabled = !this.context().ready || !this.state || this.busy;
    this.root.querySelectorAll("button, input, select, textarea").forEach((control) => { control.disabled = disabled; });
    this.sheetWindow.update(disabled);
    if (!disabled) { this.updateOwner(); this.updateGrip(); }
  }

  hasDraft() { return this.sheetWindow.editing || Boolean($("dialog[open]:not(.sheet-dialog)", this.root)); }

  attributeSpecs() { return { ...this.state.character_rules.attributes, ...this.state.rules.attributes }; }

  reset(state) {
    this.root.querySelectorAll("dialog[open]").forEach((dialog) => dialog.close());
    this.root.append(this.error);
    this.error.hidden = true;
    this.state = state;
    this.selected = null;
    this.retry = null;
    this.busy = false;
    this.lastRead = Date.now();
    this.render();
  }

  async refresh(force = false) {
    const context = this.context();
    if (!context.ready || this.busy || (!force && Date.now() - this.lastRead < 4000)) return;
    const state = await this.request(`/api/rooms/${context.room}/roster`);
    if (context.generation !== this.context().generation) return;
    this.lastRead = Date.now();
    // Keep form fields and their original revision intact when another window edits.
    if (this.hasDraft()) return;
    if (!this.state || state.revision > this.state.revision) { this.state = state; this.render(); }
  }

  async mutate(action, data = {}, extra = {}, selectResult = false) {
    const context = this.context();
    if (!context.ready || this.busy) return false;
    const body = JSON.stringify({ action, revision: this.state.revision, data, ...extra });
    const signature = `${context.room}:${body}`;
    if (this.retry?.signature !== signature) this.retry = { signature, id: this.createId() };
    this.busy = true;
    this.error.hidden = true;
    this.update();
    try {
      const result = await this.request(`/api/rooms/${context.room}/roster`, { method: "POST", body, headers: { "Idempotency-Key": this.retry.id } });
      const state = await this.request(`/api/rooms/${context.room}/roster`);
      if (context.generation !== this.context().generation) return false;
      this.retry = null;
      this.state = state;
      if (selectResult) this.selected = result.entity_id;
      this.render();
      this.renderPlayers();
      return true;
    } catch (error) {
      if (context.generation !== this.context().generation) return false;
      if (error.status === 409) {
        try {
          const state = await this.request(`/api/rooms/${context.room}/roster`);
          if (context.generation !== this.context().generation) return false;
          this.state = state;
          this.render();
        } catch { /* Preserve original conflict. */ }
      }
      if (error.status && error.status < 500) this.retry = null;
      this.showError(error);
      return false;
    } finally {
      if (context.generation === this.context().generation) { this.busy = false; this.update(); }
    }
  }

  render() {
    if (!this.state) return;
    $(".inventory-rules", this.root).textContent = `${this.state.rules.name} · Персонажей: ${this.state.characters.length} · Игроков: ${this.state.players.length}`;
    if (!this.state.characters.some((entry) => entry.id === this.selected)) this.selected = this.state.characters[0]?.id || null;
    this.renderList();
    this.renderDetail();
    if (this.sheetWindow.dialog.open) this.sheetWindow.sync();
    this.update();
  }

  renderList() {
    if (!this.state) return;
    this.root.querySelectorAll(".roster-filter").forEach((entry) => entry.setAttribute("aria-pressed", String(entry.dataset.filter === this.filter)));
    const list = $(".roster-list", this.root);
    list.replaceChildren();
    const entries = this.state.characters.filter((entry) => (this.filter === "all" || entry.kind === this.filter) && entry.name.toLocaleLowerCase().includes(this.search.toLocaleLowerCase()));
    for (const entry of entries) {
      const owner = this.state.players.find((player) => player.id === entry.player_id)?.name;
      const card = button("", () => { this.selected = entry.id; this.error.hidden = true; this.render(); }, "roster-card");
      card.setAttribute("aria-pressed", String(entry.id === this.selected));
      card.append(node("strong", "", entry.name), node("span", "muted", entry.kind === "npc" ? "NPC" : `Игровой персонаж · ${owner || "без владельца"}`));
      list.append(card);
    }
    if (!entries.length) list.append(node("p", "muted", this.state.characters.length ? "Персонажи не найдены." : "Создайте первого персонажа игрока или NPC."));
  }

  renderDetail() {
    const detail = $(".character-detail", this.root);
    detail.replaceChildren();
    const actor = this.state.characters.find((entry) => entry.id === this.selected);
    if (!actor) {
      detail.append(node("div", "inventory-empty", "У каждого персонажа будет свой инвентарь и экипировка."));
      return;
    }
    const header = node("div", "character-heading");
    const title = node("div");
    const owner = this.state.players.find((player) => player.id === actor.player_id)?.name;
    title.append(node("span", "eyebrow", actor.kind === "npc" ? "NPC" : `ПЕРСОНАЖ ИГРОКА${owner ? ` · ${owner}` : ""}`), node("h3", "", actor.name));
    const actions = node("div", "inventory-actions");
    actions.append(button("Лист персонажа", () => { this.error.hidden = true; this.sheetWindow.open(actor.id); }), button("Изменить", () => this.editCharacter(actor)), button("Удалить", async () => {
      if (confirm(`Удалить персонажа «${actor.name}» и все его предметы?`)) await this.mutate("delete_character", {}, { id: actor.id });
    }));
    header.append(title, actions);
    const summary = actor.summary;
    const stats = node("div", "inventory-stats");
    for (const [label, value] of [["Нагрузка Bulk", `${summary.bulk}${summary.light_remainder ? ` + ${summary.light_remainder}L` : ""}`], ["Без перегруза / максимум", `${summary.encumbered_at} / ${summary.maximum}`], ["Занято рук", `${summary.hands_used} / ${summary.hands_total}`], ["Монеты", String(actor.coins)]]) {
      const stat = node("div"); stat.append(node("span", "muted", label), node("strong", "", value)); stats.append(stat);
    }
    detail.append(header, stats);
    if (summary.encumbered) detail.append(node("p", "inventory-warning", "Перегруз: clumsy 1 и −10 футов ко всем Скоростям (минимум 5)."));
    if (actor.notes) detail.append(node("p", "character-notes", actor.notes));
    const itemsHeader = node("div", "inventory-items-header");
    itemsHeader.append(node("h4", "", `Инвентарь · ${actor.items.length}`), button("+ Предмет", () => this.editItem()));
    detail.append(itemsHeader);
    if (!actor.items.length) detail.append(node("p", "inventory-empty", "Инвентарь пуст. Добавьте предмет из правил или опишите свой."));
    for (const [stateId, spec] of Object.entries(this.state.rules.states)) {
      const items = actor.items.filter((item) => item.state === stateId);
      if (!items.length) continue;
      const group = node("section", "inventory-group");
      group.append(node("h4", "", `${spec.label} · ${items.length}`));
      for (const item of items) {
        const row = node("div", "inventory-item");
        const info = node("div", "item-info");
        info.append(node("strong", "", `${item.name}${item.quantity > 1 ? ` × ${item.quantity}` : ""}`));
        let meta = `${this.state.rules.categories[item.category].label} · Bulk ${item.bulk} / шт.`;
        if (spec.uses_hands) meta += ` · занято рук: ${item.hands} · ${summary.ready_item_ids.includes(item.id) ? "готов к использованию" : "не готов к использованию"}`;
        info.append(node("span", "muted", meta));
        if (item.notes) info.append(node("span", "item-notes", item.notes));
        const controls = node("div", "item-controls");
        const state = node("select");
        state.setAttribute("aria-label", `Расположение: ${item.name}`);
        for (const [key, value] of Object.entries(this.state.rules.states)) state.add(option(key, value.label));
        state.value = item.state;
        state.addEventListener("change", async () => {
          const changed = await this.mutate("update_item", { state: state.value }, { id: item.id, character_id: actor.id });
          if (!changed) state.value = item.state;
        });
        controls.append(state, button("Изменить", () => this.editItem(item)), button("×", async () => {
          if (confirm(`Удалить «${item.name}» из инвентаря?`)) await this.mutate("remove_item", {}, { id: item.id, character_id: actor.id });
        }));
        controls.lastChild.setAttribute("aria-label", `Удалить ${item.name}`);
        row.append(info, controls);
        group.append(row);
      }
      detail.append(group);
    }
  }

  renderPlayers() {
    if (!this.state) return;
    const list = $(".players-list", this.root);
    list.replaceChildren();
    for (const player of this.state.players) {
      const row = node("div", "player-row");
      row.append(node("span", "", player.name), button("Переименовать", async () => {
        const value = prompt("Имя игрока", player.name);
        if (value?.trim()) await this.mutate("update_player", { name: value }, { id: player.id });
      }), button("Удалить", async () => {
        if (confirm(`Удалить игрока «${player.name}»?`)) await this.mutate("delete_player", {}, { id: player.id });
      }));
      list.append(row);
    }
    if (!this.state.players.length) list.append(node("p", "muted", "Игроков пока нет. NPC можно создать без игрока."));
  }

  editCharacter(actor = null) {
    const form = this.characterForm;
    form.reset();
    form.elements.id.value = actor?.id || "";
    $("h3", form).textContent = actor ? "Изменить персонажа" : "Новый персонаж";
    const owner = form.elements.player_id;
    owner.replaceChildren(option("", "Без владельца"));
    for (const player of this.state.players) owner.add(option(player.id, player.name));
    const size = form.elements.size;
    size.replaceChildren(...Object.entries(this.state.rules.sizes).map(([key, value]) => option(key, value)));
    size.value = this.state.rules.default_size;
    const attributes = $(".attribute-fields", form);
    attributes.replaceChildren();
    for (const [key, spec] of Object.entries(this.attributeSpecs())) {
      const label = node("label", "", spec.label);
      const input = node("input");
      Object.assign(input, { name: `attribute-${key}`, type: "number", min: spec.min, max: spec.max, step: "1", required: true, value: actor?.attributes[key] ?? spec.default });
      label.append(input); attributes.append(label);
    }
    if (actor) for (const key of ["name", "kind", "player_id", "size", "coins", "notes"]) form.elements[key].value = actor[key] ?? "";
    this.updateOwner();
    this.openDialog("character");
  }

  updateOwner() {
    const form = this.characterForm.elements;
    form.player_id.disabled = this.busy || form.kind.value === "npc";
  }

  updateGrip() {
    const form = this.itemForm.elements;
    const held = Boolean(this.state?.rules.states[form.state.value]?.uses_hands);
    $(".grip-field", this.itemForm).hidden = !held;
    form.hands.disabled = this.busy || !held;
  }

  editItem(item = null) {
    const form = this.itemForm;
    form.reset();
    form.elements.id.value = item?.id || "";
    $("h3", form).textContent = item ? "Изменить предмет" : "Новый предмет";
    const catalog = $(".item-template", form);
    catalog.replaceChildren(option("", "Свой предмет"), ...this.state.rules.catalog.map((entry) => option(entry.id, entry.name)));
    for (const key of ["category", "state"]) {
      const entries = key === "category" ? this.state.rules.categories : this.state.rules.states;
      form.elements[key].replaceChildren(...Object.entries(entries).map(([id, spec]) => option(id, spec.label)));
    }
    form.elements.state.value = this.state.rules.default_state;
    for (const key of ["hands", "required_hands", "free_hands"]) form.elements[key].max = this.state.rules.hands;
    if (item) for (const [key, value] of Object.entries(item)) if (form.elements[key]) form.elements[key].value = value;
    if (!Number(form.elements.hands.value)) form.elements.hands.value = 1;
    this.updateGrip();
    this.openDialog("item");
  }
}
