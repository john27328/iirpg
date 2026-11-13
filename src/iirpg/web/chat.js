/* Shared chat UI: game, character assistant and combat use the same implementation. */
const $ = (selector, root = document) => root.querySelector(selector);
const TEMPLATE = `<template id="chat-template">
    <article class="chat">
      <header class="chat-header"><div class="avatar" aria-hidden="true"></div><div><h2></h2><p class="chat-subtitle"></p></div><span class="chat-number" aria-hidden="true"></span></header>
      <div class="message-list" role="log" aria-live="polite" aria-relevant="additions" tabindex="0">
        <div class="empty-state"><div class="empty-icon" aria-hidden="true">↔</div><h3>Ваше приключение начинается здесь</h3><p>Напишите свой первый ход<br>или дождитесь ответа ведущего.</p></div>
      </div>
      <form class="composer" novalidate>
        <details class="dice-panel" hidden><summary>🎲 Кости</summary><div class="dice-fields">
          <label>Формат<select class="dice-system" disabled></select></label>
          <label>Формула<input class="dice-formula" type="text" maxlength="120" placeholder="1d20+5" disabled></label>
          <label>Успех<select class="dice-operator" disabled><option value=">=">≥</option><option value=">">&gt;</option><option value="<=">≤</option><option value="<">&lt;</option><option value="==">=</option><option value="!=">≠</option></select></label>
          <label>Порог<input class="dice-target" type="number" step="1" min="-1000000" max="1000000" disabled></label>
          <div class="dice-pool-fields" hidden><label>Условие на кости<select class="dice-die-operator" disabled><option value=">=">≥</option><option value=">">&gt;</option><option value="<=">≤</option><option value="<">&lt;</option><option value="==">=</option><option value="!=">≠</option></select></label><label>Порог на кости<input class="dice-die-target" type="number" step="1" min="-1000000" max="1000000" disabled></label></div>
          <label class="dice-label-field">Проверка<input class="dice-label" type="text" maxlength="100" placeholder="Например, скрытность" disabled></label>
        </div><div class="dice-actions"><span>Запрос и результат сохранятся в ленте.</span><button class="button-secondary dice-roll" type="button" disabled>Бросить кости</button></div><p class="dice-error form-error" role="alert" hidden></p></details>
        <div class="visibility-control" hidden><label class="visibility-label">Видимость</label><select class="visibility-select" disabled><option value="all">Общее сообщение</option><option value="self">Только игроку</option></select></div><label class="sr-only"></label><div class="composer-field"><textarea rows="2" maxlength="2000" placeholder="Напишите сообщение…" disabled></textarea><button class="send-button" type="submit" aria-label="Отправить сообщение" disabled>↑</button></div><div class="composer-meta"><span>Enter — отправить <span class="meta-separator">·</span> Shift + Enter — новая строка</span><span class="character-count">0 / 2000</span></div><p class="form-error" role="alert" hidden></p>
      </form>
    </article>
  </template>`;
function getChatTemplate() {
  let template = $('#chat-template');
  if (!template) { const host=document.createElement('div');host.innerHTML=TEMPLATE;template=host.firstElementChild;document.body.append(template); }
  return template;
}
export function createChatClass(env) {
return class Chat {
  constructor(key, number, subtitle, options = {}) {
    this.options = options;
    this.key = key;
    this.cursor = 0;
    this.hasMore = false;
    this.busy = false;
    this.retry = null;
    this.ids = new Set();
    this.toolCalls = new Map();
    this.cancelledOffers = new Set();
    this.element = getChatTemplate().content.firstElementChild.cloneNode(true);
    this.element.classList.add(options.id || key);
    $("h2", this.element).textContent = options.title || "Приключение";
    $(".avatar", this.element).textContent = "i";
    $(".chat-number", this.element).textContent = options.badge || "ИГРА";
    $(".chat-subtitle", this.element).textContent = subtitle;
    this.list = $(".message-list", this.element);
    this.input = $("textarea", this.element);
    this.audience = $(".visibility-select", this.element);
    this.audience.id = `visibility-${options.id || key}`;
    $(".visibility-label", this.element).htmlFor = this.audience.id;
    this.input.id = `message-${options.id || key}`;
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
        if (!this.busy && (this.options.enabled?.() ?? env.context().ready)) this.element.querySelector("form").requestSubmit();
      }
    });
    $("form", this.element).addEventListener("submit", (event) => {
      event.preventDefault();
      this.send();
    });
    $(options.container || "#chats").append(this.element);
  }

  update() {
    this.input.disabled = !(this.options.enabled?.() ?? env.context().ready);
    this.audience.disabled = !(this.options.enabled?.() ?? env.context().ready) || this.busy;
    this.button.disabled = !(this.options.enabled?.() ?? env.context().ready) || this.busy || this.options.sendEnabled?.() === false || !this.input.value.trim();
    this.element.querySelectorAll(".reply-choice").forEach((button) => {
      button.disabled = !(this.options.enabled?.() ?? env.context().ready) || this.busy;
    });
    this.element.querySelectorAll(".dice-offer-roll").forEach((button) => {
      button.disabled = !(this.options.enabled?.() ?? env.context().ready) || this.busy;
    });
    this.dicePanel.querySelectorAll("input, select, button").forEach((control) => {
      control.disabled = !(this.options.enabled?.() ?? env.context().ready) || this.busy || !env.diceCatalog();
    });
    $(".character-count", this.element).textContent = `${Array.from(this.input.value).length} / ${this.input.maxLength}`;
    this.options.onUpdate?.(this);
  }

  hasDraft() { return Boolean(this.input.value.trim()); }

  reset() {
    this.thinking?.remove();
    this.ids.clear();
    this.toolCalls.clear();
    this.cancelledOffers.clear();
    this.cursor = 0;
    this.hasMore = false;
    this.retry = null;
    this.diceRetry = null;
    this.diceError.hidden = true;
    this.busy = false;
    this.input.value = "";
    this.audience.value = "all";
    this.error.hidden = true;
    this.list.replaceChildren($(".empty-state", getChatTemplate().content).cloneNode(true));
    this.update();
  }

  receive(message) {
    if(message.metadata?.client_request_id===this.retry?.request_id)this.optimistic?.remove();
    if (message.metadata?.cancelled_offer_id) {
      const id = message.metadata.cancelled_offer_id;
      this.cancelledOffers.add(id);
      const entry = this.toolCalls.get(id);
      if (entry?.offer) this.receiveDice(entry.offer);
    }
    if (this.ids.has(message.id)) return;
    this.ids.add(message.id);
    $(".empty-state", this.list)?.remove();
    if (message.tool === "dice.roll" && ["tool.offer", "tool.call", "tool.result"].includes(message.type)) {
      this.receiveDice(message);
      return;
    }
    const nearBottom = this.list.scrollHeight - this.list.scrollTop - this.list.clientHeight < 90;
    const outgoing = message.sender === this.key || ['intent','clarify_intent'].includes(message.combat?.action);
    const item = document.createElement("div");
    item.className = `message${outgoing ? " outgoing" : ""}`;
    item.dataset.messageId = message.id;
    item.id = `message-${this.key}-${message.id}`;
    const label = document.createElement("span");
    label.className = "message-label";
    label.textContent = outgoing ? "Вы" : ({ first: "Игрок", second: this.options.assistantName || "Ведущий" }[message.sender] || message.sender);
    if (message.combat) label.textContent = `Бой · ${message.combat.actor || "Ведущий"}`;
    if (message.visibility.mode === "selected") label.textContent += " · Ограниченная видимость";
    const text = document.createElement("p");
    text.className = "message-text";
    text.textContent = message.text;
    const time = document.createElement("time");
    time.className = "message-time";
    time.dateTime = message.created_at;
    time.textContent = new Date(message.created_at).toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit" });
    item.append(label, text);
    if(message.metadata?.combat_id){const link=document.createElement('a');link.className='button-secondary';link.textContent='Открыть бой';link.href='/?battle=1#'+env.roomFragment();link.addEventListener('click',e=>{e.preventDefault();env.showBattle();});item.append(link);}
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
        button.disabled = !(this.options.enabled?.() ?? env.context().ready) || this.busy;
        button.addEventListener("click", () => this.send(choice));
        choices.append(button);
      }
      const hint = document.createElement("span");
      hint.className = "reply-hint";
      hint.textContent = "Нажмите на вариант для отправки или напишите свой ответ ниже.";
      item.append(choices, hint);
    }
    this.options.renderExtras?.(message, item, this);
    item.append(time);
    const following = Array.from(this.list.children).find(
      (child) => Number(child.dataset.messageId) > message.id,
    );
    this.list.insertBefore(item, following || null);
    if (nearBottom || outgoing) this.list.scrollTop = this.list.scrollHeight;
  }

  receiveDice(message) {
    const isResult = message.type === "tool.result";
    const isOffer = message.type === "tool.offer";
    const key = message.offer_id || (isResult ? (message.tool_call_id || message.request_id) : message.request_id);
    if (!this.toolCalls.has(key)) this.toolCalls.set(key, { item: document.createElement("div") });
    const entry = this.toolCalls.get(key);
    entry[isOffer ? "offer" : isResult ? "result" : "call"] = message;
    const { offer, call, result, item } = entry;
    const cancelled = offer && this.cancelledOffers.has(offer.request_id);
    const source = offer || call || result;
    const owner = call?.sender || result?.requested_by || offer.recipient;
    const outgoing = offer ? offer.sender === this.key : owner === this.key;
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
    const name = (key) => key === this.key ? "Вы" : ({ first: "Игрок", second: "Ведущий" }[key] || key || "Кости");
    label.textContent = `🎲 ${offer ? `${name(offer.sender)} → ${name(offer.recipient)}` : name(owner)} · ${arguments_.label}`;
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
    outcome.textContent = roll ? (roll.success ? "Успех" : "Неудача") : (offer && !call ? "Ожидает броска" : "Бросок…");
    if(roll&&source.metadata?.session_check){
      const natural=roll.rolls[0].values[0],dc=criterion.target,total=roll.result;
      let degree=total>=dc+10?3:total>=dc?2:total<=dc-10?0:1;
      degree=Math.max(0,Math.min(3,degree+(natural===20?1:natural===1?-1:0)));
      outcome.textContent=['Критический провал','Провал','Успех','Критический успех'][degree];
      item.classList.toggle('dice-success',degree>=2);item.classList.toggle('dice-failure',degree<2);
    }
    line.append(formula, outcome);
    body.append(header);
    if (offer) {
      const description = document.createElement("p");
      description.className = "dice-offer-text";
      description.textContent = offer.text;
      body.append(description);
    }
    body.append(line);
    if (cancelled) outcome.textContent = "Попытка отменена";
    if (offer && !call && !result && !cancelled && offer.recipient === this.key) {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "button-secondary dice-offer-roll";
      button.textContent = "Бросить кости";
      button.disabled = !(this.options.enabled?.() ?? env.context().ready) || this.busy;
      button.addEventListener("click", () => this.acceptDiceOffer(offer));
      const error = document.createElement("p");
      error.className = "form-error dice-offer-error";
      error.setAttribute("role", "alert");
      error.hidden = true;
      body.append(button, error);
      if (offer.metadata?.session_check) {
        const cancel = document.createElement('button');
        cancel.type = 'button'; cancel.className = 'button-secondary dice-offer-roll';
        cancel.textContent = 'Отменить попытку';
        cancel.disabled = button.disabled;
        cancel.addEventListener('click', async () => {
          if (this.busy) return;
          this.busy = true; this.update();
          const epoch = env.context().generation, targetRoom = env.context().room;
          try {
            await env.request(`/api/rooms/${targetRoom}/session/cancel`, {method:'POST', body:JSON.stringify({offer_id:offer.request_id})});
            if (epoch === env.context().generation) await (this.options.refresh ? this.options.refresh() : env.refreshChats(targetRoom,epoch));
          } catch (failure) {
            if (epoch === env.context().generation) { error.textContent = failure.message; error.hidden = false; }
          } finally { if (epoch === env.context().generation) { this.busy = false; this.update(); } }
        });
        body.append(cancel);
      }
    }
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

  async acceptDiceOffer(offer) {
    if (!(this.options.enabled?.() ?? env.context().ready) || this.busy) return;
    const epoch = env.context().generation;
    const targetRoom = env.context().room;
    const errorBox = $(".dice-offer-error", this.toolCalls.get(offer.request_id).item);
    if (errorBox) errorBox.hidden = true;
    this.busy = true;
    this.update();
    try {
      const response = await env.request(`/api/rooms/${targetRoom}/tools/dice`, {
        method: "POST", body: JSON.stringify({ sender: this.key, request_id: offer.roll_request_id,
          offer_id: offer.request_id }),
      });
      if (epoch !== env.context().generation) return;
      this.receive(response.message);
      await (this.options.refresh ? this.options.refresh() : env.refreshChats(targetRoom, epoch));
      if(offer.metadata?.session_check){
        const thinking=document.createElement('p');thinking.className='gm-thinking';thinking.setAttribute('role','status');thinking.textContent='Мастер описывает последствия…';this.thinking=thinking;this.list.after(thinking);
        try{await env.request(`/api/rooms/${targetRoom}/session/react`,{method:'POST',body:JSON.stringify({offer_id:offer.request_id}),timeoutMs:90000});}finally{thinking.remove();}
      }
      env.setConnection(true);
      try { await (this.options.refresh ? this.options.refresh() : env.refreshChats(targetRoom, epoch)); } catch {
        if (epoch === env.context().generation) env.setConnection(false);
      }
    } catch (error) {
      if (epoch !== env.context().generation) return;
      const box = $(".dice-offer-error", this.toolCalls.get(offer.request_id).item);
      if (box) {
        box.textContent = `${error.message} Можно повторить: кости не будут переброшены.`;
        box.hidden = false;
      }
      if(!box){this.error.textContent='Бросок сохранён. Не удалось получить продолжение мастера: '+error.message;this.error.hidden=false;}
      if (!error.status || error.status >= 500) env.setConnection(false);
    } finally {
      if (epoch === env.context().generation) { this.busy = false; this.update(); }
    }
  }

  read(roomId, after = this.cursor) {
    if (this.options.read) return this.options.read(after);
    return env.request(`/api/rooms/${roomId}/messages?chat=${this.key}&after=${after}`);
  }

  applyPage(page) {
    this.options.onPage?.(page);
    page.messages.forEach((message) => this.receive(message));
    this.cursor = Math.max(this.cursor, page.next_cursor);
    this.hasMore = page.has_more;
  }

  async send(choice = null) {
    const text = choice ?? this.input.value.trim();
    if (!(this.options.enabled?.() ?? env.context().ready) || this.options.sendEnabled?.() === false || this.busy || !text) return;
    const epoch = env.context().generation;
    const targetRoom = env.context().room;
    const visibility = this.audience.value === "self"
      ? { mode: "selected", chat_ids: [this.key] }
      : { mode: "all", chat_ids: [] };
    if (!this.retry || this.retry.text !== text || JSON.stringify(this.retry.visibility) !== JSON.stringify(visibility)) {
      this.retry = { sender: this.key, type: "chat.message", text, visibility, metadata: {}, request_id: env.createRequestId() };
    }
    this.busy = true;
    this.error.hidden = true;
    const draft=text;
    const optimistic=document.createElement('div');optimistic.className='message outgoing';optimistic.textContent='Вы: '+text;
    const thinking=document.createElement('p');thinking.className='gm-thinking';thinking.setAttribute('role','status');thinking.textContent=this.options.thinking || 'Мастер думает…';
    if(this.key==='first'&&visibility.mode==='all'){
      this.list.querySelector('.empty-state')?.remove();this.optimistic=optimistic;this.list.append(optimistic);this.thinking=thinking;this.list.after(thinking);this.list.scrollTop=this.list.scrollHeight;
      if(choice===null)this.input.value='';
    }
    this.update();
    try {
      if (this.options.send) {
        await this.options.send(text, this.retry, this);
      } else if(this.key==='first'&&visibility.mode==='all'){
        if(!this.retry.sessionCommand){
          const [story,roster]=await Promise.all([env.request(`/api/rooms/${targetRoom}/session`),env.request(`/api/rooms/${targetRoom}/roster`)]);
          const hero=story.character_id||roster.characters.find(c=>c.kind==='pc')?.id;
          if(!hero)throw new Error('Сначала создайте персонажа во вкладке персонажей.');
          this.retry.sessionCommand={revision:story.revision,character_id:hero,text};
        }
        await env.request(`/api/rooms/${targetRoom}/session`,{method:'POST',body:JSON.stringify(this.retry.sessionCommand),headers:{'Idempotency-Key':this.retry.request_id},timeoutMs:120000});
      }else await env.request(`/api/rooms/${targetRoom}/messages`, {
        method: "POST", body: JSON.stringify(this.retry),
      });
      if (epoch !== env.context().generation) return;
      if (choice === null && this.input.value === draft) this.input.value = "";
      this.retry = null;
      env.setConnection(true);
      try { await (this.options.refresh ? this.options.refresh() : env.refreshChats(targetRoom, epoch)); } catch {
        if (epoch === env.context().generation) env.setConnection(false);
      }
    } catch (error) {
      if (epoch !== env.context().generation) return;
      if(choice===null&&!this.input.value)this.input.value=draft;
      if(error.status)this.retry=null;
      this.error.textContent = choice === null
        ? `${error.message} Текст сохранён в поле ввода.`
        : `${error.message} Отправка не подтверждена. Нажмите вариант ещё раз для повтора.`;
      this.error.hidden = false;
      env.setConnection(false);
    } finally {
      optimistic.remove();thinking.remove();
      if (epoch === env.context().generation) {
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
    const definition = env.diceCatalog()?.systems[this.diceSystem.value];
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
    if (!(this.options.enabled?.() ?? env.context().ready) || this.busy || !env.diceCatalog()) return;
    this.diceError.hidden = true;
    const integer = (selector) => {
      const value = $(selector, this.element).value;
      if (!String(value).trim() || !Number.isInteger(Number(value))) throw new Error("Порог должен быть целым числом.");
      return Number(value);
    };
    let arguments_;
    try {
      const success = { operator: $(".dice-operator", this.element).value, target: integer(".dice-target") };
      if (env.diceCatalog().systems[this.diceSystem.value].aggregation === "count") {
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
      this.diceRetry = { signature, payload: { sender: this.key, request_id: env.createRequestId(), arguments: arguments_, visibility } };
    }
    const epoch = env.context().generation;
    const targetRoom = env.context().room;
    this.busy = true;
    this.update();
    try {
      await env.request(`/api/rooms/${targetRoom}/tools/dice`, { method: "POST", body: JSON.stringify(this.diceRetry.payload) });
      if (epoch !== env.context().generation) return;
      this.diceRetry = null;
      env.setConnection(true);
      try { await (this.options.refresh ? this.options.refresh() : env.refreshChats(targetRoom, epoch)); } catch {
        if (epoch === env.context().generation) env.setConnection(false);
      }
    } catch (error) {
      if (epoch !== env.context().generation) return;
      this.diceError.textContent = `${error.message} Повтор с теми же параметрами не перебросит кости.`;
      this.diceError.hidden = false;
      if (!error.status || error.status >= 500) env.setConnection(false);
    } finally {
      if (epoch === env.context().generation) { this.busy = false; this.update(); }
    }
  }
}

}
