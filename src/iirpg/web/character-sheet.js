/* Field layout and displayed statistics come from the room's sheet configuration. */
const $ = (selector, root) => root.querySelector(selector);
const node = (tag, className = "", text = "") => {
  const element = document.createElement(tag);
  element.className = className;
  element.textContent = text;
  return element;
};

export class CharacterSheetWindow {
  constructor(dialog, state, save, changed) {
    Object.assign(this, { dialog, state, save, changed, actorId: null, editing: false, disabled: false });
    dialog.innerHTML = `
      <header class="sheet-header"><div><div class="eyebrow sheet-kind"></div><h2 class="sheet-name"></h2><p class="sheet-owner muted"></p></div><button type="button" class="button-secondary sheet-close">Закрыть</button></header>
      <nav class="sheet-tabs workspace-tabs" aria-label="Лист персонажа"><button type="button" class="sheet-view-tab" aria-pressed="true">Лист персонажа</button><button type="button" class="sheet-edit-tab" aria-pressed="false">Редактировать</button></nav>
      <div class="sheet-overview"></div>
      <form class="sheet-form" hidden><label class="sheet-profile-label">Способ расчёта<select name="profile"></select></label><p class="sheet-description muted"></p>
      <div class="sheet-attribute-fields sheet-form-grid"></div><div class="sheet-field-groups"></div>
      <div class="dialog-actions"><button type="submit" class="button-secondary sheet-save">Сохранить лист</button><button type="button" class="button-secondary sheet-cancel">Отмена</button></div></form>
      <p class="sheet-footnote muted"></p>
      <p class="sheet-source muted"></p>`;
    this.form = $(".sheet-form", dialog);
    $(".sheet-close", dialog).addEventListener("click", () => this.close());
    $(".sheet-view-tab", dialog).addEventListener("click", () => this.setMode(false));
    $(".sheet-edit-tab", dialog).addEventListener("click", () => this.setMode(true));
    $(".sheet-cancel", dialog).addEventListener("click", () => this.setMode(false));
    dialog.addEventListener("cancel", (event) => {
      if (this.disabled) { event.preventDefault(); return; }
      if (this.editing && !confirm("Закрыть лист без сохранения изменений?")) event.preventDefault();
    });
    dialog.addEventListener("close", () => { this.editing = false; });
    this.form.elements.profile.addEventListener("change", () => {
      const previous = this.readFields();
      const oldProfile = this.activeProfile;
      const nextProfile = this.form.elements.profile.value;
      this.drafts[oldProfile] = previous;
      // Shared field values survive switching profiles; profile-specific values
      // remain in this editing session so switching back restores them.
      const source = this.drafts[nextProfile] || previous;
      this.renderEditor(nextProfile, source.values, previous.attributes);
    });
    this.form.addEventListener("submit", async (event) => {
      event.preventDefault();
      const data = this.readFields();
      if (await this.save(this.actorId, { attributes: data.attributes, sheet: { profile: this.activeProfile, values: data.values } })) {
        this.setMode(false);
      }
    });
  }

  actor() { return this.state()?.characters.find((entry) => entry.id === this.actorId); }

  attributes() { return { ...this.state().character_rules.attributes, ...this.state().rules.attributes }; }

  open(actorId) {
    this.actorId = actorId;
    this.editing = false;
    this.sync();
    if (!this.dialog.open) this.dialog.showModal();
    this.dialog.scrollTop = 0;
  }

  close() {
    if (!this.disabled && (!this.editing || confirm("Закрыть лист без сохранения изменений?"))) this.dialog.close();
  }

  update(disabled) {
    this.disabled = disabled;
    this.dialog.querySelectorAll("button, input, select, textarea").forEach((control) => { control.disabled = disabled; });
    if (!disabled && this.actor()) {
      const kind = this.actor().kind;
      this.form.elements.profile.disabled = Object.values(this.state().character_rules.profiles).filter((profile) => profile.kinds.includes(kind)).length < 2;
    }
  }

  setMode(editing) {
    if (this.disabled || !this.actor()) return;
    if (!editing && this.editing) {
      $(".inventory-error", this.dialog)?.setAttribute("hidden", "");
    }
    this.editing = editing;
    $(".sheet-view-tab", this.dialog).setAttribute("aria-pressed", String(!editing));
    $(".sheet-edit-tab", this.dialog).setAttribute("aria-pressed", String(editing));
    $(".sheet-overview", this.dialog).hidden = editing;
    this.form.hidden = !editing;
    if (editing) {
      this.drafts = {};
      const actor = this.actor();
      this.renderEditor(actor.sheet.profile, actor.sheet.values, actor.attributes);
    } else this.renderOverview();
    this.dialog.scrollTop = 0;
    this.changed();
  }

  sync() {
    const actor = this.actor();
    if (!actor) { this.dialog.close(); return; }
    $(".sheet-kind", this.dialog).textContent = actor.kind === "npc" ? "NPC" : "ПЕРСОНАЖ ИГРОКА";
    $(".sheet-name", this.dialog).textContent = actor.name;
    const owner = this.state().players.find((entry) => entry.id === actor.player_id);
    $(".sheet-owner", this.dialog).textContent = `${this.state().rules.sizes[actor.size]}${owner ? ` · ${owner.name}` : ""}`;
    const source = $(".sheet-source", this.dialog);
    $(".sheet-footnote", this.dialog).textContent = this.state().character_rules.help || "";
    source.replaceChildren();
    const link = node("a", "", this.state().character_rules.name);
    link.href = "/character-ORC-NOTICE.md";
    link.target = "_blank"; link.rel = "noopener";
    source.append(link);
    if (!this.editing) {
      $(".sheet-view-tab", this.dialog).setAttribute("aria-pressed", "true");
      $(".sheet-edit-tab", this.dialog).setAttribute("aria-pressed", "false");
      $(".sheet-overview", this.dialog).hidden = false;
      this.form.hidden = true;
      this.renderOverview();
    }
  }

  renderOverview() {
    const actor = this.actor();
    const config = this.state().character_rules;
    const profile = config.profiles[actor.sheet.profile];
    const root = $(".sheet-overview", this.dialog);
    root.replaceChildren(node("p", "sheet-profile-note muted", profile.label));
    const attributes = node("div", "sheet-attributes");
    for (const [key, spec] of Object.entries(this.attributes())) {
      const card = node("div", "sheet-attribute");
      card.append(node("span", "muted", spec.label), node("strong", "", this.format(actor.attributes[key], true)));
      attributes.append(card);
    }
    root.append(attributes);
    for (const [group, label] of Object.entries(config.groups)) {
      const fields = Object.entries(profile.fields).filter(([, spec]) => spec.group === group && spec.overview !== false);
      const stats = Object.entries(profile.statistics).filter(([, spec]) => spec.group === group);
      // Direct NPC inputs are already shown as derived outputs in the same group.
      const direct = new Set(stats.flatMap(([, spec]) => spec.expression.ref?.startsWith("sheet.") ? [spec.expression.ref.slice(6)] : []));
      const values = fields.filter(([key, spec]) => !direct.has(key) && (spec.type !== "text" || actor.sheet.values[key]));
      if (!stats.length && !values.length) continue;
      const section = node("section", "sheet-section");
      section.append(node("h3", "", label));
      const grid = node("div", "sheet-values");
      for (const [key, spec] of stats) {
        const card = node("div", "sheet-stat");
        card.dataset.stat = key;
        card.append(node("span", "muted", spec.label), node("strong", "", this.format(actor.sheet_statistics[key], spec.signed)));
        grid.append(card);
      }
      for (const [key, spec] of values) {
        const card = node("div", `sheet-value${spec.type === "text" ? " sheet-text" : ""}`);
        const value = spec.type === "choice" ? spec.options[actor.sheet.values[key]] : String(actor.sheet.values[key]);
        card.append(node("span", "muted", spec.label), node(spec.type === "text" ? "p" : "strong", "", value));
        grid.append(card);
      }
      section.append(grid);
      root.append(section);
    }
    const inventory = node("section", "sheet-section sheet-loadout");
    inventory.append(node("h3", "", "Снаряжение"), node("p", "muted", `Предметов: ${actor.items.length} · Bulk ${actor.summary.bulk} · Занято рук: ${actor.summary.hands_used} / ${actor.summary.hands_total}`));
    if (actor.summary.encumbered) inventory.append(node("p", "inventory-warning", "Перегруз: clumsy 1, −10 футов ко всем Скоростям (минимум 5)."));
    const list = node("ul");
    for (const item of actor.items) if (item.state !== this.state().rules.default_state) {
      list.append(node("li", "", `${item.name}${item.quantity > 1 ? ` × ${item.quantity}` : ""} · ${this.state().rules.states[item.state].label}`));
    }
    inventory.append(list);
    const action = node("button", "button-secondary", "К инвентарю");
    action.type = "button";
    action.addEventListener("click", () => this.dialog.close());
    inventory.append(action);
    root.append(inventory);
    if (actor.notes) root.append(node("p", "sheet-notes", actor.notes));
  }

  format(value, signed = false) { return signed && value >= 0 ? `+${value}` : String(value); }

  field(key, spec, value, attribute = false) {
    const label = node("label", "", spec.label);
    let control;
    if (spec.type === "choice") {
      control = node("select");
      for (const [id, text] of Object.entries(spec.options)) control.add(new Option(text, id));
    } else if (spec.type === "text" && spec.max_length > 500) {
      control = node("textarea"); control.rows = 3;
    } else {
      control = node("input"); control.type = spec.type === "text" ? "text" : "number";
    }
    control.name = `${attribute ? "attribute" : "field"}-${key}`;
    if (spec.type === "text") control.maxLength = spec.max_length;
    if (attribute || spec.type === "integer") {
      Object.assign(control, { min: spec.min, max: spec.max, step: "1", required: true });
    }
    control.value = value ?? spec.default;
    if (spec.type === "text" && spec.max_length > 500) label.classList.add("wide-field");
    label.append(control);
    return label;
  }

  renderEditor(profileId, values, attributes) {
    this.activeProfile = profileId;
    const config = this.state().character_rules;
    const actor = this.actor();
    const profile = config.profiles[profileId];
    const select = this.form.elements.profile;
    select.replaceChildren(...Object.entries(config.profiles).filter(([, spec]) => spec.kinds.includes(actor.kind)).map(([id, spec]) => new Option(spec.label, id)));
    select.value = profileId;
    $(".sheet-description", this.dialog).textContent = profile.description;
    $(".sheet-attribute-fields", this.dialog).replaceChildren(...Object.entries(this.attributes()).map(([key, spec]) => this.field(key, spec, attributes[key], true)));
    const groups = $(".sheet-field-groups", this.dialog);
    groups.replaceChildren();
    for (const [key, label] of Object.entries(config.groups)) {
      const fields = Object.entries(profile.fields).filter(([, spec]) => spec.group === key);
      if (!fields.length) continue;
      const section = node("details", "sheet-editor-group");
      section.open = groups.children.length < 2;
      section.append(node("summary", "", label));
      const grid = node("div", "sheet-form-grid");
      grid.append(...fields.map(([key, spec]) => this.field(key, spec, values[key])));
      section.append(grid); groups.append(section);
    }
    this.update(this.disabled);
  }

  readFields() {
    const profile = this.state().character_rules.profiles[this.activeProfile];
    return {
      attributes: Object.fromEntries(Object.keys(this.attributes()).map((key) => [key, Number(this.form.elements[`attribute-${key}`].value)])),
      values: Object.fromEntries(Object.entries(profile.fields).map(([key, spec]) => {
        const value = this.form.elements[`field-${key}`].value;
        return [key, spec.type === "integer" ? Number(value) : value];
      })),
    };
  }
}
