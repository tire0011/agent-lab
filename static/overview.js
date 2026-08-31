"use strict";

// ---------------------------------------------------------------------------
// Übersicht: status, problems, ideas, public activity, diagnostics
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
elements.ideaForm.addEventListener("submit", submitIdea);
