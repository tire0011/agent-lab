"use strict";

// ---------------------------------------------------------------------------
// Shared state & helpers
// ---------------------------------------------------------------------------

const API_KEY_STORAGE_KEY = "agentLabApiKey";

const state = {
  apiKey: null,
};

function authHeaders() {
  return state.apiKey ? { "x-api-key": state.apiKey } : {};
}

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

function setIndicator(indicator, label, kind) {
  indicator.textContent = label;
  indicator.className = `indicator ${kind}`;
}

function showError(container, message) {
  container.replaceChildren();
  const error = document.createElement("p");
  error.className = "error-text";
  error.textContent = message;
  container.append(error);
}

/**
 * Render a list of items as mobile-friendly "cards": a title line plus an
 * optional list of meta lines. Every value is set via textContent, never
 * innerHTML, so nothing from the API can inject markup.
 */
function renderCardList(container, items, emptyMessage, buildCard) {
  container.replaceChildren();
  if (!items.length) {
    container.textContent = emptyMessage;
    return;
  }
  const list = document.createElement("ul");
  list.className = "item-list";
  items.forEach((item, index) => {
    const li = document.createElement("li");
    buildCard(li, item, index);
    list.append(li);
  });
  container.append(list);
}

function addTitle(li, text) {
  const title = document.createElement("div");
  title.className = "item-title";
  title.textContent = text || "Ohne Beschreibung";
  li.append(title);
  return title;
}

function addMetaLine(li, text) {
  if (!text) return;
  const meta = document.createElement("div");
  meta.className = "item-meta";
  meta.textContent = text;
  li.append(meta);
}

function renderItems(container, items, emptyMessage, fields) {
  renderCardList(container, items, emptyMessage, (li, item) => {
    addTitle(li, item[fields.title]);
    addMetaLine(li, item[fields.meta]);
  });
}

// ---------------------------------------------------------------------------
// Tab navigation
// ---------------------------------------------------------------------------

const tabButtons = Array.from(document.querySelectorAll(".tab-button"));
const tabPanels = Array.from(document.querySelectorAll(".tab-panel"));

function activateTab(name) {
  for (const button of tabButtons) {
    const active = button.dataset.tab === name;
    button.classList.toggle("active", active);
    button.setAttribute("aria-selected", active ? "true" : "false");
  }
  for (const panel of tabPanels) {
    panel.hidden = panel.id !== `tab-${name}`;
  }
  if (name === "tasks") loadTaskApprovals();
  if (name === "mailbox") loadMailboxMessages();
  if (name === "logs") {
    loadLogsExperimentOptions();
    loadLogs();
    startLogsAutoRefresh();
  } else {
    stopLogsAutoRefresh();
  }
}

for (const button of tabButtons) {
  button.addEventListener("click", () => activateTab(button.dataset.tab));
}

// ---------------------------------------------------------------------------
// Central API-key box
// ---------------------------------------------------------------------------

const apiKeyElements = {
  form: document.querySelector("#api-key-form"),
  input: document.querySelector("#api-key-input"),
  status: document.querySelector("#api-key-status"),
  forget: document.querySelector("#api-key-forget"),
};

function setApiKeyStatus(label, kind) {
  setIndicator(apiKeyElements.status, label, kind);
}

function onApiKeyChanged() {
  // Re-load anything that behaves differently with/without a key.
  refreshIdeas();
  if (!document.querySelector("#tab-tasks").hidden) loadTaskApprovals();
  if (!document.querySelector("#tab-logs").hidden) loadLogs();
}

async function connectApiKey(key) {
  if (!key) {
    setApiKeyStatus("Nicht verbunden", "pending");
    return;
  }
  setApiKeyStatus("Prüfe …", "pending");
  try {
    await requestJson("/secure-health", { headers: { "x-api-key": key } });
    state.apiKey = key;
    sessionStorage.setItem(API_KEY_STORAGE_KEY, key);
    setApiKeyStatus("Verbunden", "good");
    onApiKeyChanged();
  } catch {
    state.apiKey = null;
    sessionStorage.removeItem(API_KEY_STORAGE_KEY);
    setApiKeyStatus("Ungültiger API-Key", "bad");
  }
}

apiKeyElements.form.addEventListener("submit", (event) => {
  event.preventDefault();
  const key = apiKeyElements.input.value.trim();
  apiKeyElements.input.value = "";
  connectApiKey(key);
});

apiKeyElements.forget.addEventListener("click", () => {
  state.apiKey = null;
  sessionStorage.removeItem(API_KEY_STORAGE_KEY);
  apiKeyElements.input.value = "";
  setApiKeyStatus("Nicht verbunden", "pending");
  onApiKeyChanged();
});

// ---------------------------------------------------------------------------
// Übersicht: status, problems, ideas
// ---------------------------------------------------------------------------

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
  ideaSubmit: document.querySelector("#idea-submit"),
  ideaState: document.querySelector("#idea-state"),
  form: document.querySelector("#chat-form"),
  input: document.querySelector("#chat-input"),
  send: document.querySelector("#send-button"),
  chatState: document.querySelector("#chat-state"),
  messages: document.querySelector("#messages"),
  publicActivityCount: document.querySelector("#public-activity-count"),
  publicActivityContent: document.querySelector("#public-activity-content"),
  diagnosticsCount: document.querySelector("#diagnostics-count"),
  diagnosticsContent: document.querySelector("#diagnostics-content"),
};

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
  renderCardList(elements.publicActivityContent, items, "Noch keine Aktivität vorhanden.", (li, item) => {
    addTitle(li, `${item.phase} · ${item.message}`);
    const details = [item.timestamp, `Experiment ${item.experiment_id.slice(0, 8)}`];
    if (item.mailbox_task_id) details.push(`Mailbox ${item.mailbox_task_id.slice(0, 8)}`);
    if (item.test_summary) details.push(item.test_summary);
    if (item.changed_files.length) details.push(`Dateien: ${item.changed_files.join(", ")}`);
    if (item.failure_reason) details.push(item.failure_reason);
    addMetaLine(li, details.join(" · "));
  });
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

function renderDiagnostics(data) {
  const experiments = data.experiments || [];
  renderCardList(elements.diagnosticsContent, experiments, "Noch keine Experimente vorhanden.", (li, item) => {
    addTitle(li, `${item.phase} · Experiment ${item.id.slice(0, 8)}`);
    const details = [item.created_at];
    if (item.policy_rule) details.push(`Regel: ${item.policy_rule}`);
    if (item.rejection_reason) details.push(item.rejection_reason);
    if (item.failure_summary) details.push(item.failure_summary);
    if (item.test_command) details.push(item.test_command);
    if (item.test_exit_code !== null && item.test_exit_code !== undefined) {
      details.push(`Exit-Code ${item.test_exit_code}`);
    }
    if (item.changed_files.length) details.push(`Dateien: ${item.changed_files.join(", ")}`);
    addMetaLine(li, details.join(" · "));
  });
}

async function loadDiagnostics() {
  try {
    const data = await requestJson("/diagnostics/public");
    elements.diagnosticsCount.textContent = String((data.experiments || []).length);
    renderDiagnostics(data);
    return data;
  } catch (error) {
    elements.diagnosticsCount.textContent = "!";
    showError(elements.diagnosticsContent, error.message);
    return null;
  }
}

// ---------------------------------------------------------------------------
// Chat (unchanged behaviour)
// ---------------------------------------------------------------------------

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

  if (!state.apiKey) {
    elements.ideaState.textContent = "Bitte zuerst oben den API-Key eingeben.";
    return;
  }

  elements.ideaSubmit.disabled = true;
  elements.ideaSubmit.textContent = "Speichert …";
  elements.ideaState.textContent = "";

  try {
    await requestJson("/ideas", {
      method: "POST",
      headers: authHeaders(),
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
// Aufträge: mailbox task approvals + experiment detail
// ---------------------------------------------------------------------------

const taskElements = {
  loadButton: document.querySelector("#task-load-button"),
  content: document.querySelector("#task-approvals-content"),
  count: document.querySelector("#task-approvals-count"),
  detailSection: document.querySelector("#task-detail-section"),
  detailContent: document.querySelector("#task-detail-content"),
  detailClose: document.querySelector("#task-detail-close"),
};

async function loadTaskApprovals() {
  if (!state.apiKey) {
    taskElements.content.textContent = "Bitte oben den API-Key eingeben, um Aufträge zu sehen.";
    taskElements.count.textContent = "–";
    return;
  }
  taskElements.content.textContent = "Wird geladen …";
  try {
    const data = await requestJson("/mailbox/tasks?limit=20", { headers: authHeaders() });
    taskElements.count.textContent = String(data.count);
    await renderTaskApprovals(data.tasks);
  } catch (error) {
    taskElements.count.textContent = "!";
    showError(taskElements.content, error.message);
  }
}

async function fetchExperimentSummary(experimentId) {
  try {
    const path = state.apiKey ? `/experiments/${experimentId}` : `/experiments/${experimentId}/public`;
    return await requestJson(path, { headers: authHeaders() });
  } catch {
    return null;
  }
}

async function renderTaskApprovals(tasks) {
  if (!tasks.length) {
    taskElements.content.textContent = "Keine Mailbox-Aufträge vorhanden.";
    return;
  }

  const experimentSummaries = await Promise.all(
    tasks.map((task) => (task.experiment_id ? fetchExperimentSummary(task.experiment_id) : Promise.resolve(null)))
  );

  renderCardList(taskElements.content, tasks, "Keine Mailbox-Aufträge vorhanden.", (li, task, index) => {
    const preview = (task.task_body || "").slice(0, 160);
    const truncated = preview.length < (task.task_body || "").length;
    addTitle(li, `[${task.status}] ${task.sender} — ${preview}${truncated ? "…" : ""}`);

    const metaParts = [`Empfangen: ${task.received_at}`];
    if (task.result_summary) metaParts.push(task.result_summary);
    addMetaLine(li, metaParts.join(" · "));

    const exp = experimentSummaries[index];
    if (exp) {
      const phase = exp.phase || exp.status;
      const expParts = [`Phase: ${phase}`];
      if (exp.test_summary) expParts.push(exp.test_summary);
      if (exp.changed_files && exp.changed_files.length) expParts.push(`Dateien: ${exp.changed_files.join(", ")}`);
      addMetaLine(li, expParts.join(" · "));

      const openBtn = document.createElement("button");
      openBtn.type = "button";
      openBtn.className = "secondary";
      openBtn.textContent = "Details anzeigen";
      openBtn.addEventListener("click", () => showExperimentDetail(task.experiment_id));
      const actions = document.createElement("div");
      actions.className = "card-actions";
      actions.append(openBtn);
      li.append(actions);
    }

    if (task.status === "pending_approval") {
      const actions = document.createElement("div");
      actions.className = "card-actions";

      const approveBtn = document.createElement("button");
      approveBtn.textContent = "Genehmigen";
      approveBtn.type = "button";
      approveBtn.addEventListener("click", async () => {
        approveBtn.disabled = true;
        approveBtn.textContent = "…";
        try {
          await requestJson(`/mailbox/tasks/${task.id}/approve`, {
            method: "POST",
            headers: authHeaders(),
          });
          await loadTaskApprovals();
        } catch (err) {
          approveBtn.textContent = "Fehler";
          approveBtn.disabled = false;
          window.alert(err.message);
        }
      });

      const rejectBtn = document.createElement("button");
      rejectBtn.textContent = "Ablehnen";
      rejectBtn.type = "button";
      rejectBtn.className = "secondary";
      rejectBtn.addEventListener("click", async () => {
        const reason = window.prompt("Ablehnungsgrund (1–500 Zeichen):");
        if (!reason || !reason.trim()) return;
        rejectBtn.disabled = true;
        rejectBtn.textContent = "…";
        try {
          await requestJson(`/mailbox/tasks/${task.id}/reject`, {
            method: "POST",
            headers: authHeaders(),
            body: JSON.stringify({ reason: reason.trim() }),
          });
          await loadTaskApprovals();
        } catch (err) {
          rejectBtn.textContent = "Fehler";
          rejectBtn.disabled = false;
          window.alert(err.message);
        }
      });

      actions.append(approveBtn, rejectBtn);
      li.append(actions);
    }
  });
}

async function showExperimentDetail(experimentId) {
  taskElements.detailSection.hidden = false;
  taskElements.detailContent.textContent = "Wird geladen …";
  const exp = await fetchExperimentSummary(experimentId);
  if (!exp) {
    showError(taskElements.detailContent, "Experiment konnte nicht geladen werden.");
    return;
  }
  taskElements.detailContent.replaceChildren();
  const lines = [
    ["Phase", exp.phase || exp.status],
    ["Erstellt", exp.created_at],
    ["Beendet", exp.ended_at || "–"],
    ["Test-Ergebnis", exp.test_summary || (exp.test_exit_code !== undefined ? `Exit-Code ${exp.test_exit_code}` : "–")],
    ["Geänderte Dateien", (exp.changed_files || []).join(", ") || "–"],
  ];
  if (exp.policy_rule) lines.push(["Regel", exp.policy_rule]);
  if (exp.failure_reason || exp.rejection_reason) {
    lines.push(["Hinweis", exp.failure_reason || exp.rejection_reason]);
  }
  if (exp.test_command) {
    const cmd = Array.isArray(exp.test_command) ? exp.test_command.join(" ") : exp.test_command;
    lines.push(["Testbefehl", cmd]);
  }
  const dl = document.createElement("dl");
  dl.className = "detail-list";
  for (const [label, value] of lines) {
    const dt = document.createElement("dt");
    dt.textContent = label;
    const dd = document.createElement("dd");
    dd.textContent = value;
    dl.append(dt, dd);
  }
  taskElements.detailContent.append(dl);
}

taskElements.loadButton.addEventListener("click", loadTaskApprovals);
taskElements.detailClose.addEventListener("click", () => {
  taskElements.detailSection.hidden = true;
  taskElements.detailContent.replaceChildren();
});

// ---------------------------------------------------------------------------
// Inbox / Outbox
// ---------------------------------------------------------------------------

const mailboxElements = {
  refreshButton: document.querySelector("#mailbox-refresh-button"),
  inboxContent: document.querySelector("#mailbox-inbox-content"),
  inboxCount: document.querySelector("#mailbox-inbox-count"),
  outboxContent: document.querySelector("#mailbox-outbox-content"),
  outboxCount: document.querySelector("#mailbox-outbox-count"),
};

function renderMailboxEntries(container, entries, emptyMessage) {
  renderCardList(container, entries, emptyMessage, (li, entry) => {
    addTitle(li, `${entry.title} · ${entry.status}`);
    const details = [entry.timestamp];
    if (entry.sender) details.push(entry.sender);
    details.push(entry.type);
    addMetaLine(li, details.join(" · "));
    if (entry.preview) addMetaLine(li, entry.preview);
  });
}

async function loadMailboxMessages() {
  mailboxElements.inboxContent.textContent = "Wird geladen …";
  mailboxElements.outboxContent.textContent = "Wird geladen …";
  try {
    const data = await requestJson("/mailbox/messages?limit=20");
    mailboxElements.inboxCount.textContent = String(data.inbox_count);
    mailboxElements.outboxCount.textContent = String(data.outbox_count);
    renderMailboxEntries(mailboxElements.inboxContent, data.inbox, "Keine Inbox-Nachrichten vorhanden.");
    renderMailboxEntries(mailboxElements.outboxContent, data.outbox, "Keine Outbox-Nachrichten vorhanden.");
  } catch (error) {
    mailboxElements.inboxCount.textContent = "!";
    mailboxElements.outboxCount.textContent = "!";
    showError(mailboxElements.inboxContent, error.message);
    showError(mailboxElements.outboxContent, error.message);
  }
}

mailboxElements.refreshButton.addEventListener("click", loadMailboxMessages);

// ---------------------------------------------------------------------------
// Logs
// ---------------------------------------------------------------------------

const logsElements = {
  refreshButton: document.querySelector("#logs-refresh-button"),
  experimentSelect: document.querySelector("#logs-experiment-select"),
  mailboxTaskInput: document.querySelector("#logs-mailbox-task-input"),
  content: document.querySelector("#logs-content"),
  count: document.querySelector("#logs-count"),
  autoRefreshState: document.querySelector("#logs-auto-refresh-state"),
};

let logsAutoRefreshTimer = null;

function startLogsAutoRefresh() {
  if (logsAutoRefreshTimer) return;
  logsAutoRefreshTimer = setInterval(loadLogs, 10000);
}

function stopLogsAutoRefresh() {
  if (!logsAutoRefreshTimer) return;
  clearInterval(logsAutoRefreshTimer);
  logsAutoRefreshTimer = null;
}

async function loadLogsExperimentOptions() {
  const previousValue = logsElements.experimentSelect.value;
  try {
    const data = await requestJson("/diagnostics/public?limit=50");
    const experiments = data.experiments || [];
    logsElements.experimentSelect.replaceChildren();
    const allOption = document.createElement("option");
    allOption.value = "";
    allOption.textContent = "Alle Experimente";
    logsElements.experimentSelect.append(allOption);
    for (const exp of experiments) {
      const option = document.createElement("option");
      option.value = exp.id;
      option.textContent = `${exp.phase} · ${exp.id.slice(0, 8)}`;
      logsElements.experimentSelect.append(option);
    }
    if (experiments.some((exp) => exp.id === previousValue)) {
      logsElements.experimentSelect.value = previousValue;
    }
  } catch {
    // Leave the existing options in place; the log list itself will show the error.
  }
}

function renderLogsFromDiagnostics(entries) {
  renderCardList(logsElements.content, entries, "Keine Log-Einträge gefunden.", (li, item) => {
    addTitle(li, `${item.phase} · Experiment ${item.id.slice(0, 8)}`);
    const details = [item.created_at];
    if (item.failure_summary) details.push(item.failure_summary);
    if (item.test_command) details.push(item.test_command);
    if (item.test_exit_code !== null && item.test_exit_code !== undefined) details.push(`Exit-Code ${item.test_exit_code}`);
    if (item.changed_files.length) details.push(`Dateien: ${item.changed_files.join(", ")}`);
    addMetaLine(li, details.join(" · "));
    for (const message of item.diagnostic_messages || []) {
      addMetaLine(li, message);
    }
  });
}

function renderLogsFromActivity(entries) {
  renderCardList(logsElements.content, entries, "Keine Log-Einträge gefunden.", (li, item) => {
    addTitle(li, `${item.phase} · ${item.timestamp}`);
    const details = [];
    if (item.test_command && item.test_command.length) details.push(item.test_command.join(" "));
    if (item.test_exit_code !== null && item.test_exit_code !== undefined) details.push(`Exit-Code ${item.test_exit_code}`);
    if (item.changed_files.length) details.push(`Dateien: ${item.changed_files.join(", ")}`);
    if (item.rejection_reason) details.push(item.rejection_reason);
    if (item.policy_rule) details.push(`Regel: ${item.policy_rule}`);
    addMetaLine(li, details.join(" · "));
    for (const line of item.test_output_lines || []) {
      addMetaLine(li, line);
    }
  });
}

async function loadLogs() {
  const experimentId = logsElements.experimentSelect.value.trim();
  const mailboxTaskId = logsElements.mailboxTaskInput.value.trim();
  logsElements.content.textContent = "Wird geladen …";

  if (state.apiKey) {
    try {
      const params = new URLSearchParams({ limit: "50" });
      if (experimentId) params.set("experiment_id", experimentId);
      if (mailboxTaskId) params.set("mailbox_task_id", mailboxTaskId);
      const data = await requestJson(`/activity?${params.toString()}`, { headers: authHeaders() });
      logsElements.count.textContent = String(data.count);
      renderLogsFromActivity(data.activity);
      return;
    } catch (error) {
      logsElements.count.textContent = "!";
      showError(logsElements.content, error.message);
      return;
    }
  }

  try {
    const data = await requestJson("/diagnostics/public?limit=50");
    let experiments = data.experiments || [];
    if (experimentId) experiments = experiments.filter((exp) => exp.id === experimentId);
    if (mailboxTaskId) experiments = experiments.filter((exp) => exp.mailbox_task_id === mailboxTaskId);
    logsElements.count.textContent = String(experiments.length);
    renderLogsFromDiagnostics(experiments);
  } catch (error) {
    logsElements.count.textContent = "!";
    showError(logsElements.content, error.message);
  }
}

logsElements.refreshButton.addEventListener("click", loadLogs);
logsElements.experimentSelect.addEventListener("change", loadLogs);
logsElements.mailboxTaskInput.addEventListener("input", () => {
  clearTimeout(logsElements.mailboxTaskInput._debounce);
  logsElements.mailboxTaskInput._debounce = setTimeout(loadLogs, 400);
});

// ---------------------------------------------------------------------------
// Startup
// ---------------------------------------------------------------------------

addMessage("Hallo! Ich beantworte Fragen zu Status, Problemen und Ideen.", "agent");
loadOverview();
loadPublicActivity();
setInterval(loadPublicActivity, 10000);
loadDiagnostics();
setInterval(loadDiagnostics, 10000);

const storedApiKey = sessionStorage.getItem(API_KEY_STORAGE_KEY);
if (storedApiKey) {
  connectApiKey(storedApiKey);
}
