"use strict";

const elements = {
  refresh: document.querySelector("#refresh-button"),
  healthIndicator: document.querySelector("#health-indicator"),
  healthDetail: document.querySelector("#health-detail"),
  agentIndicator: document.querySelector("#agent-indicator"),
  agentDetail: document.querySelector("#agent-detail"),
  problemsCount: document.querySelector("#problems-count"),
  problemsContent: document.querySelector("#problems-content"),
  ideasCount: document.querySelector("#ideas-count"),
  ideasContent: document.querySelector("#ideas-content"),
  ideaForm: document.querySelector("#idea-form"),
  ideaInput: document.querySelector("#idea-input"),
  ideaApiKey: document.querySelector("#idea-api-key"),
  ideaSubmit: document.querySelector("#idea-submit"),
  ideaState: document.querySelector("#idea-state"),
  form: document.querySelector("#chat-form"),
  input: document.querySelector("#chat-input"),
  send: document.querySelector("#send-button"),
  chatState: document.querySelector("#chat-state"),
  messages: document.querySelector("#messages"),
  publicActivityCount: document.querySelector("#public-activity-count"),
  publicActivityContent: document.querySelector("#public-activity-content"),
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

async function refreshIdeas() {
  elements.ideasContent.textContent = "Wird geladen …";
  try {
    const ideas = await requestJson("/ideas");
    elements.ideasCount.textContent = String(ideas.count);
    renderItems(elements.ideasContent, ideas.ideas, "Keine Ideen erfasst.", { title: "text", meta: "section" });
  } catch (error) {
    elements.ideasCount.textContent = "!";
    showError(elements.ideasContent, error.message);
  }
}

async function loadOverview() {
  elements.refresh.disabled = true;
  elements.refresh.textContent = "Lädt …";
  setIndicator(elements.healthIndicator, "Lädt …", "pending");
  setIndicator(elements.agentIndicator, "Lädt …", "pending");
  elements.problemsContent.textContent = "Wird geladen …";

  const [health, status, problems] = await Promise.allSettled([
    requestJson("/health"),
    requestJson("/status"),
    requestJson("/problems"),
  ]);

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

  await refreshIdeas();

  elements.refresh.disabled = false;
  elements.refresh.textContent = "Aktualisieren";
}

function renderPublicActivity(items) {
  elements.publicActivityContent.replaceChildren();
  if (!items.length) {
    elements.publicActivityContent.textContent = "Noch keine Aktivität vorhanden.";
    return;
  }
  const list = document.createElement("ul");
  list.className = "item-list";
  for (const item of items) {
    const row = document.createElement("li");
    const title = document.createElement("div");
    title.className = "item-title";
    title.textContent = `${item.phase} · ${item.message}`;
    const meta = document.createElement("div");
    meta.className = "item-meta";
    const details = [item.timestamp, `Experiment ${item.experiment_id.slice(0, 8)}`];
    if (item.mailbox_task_id) details.push(`Mailbox ${item.mailbox_task_id.slice(0, 8)}`);
    if (item.test_summary) details.push(item.test_summary);
    if (item.changed_files.length) details.push(`Dateien: ${item.changed_files.join(", ")}`);
    if (item.failure_reason) details.push(item.failure_reason);
    meta.textContent = details.join(" · ");
    row.append(title, meta);
    list.append(row);
  }
  elements.publicActivityContent.append(list);
}

async function loadPublicActivity() {
  try {
    const data = await requestJson("/activity/public");
    elements.publicActivityCount.textContent = String(data.count);
    renderPublicActivity(data.activity);
  } catch (error) {
    elements.publicActivityCount.textContent = "!";
    showError(elements.publicActivityContent, error.message);
  }
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

async function submitIdea(event) {
  event.preventDefault();
  const text = elements.ideaInput.value.trim();
  if (!text || elements.ideaSubmit.disabled) return;

  elements.ideaSubmit.disabled = true;
  elements.ideaSubmit.textContent = "Speichert …";
  elements.ideaState.textContent = "";

  try {
    await requestJson("/ideas", {
      method: "POST",
      headers: { "x-api-key": elements.ideaApiKey.value },
      body: JSON.stringify({ text }),
    });
    elements.ideaInput.value = "";
    elements.ideaState.textContent = "Idee gespeichert.";
    await refreshIdeas();
  } catch (error) {
    elements.ideaState.textContent = error.message;
  } finally {
    elements.ideaSubmit.disabled = false;
    elements.ideaSubmit.textContent = "Idee hinzufügen";
  }
}

elements.refresh.addEventListener("click", loadOverview);
elements.form.addEventListener("submit", sendMessage);
elements.ideaForm.addEventListener("submit", submitIdea);
elements.input.addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey) {
    event.preventDefault();
    elements.form.requestSubmit();
  }
});

// ---------------------------------------------------------------------------
// Task approvals
// ---------------------------------------------------------------------------

const taskApiKeyInput = document.querySelector("#task-api-key");
const taskLoadButton = document.querySelector("#task-load-button");
const taskApprovalsContent = document.querySelector("#task-approvals-content");
const taskApprovalsCount = document.querySelector("#task-approvals-count");

let taskRefreshTimer = null;

async function loadTaskApprovals() {
  const apiKey = taskApiKeyInput ? taskApiKeyInput.value.trim() : "";
  if (!apiKey) {
    taskApprovalsContent.textContent = "API-Key eingeben und Laden klicken.";
    return;
  }
  taskApprovalsContent.textContent = "Wird geladen …";
  try {
    const data = await requestJson("/mailbox/tasks?limit=20", {
      headers: { "x-api-key": apiKey },
    });
    taskApprovalsCount.textContent = String(data.count);
    renderTaskApprovals(data.tasks, apiKey);
    if (!taskRefreshTimer) {
      taskRefreshTimer = setInterval(() => loadTaskApprovals(), 30000);
    }
  } catch (error) {
    taskApprovalsCount.textContent = "!";
    showError(taskApprovalsContent, error.message);
    if (taskRefreshTimer) { clearInterval(taskRefreshTimer); taskRefreshTimer = null; }
  }
}

function renderTaskApprovals(tasks, apiKey) {
  taskApprovalsContent.replaceChildren();
  if (!tasks.length) {
    taskApprovalsContent.textContent = "Keine Task Approvals vorhanden.";
    return;
  }
  const list = document.createElement("ul");
  list.className = "item-list";
  for (const task of tasks) {
    const li = document.createElement("li");

    const title = document.createElement("div");
    title.className = "item-title";
    const preview = (task.task_body || "").slice(0, 200);
    title.textContent = `[${task.status}] ${task.sender} — ${preview}${preview.length < (task.task_body || "").length ? "…" : ""}`;
    li.append(title);

    const meta = document.createElement("div");
    meta.className = "item-meta";
    let metaText = `Empfangen: ${task.received_at}`;
    if (task.experiment_id) metaText += ` · Experiment: ${task.experiment_id.slice(0, 8)}`;
    if (task.result_summary) metaText += ` · ${task.result_summary}`;
    meta.textContent = metaText;
    li.append(meta);

    if (task.status === "pending_approval") {
      const actions = document.createElement("div");
      actions.style.marginTop = "6px";
      actions.style.display = "flex";
      actions.style.gap = "8px";

      const approveBtn = document.createElement("button");
      approveBtn.textContent = "Genehmigen";
      approveBtn.type = "button";
      approveBtn.addEventListener("click", async () => {
        approveBtn.disabled = true;
        approveBtn.textContent = "…";
        try {
          await requestJson(`/mailbox/tasks/${task.id}/approve`, {
            method: "POST",
            headers: { "x-api-key": apiKey },
          });
          await loadTaskApprovals();
        } catch (err) {
          approveBtn.textContent = "Fehler";
          approveBtn.disabled = false;
          alert(err.message);
        }
      });

      const rejectBtn = document.createElement("button");
      rejectBtn.textContent = "Ablehnen";
      rejectBtn.type = "button";
      rejectBtn.className = "secondary";
      rejectBtn.addEventListener("click", async () => {
        const reason = prompt("Ablehnungsgrund (1–500 Zeichen):");
        if (!reason || !reason.trim()) return;
        rejectBtn.disabled = true;
        rejectBtn.textContent = "…";
        try {
          await requestJson(`/mailbox/tasks/${task.id}/reject`, {
            method: "POST",
            headers: { "x-api-key": apiKey },
            body: JSON.stringify({ reason: reason.trim() }),
          });
          await loadTaskApprovals();
        } catch (err) {
          rejectBtn.textContent = "Fehler";
          rejectBtn.disabled = false;
          alert(err.message);
        }
      });

      actions.append(approveBtn, rejectBtn);
      li.append(actions);
    }

    list.append(li);
  }
  taskApprovalsContent.append(list);
}

if (taskLoadButton) {
  taskLoadButton.addEventListener("click", loadTaskApprovals);
}

addMessage("Hallo! Ich beantworte Fragen zu Status, Problemen und Ideen.", "agent");
loadOverview();
loadPublicActivity();
setInterval(loadPublicActivity, 10000);
