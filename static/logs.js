"use strict";

// ---------------------------------------------------------------------------
// Logs
// ---------------------------------------------------------------------------

const logsElements = {
  refreshButton: document.querySelector("#logs-refresh-button"),
  loadOlderButton: document.querySelector("#logs-load-older-button"),
  experimentSelect: document.querySelector("#logs-experiment-select"),
  levelSelect: document.querySelector("#logs-level-select"),
  phaseSelect: document.querySelector("#logs-phase-select"),
  mailboxTaskInput: document.querySelector("#logs-mailbox-task-input"),
  jobInput: document.querySelector("#logs-job-input"),
  sessionInput: document.querySelector("#logs-session-input"),
  content: document.querySelector("#logs-content"),
  count: document.querySelector("#logs-count"),
  visibleCount: document.querySelector("#logs-visible-count"),
  lastSuccessfulRefresh: document.querySelector("#logs-last-successful-refresh"),
  autoRefreshState: document.querySelector("#logs-auto-refresh-state"),
};

// Every systemd unit the Operations tab already knows how to manage
// (see modules.service_operations._DEFAULT_NEXUS_SERVICES); "Alle
// Services" tails each of these and merges the results, so the Logs
// page can show every journalctl log without the operator picking one
// service at a time.
const JOURNALCTL_SERVICES = ["agent-api", "nexus-mailbox", "llama-server"];

const journalctlElements = {
  refreshButton: document.querySelector("#logs-journalctl-refresh-button"),
  serviceSelect: document.querySelector("#logs-journalctl-service-select"),
  linesInput: document.querySelector("#logs-journalctl-lines-input"),
  content: document.querySelector("#logs-journalctl-content"),
};

// Accumulated state for the currently displayed page(s): which URL to
// re-request for "Ältere laden" (same base path/filters as the last
// manual refresh), the entries collected so far (oldest first, exactly
// as returned/merged from the API - no client-side re-sorting), and the
// cursor for the next older page, if any. Reset on every manual refresh;
// never touched by any automatic/background process.
const logsPaginationState = {
  baseUrl: null,
  entries: [],
  nextCursor: null,
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
      entry.exception_type || entry.exception_message || entry.stacktrace ||
      entry.job_id || entry.session_id
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
      addDetail("Chat-Job-ID", entry.job_id);
      addDetail("Session-ID", entry.session_id);

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

function hideLoadOlderButton() {
  logsPaginationState.baseUrl = null;
  logsPaginationState.entries = [];
  logsPaginationState.nextCursor = null;
  if (logsElements.loadOlderButton) {
    logsElements.loadOlderButton.hidden = true;
    logsElements.loadOlderButton.disabled = false;
    logsElements.loadOlderButton.textContent = "Ältere laden";
  }
}

function updateLoadOlderButton() {
  if (!logsElements.loadOlderButton) return;
  logsElements.loadOlderButton.hidden = !logsPaginationState.nextCursor;
}

// Manual-only pagination: fetches exactly one older page using the same
// endpoint/filters as the last manual refresh, plus the cursor it
// returned, then prepends the (chronologically older) results to what is
// already shown. Never triggered automatically - only by this button.
async function loadOlderLogs() {
  if (!logsPaginationState.baseUrl || !logsPaginationState.nextCursor) return;
  const separator = logsPaginationState.baseUrl.includes("?") ? "&" : "?";
  const url = `${logsPaginationState.baseUrl}${separator}cursor=${encodeURIComponent(logsPaginationState.nextCursor)}`;
  logsElements.loadOlderButton.disabled = true;
  logsElements.loadOlderButton.textContent = "Lädt …";
  try {
    const data = await requestJson(url, { headers: authHeaders() });
    const olderEntries = data.entries || [];
    logsPaginationState.entries = [...olderEntries, ...logsPaginationState.entries];
    logsPaginationState.nextCursor = data.next_cursor || null;
    renderExperimentLogEntries(logsPaginationState.entries);
    updateLogsSuccessStatus(logsPaginationState.entries);
  } catch (error) {
    showError(logsElements.content, error.message);
  } finally {
    logsElements.loadOlderButton.disabled = false;
    logsElements.loadOlderButton.textContent = "Ältere laden";
    updateLoadOlderButton();
  }
}

async function loadLogs() {
  const experimentId = logsElements.experimentSelect.value.trim();
  const level = logsElements.levelSelect ? logsElements.levelSelect.value.trim() : "";
  const phase = logsElements.phaseSelect ? logsElements.phaseSelect.value.trim() : "";
  const mailboxTaskId = logsElements.mailboxTaskInput.value.trim();
  const jobId = logsElements.jobInput ? logsElements.jobInput.value.trim() : "";
  const sessionId = logsElements.sessionInput ? logsElements.sessionInput.value.trim() : "";
  logsElements.content.textContent = "Wird geladen …";
  hideLoadOlderButton();

  // A specific experiment is selected: use the dedicated, complete
  // per-experiment JSONL log view (chronological, all internal phases).
  if (state.apiKey && experimentId) {
    const params = new URLSearchParams({ limit: "500" });
    if (level) params.set("level", level);
    if (phase) params.set("phase", phase);
    const baseUrl = `/experiments/${encodeURIComponent(experimentId)}/logs?${params.toString()}`;
    try {
      const data = await requestJson(baseUrl, { headers: authHeaders() });
      const entries = data.entries || [];
      logsPaginationState.baseUrl = baseUrl;
      logsPaginationState.entries = entries;
      logsPaginationState.nextCursor = data.next_cursor || null;
      renderExperimentLogEntries(entries);
      updateLogsSuccessStatus(entries);
      updateLoadOlderButton();
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
    const params = new URLSearchParams({ limit: "500" });
    if (level) params.set("level", level);
    if (phase) params.set("phase", phase);
    if (mailboxTaskId) params.set("mailbox_task_id", mailboxTaskId);
    if (jobId) params.set("job_id", jobId);
    if (sessionId) params.set("session_id", sessionId);
    const baseUrl = `/logs?${params.toString()}`;
    try {
      const data = await requestJson(baseUrl, { headers: authHeaders() });
      const entries = data.entries || [];
      logsPaginationState.baseUrl = baseUrl;
      logsPaginationState.entries = entries;
      logsPaginationState.nextCursor = data.next_cursor || null;
      renderExperimentLogEntries(entries);
      updateLogsSuccessStatus(entries);
      updateLoadOlderButton();
      return;
    } catch (error) {
      logsElements.count.textContent = "!";
      updateLogsErrorStatus();
      showError(logsElements.content, error.message);
      return;
    }
  }

  // No API key: unauthenticated fallback. Uses the same central,
  // multi-source feed as the authenticated view (experiments + mailbox
  // tasks + system), just re-shaped through the public allowlist
  // (GET /logs/public) - no longer limited to per-experiment diagnostic
  // summaries.
  const params = new URLSearchParams({ limit: "100" });
  if (level) params.set("level", level);
  if (phase) params.set("phase", phase);
  if (experimentId) params.set("experiment_id", experimentId);
  if (mailboxTaskId) params.set("mailbox_task_id", mailboxTaskId);
  const baseUrl = `/logs/public?${params.toString()}`;
  try {
    const data = await requestJson(baseUrl);
    const entries = data.entries || [];
    logsPaginationState.baseUrl = baseUrl;
    logsPaginationState.entries = entries;
    logsPaginationState.nextCursor = data.next_cursor || null;
    renderExperimentLogEntries(entries);
    updateLogsSuccessStatus(entries);
    updateLoadOlderButton();
  } catch (error) {
    logsElements.count.textContent = "!";
    updateLogsErrorStatus();
    showError(logsElements.content, error.message);
  }
}

logsElements.refreshButton.addEventListener("click", loadLogs);
logsElements.refreshButton.addEventListener("click", loadJournalctlLogs);
if (logsElements.loadOlderButton) logsElements.loadOlderButton.addEventListener("click", loadOlderLogs);
logsElements.experimentSelect.addEventListener("change", loadLogs);
if (logsElements.levelSelect) logsElements.levelSelect.addEventListener("change", loadLogs);
if (logsElements.phaseSelect) logsElements.phaseSelect.addEventListener("change", loadLogs);
logsElements.mailboxTaskInput.addEventListener("input", () => {
  clearTimeout(logsElements.mailboxTaskInput._debounce);
  logsElements.mailboxTaskInput._debounce = setTimeout(loadLogs, 400);
});
for (const input of [logsElements.jobInput, logsElements.sessionInput]) {
  if (!input) continue;
  input.addEventListener("input", () => {
    clearTimeout(input._debounce);
    input._debounce = setTimeout(loadLogs, 400);
  });
}

// ---------------------------------------------------------------------------
// System-Logs (journalctl) - separate data source (systemd, via
// GET /operations/logs/tail), not the JSONL feed above, so it is loaded
// and rendered independently.
// ---------------------------------------------------------------------------

// A raw "--output=short-iso" journalctl line starts with an ISO-8601
// timestamp token followed by the rest of the line; splitting on the
// first space keeps that timestamp sortable as plain text so entries
// from multiple services can be merged chronologically.
function parseJournalctlLine(service, rawLine) {
  const separatorIndex = rawLine.indexOf(" ");
  const timestamp = separatorIndex === -1 ? "" : rawLine.slice(0, separatorIndex);
  return { service, timestamp, text: rawLine };
}

function renderJournalctlEntries(entries) {
  journalctlElements.content.replaceChildren();
  if (!entries.length) {
    journalctlElements.content.textContent = "Keine Log-Einträge gefunden.";
    return;
  }
  const list = document.createElement("ul");
  list.className = "item-list";
  for (const entry of entries) {
    const li = document.createElement("li");
    li.className = "log-entry";
    const head = document.createElement("div");
    head.className = "log-entry-head";

    const service = document.createElement("span");
    service.className = "log-entry-phase";
    service.textContent = entry.service;
    head.append(service);

    const message = document.createElement("span");
    message.className = "log-entry-message";
    message.textContent = entry.text;
    head.append(message);

    li.append(head);
    list.append(li);
  }
  journalctlElements.content.append(list);
}

async function loadJournalctlLogs() {
  if (!journalctlElements.content) return;
  if (!state.apiKey) {
    journalctlElements.content.textContent = "Erfordert einen API-Schlüssel.";
    return;
  }
  const selectedService = journalctlElements.serviceSelect ? journalctlElements.serviceSelect.value.trim() : "";
  const services = selectedService ? [selectedService] : JOURNALCTL_SERVICES;
  const lines = journalctlElements.linesInput && journalctlElements.linesInput.value.trim()
    ? journalctlElements.linesInput.value.trim()
    : "100";
  journalctlElements.content.textContent = "Wird geladen …";
  try {
    const perService = await Promise.all(
      services.map((service) =>
        requestJson(
          `/operations/logs/tail?service=${encodeURIComponent(service)}&lines=${encodeURIComponent(lines)}`,
          { headers: authHeaders() },
        ).then(
          (data) => (data.data && data.data.lines ? data.data.lines : []).map((line) => parseJournalctlLine(service, line)),
          // One service being unreachable (e.g. unknown unit) must not
          // blank out the others when "Alle Services" is selected.
          () => [],
        )
      ),
    );
    const merged = perService.flat().sort((a, b) => a.timestamp.localeCompare(b.timestamp));
    renderJournalctlEntries(merged);
  } catch (error) {
    showError(journalctlElements.content, error.message);
  }
}

if (journalctlElements.refreshButton) journalctlElements.refreshButton.addEventListener("click", loadJournalctlLogs);
if (journalctlElements.serviceSelect) journalctlElements.serviceSelect.addEventListener("change", loadJournalctlLogs);
