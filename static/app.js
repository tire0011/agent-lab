"use strict";

// ---------------------------------------------------------------------------
// Shared state & helpers
// ---------------------------------------------------------------------------

const API_KEY_STORAGE_KEY = "agentLabApiKey";
const CHAT_SESSION_STORAGE_KEY = "nexusChatSessionId";

const state = {
  apiKey: null,
  chatSessionId: sessionStorage.getItem(CHAT_SESSION_STORAGE_KEY),
  operationsCatalog: [],
  operationsHistory: [],
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
 * HTML injection APIs, so nothing from the API can inject markup.
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
  if (name === "operations") loadOperationsCatalog();
  if (name === "logs") {
    loadLogsExperimentOptions();
    loadLogs();
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
  if (!document.querySelector("#tab-operations").hidden) loadOperationsCatalog();
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
  if (!state.apiKey) {
    elements.chatState.textContent = "Bitte zuerst oben den API-Key eingeben.";
    return;
  }

  addMessage(message, "user");
  elements.input.value = "";
  elements.input.disabled = true;
  elements.send.disabled = true;
  elements.send.textContent = "Sendet …";
  elements.chatState.textContent = "Nexus wurde gestartet …";

  try {
    let response = await requestJson("/chat", {
      method: "POST",
      headers: authHeaders(),
      body: JSON.stringify({ message, session_id: state.chatSessionId }),
    });
    const jobId = response.job_id;
    if (!jobId) throw new Error("Chat-Job wurde ohne ID gestartet");

    while (response.status === "queued" || response.status === "running") {
      elements.chatState.textContent = response.status === "queued" ? "Nexus wartet …" : "Nexus arbeitet …";
      await new Promise((resolve) => setTimeout(resolve, 2000));
      response = await requestJson(`/chat/jobs/${encodeURIComponent(jobId)}`, {
        headers: authHeaders(),
      });
    }

    if (response.status === "failed") {
      throw new Error(response.error || "Nexus-Chat ist fehlgeschlagen");
    }
    if (response.session_id) {
      state.chatSessionId = response.session_id;
      sessionStorage.setItem(CHAT_SESSION_STORAGE_KEY, response.session_id);
    }
    addMessage(response.reply || "Nexus hat keine Antwort geliefert.", "agent");
    const details = [response.action, response.status].filter(Boolean);
    if (response.experiment_id) details.push(`Experiment ${response.experiment_id}`);
    elements.chatState.textContent = details.join(" · ");
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

const taskState = {
  currentExperimentId: null,
  adoptionTimer: null,
  adoptionUi: null,
};

function stopAdoptionPolling() {
  if (!taskState.adoptionTimer) return;
  clearInterval(taskState.adoptionTimer);
  taskState.adoptionTimer = null;
}

function startAdoptionPolling(experimentId) {
  stopAdoptionPolling();
  taskState.adoptionTimer = setInterval(async () => {
    if (taskState.currentExperimentId !== experimentId || !state.apiKey) {
      stopAdoptionPolling();
      return;
    }
    try {
      const adoption = await requestJson(`/experiments/${encodeURIComponent(experimentId)}/adoption`, { headers: authHeaders() });
      updateAdoptionUi(adoption);
      if (["adopted", "needs_review", "failed", "interrupted"].includes(adoption.status)) {
        stopAdoptionPolling();
      }
    } catch {
      stopAdoptionPolling();
    }
  }, 5000);
}

function adoptionSummary(adoption) {
  if (!adoption) return "Noch nicht gestartet.";
  const parts = [`Status: ${adoption.status || "not_started"}`];
  if (adoption.integrated_commit_sha) parts.push(`Integrations-Commit: ${adoption.integrated_commit_sha.slice(0, 8)}`);
  if (adoption.experiment_commit_sha) parts.push(`Experiment-Commit: ${adoption.experiment_commit_sha.slice(0, 8)}`);
  if (adoption.test_exit_code !== null && adoption.test_exit_code !== undefined) parts.push(`Tests: Exit-Code ${adoption.test_exit_code}`);
  return parts.join(" · ");
}

function updateAdoptionUi(adoption) {
  const ui = taskState.adoptionUi;
  if (!ui) return;
  const status = adoption && adoption.status ? adoption.status : "not_started";
  ui.status.textContent = adoptionSummary(adoption);
  ui.reason.textContent = adoption && adoption.reason ? adoption.reason : "";
  ui.files.textContent = adoption && adoption.actual_files && adoption.actual_files.length
    ? `Dateien: ${adoption.actual_files.join(", ")}`
    : ui.defaultFiles;
  ui.localSync.textContent = adoption && adoption.local_sync_pending
    ? "Lokale Synchronisierung ausstehend."
    : "";
  ui.button.disabled = ["queued", "adopting"].includes(status);
  if (status === "adopted") ui.button.textContent = "Übernommen";
  else if (["queued", "adopting"].includes(status)) ui.button.textContent = "Übernahme läuft …";
  else ui.button.textContent = "Experiment übernehmen";
}

async function triggerAdoption(experiment) {
  const files = (experiment.quality_evaluation && experiment.quality_evaluation.changed_files) || [];
  const targetBranch = (experiment.adoption && experiment.adoption.target_branch) || "main";
  const confirmed = window.confirm(
    `Experiment ${experiment.id}
Zielbranch: ${targetBranch}
Dateien: ${files.join(", ") || "–"}

Jetzt übernehmen?`
  );
  if (!confirmed) return;
  taskState.adoptionUi.button.disabled = true;
  taskState.adoptionUi.button.textContent = "Übernahme läuft …";
  try {
    const adoption = await requestJson(`/experiments/${encodeURIComponent(experiment.id)}/adopt`, {
      method: "POST",
      headers: authHeaders(),
    });
    updateAdoptionUi(adoption);
    startAdoptionPolling(experiment.id);
  } catch (error) {
    taskState.adoptionUi.button.disabled = false;
    taskState.adoptionUi.button.textContent = "Experiment übernehmen";
    window.alert(error.message);
  }
}

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

async function copyTextToClipboard(value) {
  if (!value) return false;
  if (navigator.clipboard && typeof navigator.clipboard.writeText === "function") {
    try {
      await navigator.clipboard.writeText(value);
      return true;
    } catch {
      // Fallback below.
    }
  }
  const textArea = document.createElement("textarea");
  textArea.value = value;
  textArea.setAttribute("readonly", "");
  textArea.style.position = "fixed";
  textArea.style.opacity = "0";
  textArea.style.left = "-9999px";
  document.body.append(textArea);
  textArea.select();
  textArea.setSelectionRange(0, textArea.value.length);
  try {
    // Legacy fallback when Clipboard API is unavailable.
    return document.execCommand("copy");
  } catch {
    return false;
  } finally {
    textArea.remove();
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
  stopAdoptionPolling();
  taskState.currentExperimentId = experimentId;
  taskElements.detailSection.hidden = false;
  taskElements.detailContent.textContent = "Wird geladen …";
  const exp = await fetchExperimentSummary(experimentId);
  if (!exp) {
    showError(taskElements.detailContent, "Experiment konnte nicht geladen werden.");
    return;
  }
  taskElements.detailContent.replaceChildren();
  const fullExperimentId = exp.id || "";
  const lines = [
    ["Experiment-ID", fullExperimentId || "–"],
    ["Phase", exp.phase || exp.status],
    ["Erstellt", exp.created_at],
    ["Beendet", exp.ended_at || "–"],
    ["Test-Ergebnis", exp.test_summary || (exp.test_exit_code !== undefined ? `Exit-Code ${exp.test_exit_code}` : "–")],
    ["Geänderte Dateien", (exp.changed_files || []).join(", ") || "–"],
  ];
  if (exp.policy_rule) lines.push(["Regel", exp.policy_rule]);
  if (exp.failure_reason || exp.rejection_reason) lines.push(["Hinweis", exp.failure_reason || exp.rejection_reason]);
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
    if (label === "Experiment-ID") {
      const valueSpan = document.createElement("span");
      valueSpan.textContent = value;
      valueSpan.className = "selectable-id";
      dd.append(valueSpan);
      if (fullExperimentId) {
        const copyButton = document.createElement("button");
        copyButton.type = "button";
        copyButton.className = "secondary inline-copy-button";
        copyButton.textContent = "Kopieren";
        const status = document.createElement("span");
        status.className = "inline-copy-status";
        copyButton.addEventListener("click", async () => {
          const copied = await copyTextToClipboard(fullExperimentId);
          status.textContent = copied ? "Kopiert." : "Kopieren fehlgeschlagen.";
          setTimeout(() => {
            status.textContent = "";
          }, 1600);
        });
        dd.append(copyButton, status);
      }
    } else {
      dd.textContent = value;
    }
    dl.append(dt, dd);
  }
  taskElements.detailContent.append(dl);

  if (exp.test_failure_tail && exp.test_failure_tail.length) {
    const section = document.createElement("section");
    section.className = "test-failure-section";
    const heading = document.createElement("h3");
    heading.textContent = "Testfehler";
    const pre = document.createElement("pre");
    pre.className = "test-failure-output";
    pre.textContent = exp.test_failure_tail.join("\n");
    section.append(heading, pre);
    taskElements.detailContent.append(section);
  }

  const canAdopt = Boolean(
    state.apiKey && exp.status === "succeeded" && exp.quality_evaluation && exp.quality_evaluation.outcome === "pass"
  );
  if (canAdopt) {
    const box = document.createElement("section");
    box.className = "adoption-box";
    const title = document.createElement("h3");
    title.textContent = "Übernahme";
    const status = document.createElement("p");
    const files = document.createElement("p");
    const reason = document.createElement("p");
    reason.className = "muted";
    const localSync = document.createElement("p");
    localSync.className = "muted";
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = "Experiment übernehmen";
    button.addEventListener("click", () => triggerAdoption(exp));
    box.append(title, status, files, reason, localSync, button);
    taskElements.detailContent.append(box);
    taskState.adoptionUi = {
      status,
      files,
      reason,
      localSync,
      button,
      defaultFiles: `Dateien: ${((exp.quality_evaluation && exp.quality_evaluation.changed_files) || exp.changed_files || []).join(", ") || "–"}`,
    };
    updateAdoptionUi(exp.adoption || { status: "not_started", actual_files: (exp.changed_files || []), target_branch: "main" });
    if (exp.adoption && ["queued", "adopting"].includes(exp.adoption.status)) startAdoptionPolling(exp.id);
  } else {
    taskState.adoptionUi = null;
  }
}

taskElements.loadButton.addEventListener("click", loadTaskApprovals);
taskElements.detailClose.addEventListener("click", () => {
  stopAdoptionPolling();
  taskState.currentExperimentId = null;
  taskState.adoptionUi = null;
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
// Operations
// ---------------------------------------------------------------------------

const operationsElements = {
  refreshButton: document.querySelector("#operations-refresh-button"),
  form: document.querySelector("#operations-form"),
  select: document.querySelector("#operations-operation-select"),
  experimentId: document.querySelector("#operations-experiment-id"),
  limit: document.querySelector("#operations-limit"),
  help: document.querySelector("#operations-help"),
  output: document.querySelector("#operations-output"),
  status: document.querySelector("#operations-status"),
  history: document.querySelector("#operations-history"),
};

function renderOperationsHistory() {
  renderCardList(operationsElements.history, state.operationsHistory, "Noch keine Operationen.", (li, item) => {
    addTitle(li, item.operation);
    addMetaLine(li, `${item.success ? "OK" : "Fehler"} · ${item.duration_ms} ms`);
    if (item.summary) addMetaLine(li, item.summary);
  });
}

function updateOperationsHelp() {
  const selected = state.operationsCatalog.find((entry) => entry.operation === operationsElements.select.value);
  if (!selected) {
    operationsElements.help.textContent = "Keine Operation ausgewählt.";
    return;
  }
  const args = (selected.arguments || []).map((arg) => `${arg.name}${arg.required ? "*" : ""}: ${arg.description}`).join(" · ");
  operationsElements.help.textContent = `${selected.description}${args ? ` · ${args}` : ""}`;
}

async function loadOperationsCatalog() {
  if (!state.apiKey) {
    operationsElements.help.textContent = "Bitte oben den API-Key eingeben, um Operations zu verwenden.";
    operationsElements.select.replaceChildren();
    return;
  }
  operationsElements.help.textContent = "Katalog wird geladen …";
  try {
    const data = await requestJson("/operations/catalog", { headers: authHeaders() });
    state.operationsCatalog = data.operations || [];
    const previous = operationsElements.select.value;
    operationsElements.select.replaceChildren();
    for (const entry of state.operationsCatalog) {
      const option = document.createElement("option");
      option.value = entry.operation;
      option.textContent = entry.operation;
      operationsElements.select.append(option);
    }
    if (state.operationsCatalog.some((entry) => entry.operation === previous)) {
      operationsElements.select.value = previous;
    }
    updateOperationsHelp();
  } catch (error) {
    operationsElements.select.replaceChildren();
    operationsElements.help.textContent = error.message;
  }
}

operationsElements.select.addEventListener("change", updateOperationsHelp);
operationsElements.refreshButton.addEventListener("click", loadOperationsCatalog);
operationsElements.form.addEventListener("submit", async (event) => {
  event.preventDefault();
  const operation = operationsElements.select.value;
  if (!operation) return;
  const args = {};
  const experimentId = operationsElements.experimentId.value.trim();
  const limit = operationsElements.limit.value.trim();
  if (experimentId) args.experiment_id = experimentId;
  if (limit) args.limit = Number(limit);
  operationsElements.status.textContent = "Läuft …";
  operationsElements.output.textContent = "Wird ausgeführt …";
  try {
    const result = await requestJson("/operations/execute", {
      method: "POST",
      headers: authHeaders(),
      body: JSON.stringify({ operation, args }),
    });
    operationsElements.status.textContent = result.success ? "Fertig" : "Fehler";
    operationsElements.output.textContent = (result.output_lines || []).join("\n") || (result.error_summary || "Keine Ausgabe.");
    state.operationsHistory.unshift({
      operation: result.operation,
      success: result.success,
      duration_ms: result.duration_ms,
      summary: result.error_summary || ((result.output_lines || [])[0] || ""),
    });
    state.operationsHistory = state.operationsHistory.slice(0, 10);
    renderOperationsHistory();
  } catch (error) {
    operationsElements.status.textContent = "Fehler";
    operationsElements.output.textContent = error.message;
  }
});

// ---------------------------------------------------------------------------
// Chain Editor
// ---------------------------------------------------------------------------

const NEXUS_GESAMTSTATUS_TEMPLATE = [
  { id: "git", action: "git_status" },
  { id: "log", action: "git_log" },
  { id: "worktrees", action: "git_worktrees" },
  { id: "service", action: "service_status" },
  { id: "disk", action: "disk" },
  { id: "memory", action: "memory" },
  { id: "problems", action: "problems_list" },
  { id: "inbox", action: "inbox_status" },
  { id: "outbox", action: "outbox_status" },
];

const MAX_CHAIN_STEPS = 10;

const chainEditorState = {
  steps: [],           // [{id, action, arguments: {}}]
  pollingTimer: null,
  currentRequestId: null,
  // Per-step expand state: Map<stepId, boolean>
  expandedSteps: new Map(),
};

const chainElements = {
  templateButton: document.querySelector("#chain-template-button"),
  stopOnError: document.querySelector("#chain-stop-on-error"),
  stepsList: document.querySelector("#chain-steps-list"),
  addStepButton: document.querySelector("#chain-add-step-button"),
  stepCount: document.querySelector("#chain-step-count"),
  runButton: document.querySelector("#chain-run-button"),
  runStatus: document.querySelector("#chain-run-status"),
  resultCard: document.querySelector("#chain-result-card"),
  resultSteps: document.querySelector("#chain-result-steps"),
  resultOverallStatus: document.querySelector("#chain-result-overall-status"),
  resultMeta: document.querySelector("#chain-result-meta"),
  resultCopyButton: document.querySelector("#chain-result-copy-button"),
};

function _chainGenerateId() {
  return "step-" + Math.random().toString(36).slice(2, 9);
}

function _getArgSpecsForAction(action) {
  const entry = state.operationsCatalog.find((e) => e.operation === action);
  return entry ? (entry.arguments || []) : [];
}

function _renderChainStepArgInputs(container, step) {
  container.replaceChildren();
  const specs = _getArgSpecsForAction(step.action);
  for (const spec of specs) {
    const label = document.createElement("label");
    label.textContent = `${spec.name}${spec.required ? " *" : ""}: ${spec.description || ""}`;
    const input = document.createElement("input");
    input.type = spec.type === "int" ? "number" : "text";
    input.placeholder = spec.name;
    input.dataset.argName = spec.name;
    if (step.arguments && step.arguments[spec.name] !== undefined) {
      input.value = step.arguments[spec.name];
    }
    input.addEventListener("input", () => {
      const val = input.value.trim();
      if (val === "") {
        delete step.arguments[spec.name];
      } else {
        step.arguments[spec.name] = spec.type === "int" ? Number(val) : val;
      }
    });
    container.append(label, input);
  }
}

function renderChainSteps() {
  chainElements.stepsList.replaceChildren();
  chainElements.stepCount.textContent = `${chainEditorState.steps.length} / ${MAX_CHAIN_STEPS} Schritte`;
  chainElements.addStepButton.disabled = chainEditorState.steps.length >= MAX_CHAIN_STEPS;

  chainEditorState.steps.forEach((step, index) => {
    const item = document.createElement("div");
    item.className = "chain-step-item";

    // Header row
    const header = document.createElement("div");
    header.className = "chain-step-header";

    // Move buttons
    const moveDiv = document.createElement("div");
    moveDiv.className = "chain-step-move";
    const upBtn = document.createElement("button");
    upBtn.type = "button";
    upBtn.textContent = "↑";
    upBtn.title = "Nach oben";
    upBtn.disabled = index === 0;
    upBtn.addEventListener("click", () => {
      if (index > 0) {
        [chainEditorState.steps[index - 1], chainEditorState.steps[index]] =
          [chainEditorState.steps[index], chainEditorState.steps[index - 1]];
        renderChainSteps();
      }
    });
    const downBtn = document.createElement("button");
    downBtn.type = "button";
    downBtn.textContent = "↓";
    downBtn.title = "Nach unten";
    downBtn.disabled = index === chainEditorState.steps.length - 1;
    downBtn.addEventListener("click", () => {
      if (index < chainEditorState.steps.length - 1) {
        [chainEditorState.steps[index], chainEditorState.steps[index + 1]] =
          [chainEditorState.steps[index + 1], chainEditorState.steps[index]];
        renderChainSteps();
      }
    });
    moveDiv.append(upBtn, downBtn);

    // Step ID input
    const idLabel = document.createElement("label");
    idLabel.textContent = "ID";
    const idInput = document.createElement("input");
    idInput.type = "text";
    idInput.value = step.id;
    idInput.placeholder = "step-id";
    idInput.addEventListener("change", () => {
      const val = idInput.value.trim().replace(/[^a-zA-Z0-9_-]/g, "-");
      idInput.value = val;
      step.id = val || _chainGenerateId();
    });

    // Action select
    const actionLabel = document.createElement("label");
    actionLabel.textContent = "Aktion";
    const actionSelect = document.createElement("select");
    const blankOpt = document.createElement("option");
    blankOpt.value = "";
    blankOpt.textContent = "– wählen –";
    actionSelect.append(blankOpt);
    for (const entry of state.operationsCatalog) {
      const opt = document.createElement("option");
      opt.value = entry.operation;
      opt.textContent = entry.operation;
      actionSelect.append(opt);
    }
    actionSelect.value = step.action;

    const argsDiv = document.createElement("div");
    argsDiv.className = "chain-step-args";

    actionSelect.addEventListener("change", () => {
      step.action = actionSelect.value;
      step.arguments = {};
      _renderChainStepArgInputs(argsDiv, step);
    });

    _renderChainStepArgInputs(argsDiv, step);

    // Remove button
    const removeBtn = document.createElement("button");
    removeBtn.type = "button";
    removeBtn.className = "chain-step-remove";
    removeBtn.textContent = "✕";
    removeBtn.title = "Schritt entfernen";
    removeBtn.addEventListener("click", () => {
      chainEditorState.steps.splice(index, 1);
      renderChainSteps();
    });

    header.append(moveDiv, idLabel, idInput, actionLabel, actionSelect, removeBtn);
    item.append(header, argsDiv);
    chainElements.stepsList.append(item);
  });
}

function chainAddStep(action, stepId) {
  if (chainEditorState.steps.length >= MAX_CHAIN_STEPS) return;
  chainEditorState.steps.push({
    id: stepId || _chainGenerateId(),
    action: action || "",
    arguments: {},
  });
  renderChainSteps();
}

function chainLoadTemplate(template) {
  chainEditorState.steps = template.map((t) => ({ ...t, arguments: {} }));
  renderChainSteps();
}

chainElements.templateButton.addEventListener("click", () => {
  chainLoadTemplate(NEXUS_GESAMTSTATUS_TEMPLATE);
});

chainElements.addStepButton.addEventListener("click", () => chainAddStep());

function renderChainResultStep(stepResult, expandedSteps) {
  const wrapper = document.createElement("div");
  wrapper.className = "chain-result-step";
  const isExpanded = expandedSteps.get(stepResult.id) !== false; // default open
  wrapper.setAttribute("aria-expanded", isExpanded ? "true" : "false");

  const headerEl = document.createElement("div");
  headerEl.className = "chain-result-step-header";
  headerEl.setAttribute("role", "button");
  headerEl.tabIndex = 0;

  const title = document.createElement("span");
  title.className = "chain-result-step-title";
  title.textContent = `${stepResult.id} · ${stepResult.action}`;

  const statusSpan = document.createElement("span");
  statusSpan.className = `chain-result-step-meta step-status-${stepResult.status}`;
  statusSpan.textContent = `${stepResult.status} · ${stepResult.duration_ms} ms`;
  if (stepResult.truncated) {
    statusSpan.textContent += " · [gekürzt]";
  }

  headerEl.append(title, statusSpan);

  const body = document.createElement("div");
  body.className = "chain-result-step-body";

  if (stepResult.error) {
    const errLine = document.createElement("div");
    errLine.textContent = `Fehler: ${stepResult.error}`;
    body.append(errLine);
  }
  if (stepResult.output && stepResult.output.length > 0) {
    body.append(document.createTextNode(stepResult.output.join("\n")));
  } else if (!stepResult.error) {
    body.append(document.createTextNode("(Keine Ausgabe)"));
  }

  const toggleExpand = () => {
    const expanded = wrapper.getAttribute("aria-expanded") === "true";
    wrapper.setAttribute("aria-expanded", expanded ? "false" : "true");
    expandedSteps.set(stepResult.id, !expanded);
  };
  headerEl.addEventListener("click", toggleExpand);
  headerEl.addEventListener("keydown", (e) => {
    if (e.key === "Enter" || e.key === " ") { e.preventDefault(); toggleExpand(); }
  });

  wrapper.append(headerEl, body);
  return wrapper;
}

function renderChainResult(result) {
  chainElements.resultCard.hidden = false;

  const status = result.overall_status || "–";
  chainElements.resultOverallStatus.textContent = status;
  chainElements.resultOverallStatus.className = "indicator " + (
    status === "completed" ? "good" :
    status === "completed_with_errors" ? "bad" :
    status === "failed" ? "bad" :
    status === "rejected" ? "bad" :
    "pending"
  );

  const duration = result.duration_ms != null ? `${result.duration_ms} ms` : "";
  const counts = `${result.steps_successful} OK · ${result.steps_failed} Fehler`;
  chainElements.resultMeta.textContent = [result.request_id, counts, duration].filter(Boolean).join(" · ");

  // Re-render step results without losing expanded state
  const steps = result.steps || [];
  const existingStepEls = Array.from(chainElements.resultSteps.children);

  // If structure unchanged (same step IDs in same order), update in-place; otherwise re-build
  const same = steps.length === existingStepEls.length &&
    steps.every((s, i) => existingStepEls[i] &&
      existingStepEls[i].querySelector(".chain-result-step-title") &&
      existingStepEls[i].querySelector(".chain-result-step-title").textContent === `${s.id} · ${s.action}`);

  if (!same) {
    chainElements.resultSteps.replaceChildren();
    for (const s of steps) {
      chainElements.resultSteps.append(renderChainResultStep(s, chainEditorState.expandedSteps));
    }
  } else {
    // Update status/meta text without touching expanded state
    steps.forEach((s, i) => {
      const el = existingStepEls[i];
      const metaSpan = el.querySelector(".chain-result-step-meta");
      if (metaSpan) {
        metaSpan.className = `chain-result-step-meta step-status-${s.status}`;
        metaSpan.textContent = `${s.status} · ${s.duration_ms} ms${s.truncated ? " · [gekürzt]" : ""}`;
      }
      const bodyEl = el.querySelector(".chain-result-step-body");
      if (bodyEl) {
        bodyEl.replaceChildren();
        if (s.error) {
          const errLine = document.createElement("div");
          errLine.textContent = `Fehler: ${s.error}`;
          bodyEl.append(errLine);
        }
        if (s.output && s.output.length > 0) {
          bodyEl.append(document.createTextNode(s.output.join("\n")));
        } else if (!s.error) {
          bodyEl.append(document.createTextNode("(Keine Ausgabe)"));
        }
      }
    });
  }
}

let _chainLastResultJson = "";

async function pollChainStatus(requestId) {
  if (!state.apiKey) return;
  try {
    const result = await requestJson(`/operations/chains/${requestId}`, { headers: authHeaders() });
    renderChainResult(result);
    _chainLastResultJson = JSON.stringify(result, null, 2);

    const terminal = ["completed", "completed_with_errors", "failed", "rejected", "interrupted"];
    if (!terminal.includes(result.overall_status)) {
      chainEditorState.pollingTimer = setTimeout(() => pollChainStatus(requestId), 2000);
    } else {
      chainElements.runStatus.textContent = `Fertig: ${result.overall_status}`;
      chainEditorState.pollingTimer = null;
    }
  } catch (error) {
    chainElements.runStatus.textContent = `Poll-Fehler: ${error.message}`;
  }
}

chainElements.runButton.addEventListener("click", async () => {
  if (!state.apiKey) {
    chainElements.runStatus.textContent = "Bitte zuerst API-Key eingeben.";
    return;
  }
  if (chainEditorState.steps.length === 0) {
    chainElements.runStatus.textContent = "Keine Schritte definiert.";
    return;
  }
  if (chainEditorState.pollingTimer) {
    clearTimeout(chainEditorState.pollingTimer);
    chainEditorState.pollingTimer = null;
  }

  const body = {
    steps: chainEditorState.steps.map((s) => ({
      id: s.id,
      action: s.action,
      arguments: s.arguments || {},
    })),
    stop_on_error: chainElements.stopOnError.checked,
  };

  chainElements.runStatus.textContent = "Wird gestartet …";
  try {
    const result = await requestJson("/operations/chains", {
      method: "POST",
      headers: authHeaders(),
      body: JSON.stringify(body),
    });
    chainEditorState.currentRequestId = result.request_id;
    chainEditorState.expandedSteps = new Map();
    chainElements.runStatus.textContent = `Gestartet: ${result.request_id}`;
    renderChainResult(result);
    pollChainStatus(result.request_id);
  } catch (error) {
    chainElements.runStatus.textContent = `Fehler: ${error.message}`;
  }
});

chainElements.resultCopyButton.addEventListener("click", () => {
  if (!_chainLastResultJson) return;
  navigator.clipboard.writeText(_chainLastResultJson).then(() => {
    const orig = chainElements.resultCopyButton.textContent;
    chainElements.resultCopyButton.textContent = "Kopiert!";
    setTimeout(() => { chainElements.resultCopyButton.textContent = orig; }, 1500);
  }).catch(() => {
    chainElements.resultCopyButton.textContent = "Fehler";
  });
});

// ---------------------------------------------------------------------------
// Logs
// ---------------------------------------------------------------------------

const logsElements = {
  refreshButton: document.querySelector("#logs-refresh-button"),
  experimentSelect: document.querySelector("#logs-experiment-select"),
  levelSelect: document.querySelector("#logs-level-select"),
  phaseSelect: document.querySelector("#logs-phase-select"),
  mailboxTaskInput: document.querySelector("#logs-mailbox-task-input"),
  content: document.querySelector("#logs-content"),
  count: document.querySelector("#logs-count"),
  visibleCount: document.querySelector("#logs-visible-count"),
  lastSuccessfulRefresh: document.querySelector("#logs-last-successful-refresh"),
  autoRefreshState: document.querySelector("#logs-auto-refresh-state"),
};

function updateLogsErrorStatus() {
  logsElements.visibleCount.textContent = "0";
}

function updateLogsSuccessStatus(renderedEntries) {
  const visibleCount = renderedEntries.length;
  logsElements.count.textContent = String(visibleCount);
  logsElements.visibleCount.textContent = String(visibleCount);
  const refreshedAt = new Date();
  logsElements.lastSuccessfulRefresh.dateTime = refreshedAt.toISOString();
  logsElements.lastSuccessfulRefresh.textContent = new Intl.DateTimeFormat("de-DE", {
    dateStyle: "medium",
    timeStyle: "medium",
  }).format(refreshedAt);
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

// One structured JSONL entry from GET /experiments/{id}/logs, rendered as
// a collapsible card. Every value is written via textContent only.
function renderExperimentLogEntries(entries) {
  logsElements.content.replaceChildren();
  if (!entries.length) {
    logsElements.content.textContent = "Keine Log-Einträge gefunden.";
    return;
  }
  const list = document.createElement("ul");
  list.className = "item-list";

  for (const entry of entries) {
    const li = document.createElement("li");
    li.className = "log-entry";

    const hasDetails = Boolean(
      entry.command || entry.cwd || entry.duration_ms !== null && entry.duration_ms !== undefined ||
      entry.exit_code !== null && entry.exit_code !== undefined || entry.stdout || entry.stderr ||
      entry.exception_type || entry.exception_message || entry.stacktrace
    );

    const head = document.createElement(hasDetails ? "button" : "div");
    head.className = "log-entry-head";
    if (hasDetails) head.type = "button";

    const level = document.createElement("span");
    level.className = `log-level log-level-${(entry.level || "info").toLowerCase()}`;
    level.textContent = entry.level || "INFO";
    head.append(level);

    const time = document.createElement("span");
    time.className = "log-entry-time";
    time.textContent = entry.timestamp || "";
    head.append(time);

    const phase = document.createElement("span");
    phase.className = "log-entry-phase";
    phase.textContent = entry.phase ? `${entry.phase} · ${entry.event || ""}` : entry.event || "";
    head.append(phase);

    const message = document.createElement("span");
    message.className = "log-entry-message";
    message.textContent = entry.message || "";
    head.append(message);

    let details = null;
    let detailWrap = null;
    if (hasDetails) {
      const toggle = document.createElement("span");
      toggle.className = "log-entry-toggle";
      toggle.textContent = "Details ▾";
      head.append(toggle);

      details = document.createElement("dl");
      detailWrap = document.createElement("div");
      detailWrap.className = "log-entry-details";
      detailWrap.hidden = true;
      detailWrap.append(details);

      const addDetail = (label, value) => {
        if (value === null || value === undefined || value === "") return;
        const dt = document.createElement("dt");
        dt.textContent = label;
        const dd = document.createElement("dd");
        dd.textContent = String(value);
        details.append(dt, dd);
      };
      addDetail("Command", entry.command);
      addDetail("Working Dir", entry.cwd);
      addDetail("Laufzeit (ms)", entry.duration_ms);
      addDetail("Exit-Code", entry.exit_code);
      addDetail("stdout", entry.stdout);
      addDetail("stderr", entry.stderr);
      addDetail("Exception", entry.exception_type);
      addDetail("Exception-Meldung", entry.exception_message);
      addDetail("Stacktrace", entry.stacktrace);

      head.addEventListener("click", () => {
        detailWrap.hidden = !detailWrap.hidden;
        toggle.textContent = detailWrap.hidden ? "Details ▾" : "Details ▴";
      });
    }

    li.append(head);
    if (detailWrap) li.append(detailWrap);
    if (entry.truncated) {
      const notice = document.createElement("div");
      notice.className = "log-entry-truncated";
      notice.textContent = "Eintrag wurde aus Sicherheits-/Größengründen gekürzt.";
      li.append(notice);
    }
    list.append(li);
  }
  logsElements.content.append(list);
}

async function loadLogs() {
  const experimentId = logsElements.experimentSelect.value.trim();
  const level = logsElements.levelSelect ? logsElements.levelSelect.value.trim() : "";
  const phase = logsElements.phaseSelect ? logsElements.phaseSelect.value.trim() : "";
  const mailboxTaskId = logsElements.mailboxTaskInput.value.trim();
  logsElements.content.textContent = "Wird geladen …";

  // A specific experiment is selected: use the dedicated, complete
  // per-experiment JSONL log view (chronological, all internal phases).
  if (state.apiKey && experimentId) {
    try {
      const params = new URLSearchParams({ limit: "500" });
      if (level) params.set("level", level);
      if (phase) params.set("phase", phase);
      const data = await requestJson(`/experiments/${encodeURIComponent(experimentId)}/logs?${params.toString()}`, {
        headers: authHeaders(),
      });
      const entries = data.entries || [];
      renderExperimentLogEntries(entries);
      updateLogsSuccessStatus(entries);
      return;
    } catch (error) {
      logsElements.count.textContent = "!";
      updateLogsErrorStatus();
      showError(logsElements.content, error.message);
      return;
    }
  }

  if (state.apiKey) {
    try {
      const params = new URLSearchParams({ limit: "50" });
      if (mailboxTaskId) params.set("mailbox_task_id", mailboxTaskId);
      const data = await requestJson(`/activity?${params.toString()}`, { headers: authHeaders() });
      const activity = data.activity || [];
      renderLogsFromActivity(activity);
      updateLogsSuccessStatus(activity);
      return;
    } catch (error) {
      logsElements.count.textContent = "!";
      updateLogsErrorStatus();
      showError(logsElements.content, error.message);
      return;
    }
  }

  try {
    const data = await requestJson("/diagnostics/public?limit=50");
    let experiments = data.experiments || [];
    if (experimentId) experiments = experiments.filter((exp) => exp.id === experimentId);
    if (mailboxTaskId) experiments = experiments.filter((exp) => exp.mailbox_task_id === mailboxTaskId);
    renderLogsFromDiagnostics(experiments);
    updateLogsSuccessStatus(experiments);
  } catch (error) {
    logsElements.count.textContent = "!";
    updateLogsErrorStatus();
    showError(logsElements.content, error.message);
  }
}

logsElements.refreshButton.addEventListener("click", loadLogs);
logsElements.experimentSelect.addEventListener("change", loadLogs);
if (logsElements.levelSelect) logsElements.levelSelect.addEventListener("change", loadLogs);
if (logsElements.phaseSelect) logsElements.phaseSelect.addEventListener("change", loadLogs);
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
