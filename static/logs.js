"use strict";

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

  // Default "Alle Experimente" view: the unified, chronological feed of
  // real structured log entries across every experiment - not the
  // lifecycle/status summaries from /activity - so a running/new
  // experiment is visible on refresh without knowing its id up front.
  if (state.apiKey) {
    try {
      const params = new URLSearchParams({ limit: "500" });
      if (level) params.set("level", level);
      if (phase) params.set("phase", phase);
      if (mailboxTaskId) params.set("mailbox_task_id", mailboxTaskId);
      const data = await requestJson(`/logs?${params.toString()}`, { headers: authHeaders() });
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
