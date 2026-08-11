"use strict";

const elements = {
  refresh: document.querySelector("#refresh-button"),
  healthIndicator: document.querySelector("#health-indicator"),
  healthDetail: document.querySelector("#health-detail"),
  agentIndicator: document.querySelector("#agent-indicator"),
  agentDetail: document.querySelector("#agent-detail"),
  problemsCount: document.querySelector("#problems-count"),
  problemsContent: document.querySelector("#problems-content"),
  todosCount: document.querySelector("#todos-count"),
  todosContent: document.querySelector("#todos-content"),
  form: document.querySelector("#chat-form"),
  input: document.querySelector("#chat-input"),
  send: document.querySelector("#send-button"),
  chatState: document.querySelector("#chat-state"),
  messages: document.querySelector("#messages"),
};

async function requestJson(url, options = {}) {
  let response;
  try {
    response = await fetch(url, {
      ...options,
      headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    });
  } catch {
    throw new Error("Der Server ist derzeit nicht erreichbar.");
  }

  let body = null;
  try {
    body = await response.json();
  } catch {
    if (response.ok) throw new Error("Der Server hat eine ungültige Antwort gesendet.");
  }

  if (!response.ok) {
    const detail = body && typeof body.detail === "string" ? body.detail : `HTTP ${response.status}`;
    throw new Error(`Anfrage fehlgeschlagen: ${detail}`);
  }
  return body;
}

function setIndicator(indicator, label, state) {
  indicator.textContent = label;
  indicator.className = `indicator ${state}`;
}

function showError(container, message) {
  container.replaceChildren();
  const error = document.createElement("p");
  error.className = "error-text";
  error.textContent = message;
  container.append(error);
}

function renderItems(container, items, emptyMessage, fields) {
  container.replaceChildren();
  if (!items.length) {
    container.textContent = emptyMessage;
    return;
  }

  const list = document.createElement("ul");
  list.className = "item-list";
  for (const item of items) {
    const row = document.createElement("li");
    const title = document.createElement("div");
    title.className = "item-title";
    title.textContent = item[fields.title] || "Ohne Beschreibung";
    row.append(title);
    if (item[fields.meta]) {
      const meta = document.createElement("div");
      meta.className = "item-meta";
      meta.textContent = item[fields.meta];
      row.append(meta);
    }
    list.append(row);
  }
  container.append(list);
}

async function loadOverview() {
  elements.refresh.disabled = true;
  elements.refresh.textContent = "Lädt …";
  setIndicator(elements.healthIndicator, "Lädt …", "pending");
  setIndicator(elements.agentIndicator, "Lädt …", "pending");
  elements.problemsContent.textContent = "Wird geladen …";
  elements.todosContent.textContent = "Wird geladen …";

  const results = await Promise.allSettled([
    requestJson("/health"),
    requestJson("/status"),
    requestJson("/problems"),
    requestJson("/todos"),
  ]);

  const [health, status, problems, todos] = results;
  if (health.status === "fulfilled") {
    const ok = health.value.status === "ok";
    setIndicator(elements.healthIndicator, ok ? "Online" : "Unbekannt", ok ? "good" : "bad");
    elements.healthDetail.textContent = ok ? "API ist erreichbar." : "Unerwarteter Health-Status.";
  } else {
    setIndicator(elements.healthIndicator, "Fehler", "bad");
    elements.healthDetail.textContent = health.reason.message;
  }

  if (status.status === "fulfilled") {
    setIndicator(elements.agentIndicator, status.value.agent_status, "good");
    elements.agentDetail.textContent = `API ${status.value.api_version} · ${status.value.components.length} Komponenten`;
  } else {
    setIndicator(elements.agentIndicator, "Fehler", "bad");
    elements.agentDetail.textContent = status.reason.message;
  }

  if (problems.status === "fulfilled") {
    elements.problemsCount.textContent = String(problems.value.count);
    renderItems(elements.problemsContent, problems.value.problems, "Keine aktuellen Probleme.", { title: "summary", meta: "timestamp" });
  } else {
    elements.problemsCount.textContent = "!";
    showError(elements.problemsContent, problems.reason.message);
  }

  if (todos.status === "fulfilled") {
    elements.todosCount.textContent = String(todos.value.count);
    renderItems(elements.todosContent, todos.value.todos, "Keine offenen Todos.", { title: "text", meta: "section" });
  } else {
    elements.todosCount.textContent = "!";
    showError(elements.todosContent, todos.reason.message);
  }

  elements.refresh.disabled = false;
  elements.refresh.textContent = "Aktualisieren";
}

function addMessage(text, type) {
  const message = document.createElement("div");
  message.className = `message ${type}`;
  message.textContent = text;
  elements.messages.append(message);
  elements.messages.scrollTop = elements.messages.scrollHeight;
}

async function sendMessage(event) {
  event.preventDefault();
  const message = elements.input.value.trim();
  if (!message || elements.send.disabled) return;

  addMessage(message, "user");
  elements.input.value = "";
  elements.input.disabled = true;
  elements.send.disabled = true;
  elements.send.textContent = "Sendet …";
  elements.chatState.textContent = "Antwort wird geladen …";

  try {
    const response = await requestJson("/chat", {
      method: "POST",
      body: JSON.stringify({ message }),
    });
    addMessage(response.reply, "agent");
    elements.chatState.textContent = "";
  } catch (error) {
    addMessage(error.message, "error");
    elements.chatState.textContent = "Senden fehlgeschlagen";
  } finally {
    elements.input.disabled = false;
    elements.send.disabled = false;
    elements.send.textContent = "Senden";
    elements.input.focus();
  }
}

elements.refresh.addEventListener("click", loadOverview);
elements.form.addEventListener("submit", sendMessage);
elements.input.addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey) {
    event.preventDefault();
    elements.form.requestSubmit();
  }
});

addMessage("Hallo! Ich beantworte Fragen zu Status, Problemen und offenen Todos.", "agent");
loadOverview();
