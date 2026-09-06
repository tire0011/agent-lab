"use strict";

// ---------------------------------------------------------------------------
// Logs - one complete, chronological Nexus log timeline (GET /logs), merging
// structured JSONL entries (mailbox, controller, experiment, chat, system)
// with the agent-api/nexus-mailbox/llama-server systemd journals. There is
// no experiment selector: the default view already shows everything, with
// experiment/mailbox-task/job/session ids kept only as optional filters.
// ---------------------------------------------------------------------------

const logsElements = {
  refreshButton: document.querySelector("#logs-refresh-button"),
  loadOlderButton: document.querySelector("#logs-load-older-button"),
  searchInput: document.querySelector("#logs-search-input"),
  levelSelect: document.querySelector("#logs-level-select"),
  sourceSelect: document.querySelector("#logs-source-select"),
  phaseSelect: document.querySelector("#logs-phase-select"),
  experimentInput: document.querySelector("#logs-experiment-input"),
  mailboxTaskInput: document.querySelector("#logs-mailbox-task-input"),
  jobInput: document.querySelector("#logs-job-input"),
  sessionInput: document.querySelector("#logs-session-input"),
  content: document.querySelector("#logs-content"),
  count: document.querySelector("#logs-count"),
  visibleCount: document.querySelector("#logs-visible-count"),
  lastSuccessfulRefresh: document.querySelector("#logs-last-successful-refresh"),
  autoRefreshState: document.querySelector("#logs-auto-refresh-state"),
  sourceErrors: document.querySelector("#logs-source-errors"),
  traceInput: document.querySelector("#logs-trace-input"),
  traceButton: document.querySelector("#logs-trace-button"),
  traceSummary: document.querySelector("#logs-trace-summary"),
  truncationHint: document.querySelector("#logs-truncation-hint"),
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

function renderLogSourceErrors(sourceErrors) {
  if (!logsElements.sourceErrors) return;
  const entries = sourceErrors ? Object.entries(sourceErrors) : [];
  if (!entries.length) {
    logsElements.sourceErrors.hidden = true;
    logsElements.sourceErrors.replaceChildren();
    return;
  }
  logsElements.sourceErrors.hidden = false;
  logsElements.sourceErrors.replaceChildren();
  const summary = document.createElement("p");
  summary.textContent = "Einzelne Quellen konnten nicht gelesen werden:";
  logsElements.sourceErrors.append(summary);
  const list = document.createElement("ul");
  for (const [source, reason] of entries) {
    const li = document.createElement("li");
    li.textContent = `${source}: ${reason}`;
    list.append(li);
  }
  logsElements.sourceErrors.append(list);
}

// One merged log entry from GET /logs (structured JSONL or normalized
// systemd journal line), rendered as a collapsible card in one common,
// compact shape: time, level, source, message, with everything else as
// expandable detail. Every value is written via textContent only.
function renderLogEntries(entries) {
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
      entry.experiment_id || entry.mailbox_task_id || entry.job_id || entry.session_id
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

    const source = document.createElement("span");
    source.className = "log-entry-phase";
    source.textContent = entry.source || "experiment";
    head.append(source);

    const message = document.createElement("span");
    message.className = "log-entry-message";
    message.textContent = entry.message || entry.event || "";
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
      addDetail("Phase", entry.phase);
      addDetail("Ereignis", entry.event);
      addDetail("Experiment-ID", entry.experiment_id);
      addDetail("Mailbox-Task-ID", entry.mailbox_task_id);
      addDetail("Chat-Job-ID", entry.job_id);
      addDetail("Session-ID", entry.session_id);
      addDetail("Command", entry.command);
      addDetail("Working Dir", entry.cwd);
      addDetail("Laufzeit (ms)", entry.duration_ms);
      addDetail("Exit-Code", entry.exit_code);
      addDetail("stdout", entry.stdout);
      addDetail("stderr", entry.stderr);
      addDetail("Exception", entry.exception_type);
      addDetail("Exception-Meldung", entry.exception_message);
      addDetail("Stacktrace", entry.stacktrace);

      // Jump straight from any entry to the full order it belongs to,
      // so an operator never has to copy an id into a filter by hand.
      const traceRef = entry.experiment_id || entry.mailbox_task_id
        || entry.session_id || entry.job_id;
      if (traceRef) {
        const traceButton = document.createElement("button");
        traceButton.type = "button";
        traceButton.className = "secondary";
        traceButton.textContent = "Auftrag verfolgen";
        traceButton.addEventListener("click", (event) => {
          event.stopPropagation();
          loadTrace(traceRef);
        });
        detailWrap.append(traceButton);
      }

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
    renderLogEntries(logsPaginationState.entries);
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
  const level = logsElements.levelSelect ? logsElements.levelSelect.value.trim() : "";
  const source = logsElements.sourceSelect ? logsElements.sourceSelect.value.trim() : "";
  const phase = logsElements.phaseSelect ? logsElements.phaseSelect.value.trim() : "";
  const search = logsElements.searchInput ? logsElements.searchInput.value.trim() : "";
  const experimentId = logsElements.experimentInput ? logsElements.experimentInput.value.trim() : "";
  const mailboxTaskId = logsElements.mailboxTaskInput.value.trim();
  const jobId = logsElements.jobInput ? logsElements.jobInput.value.trim() : "";
  const sessionId = logsElements.sessionInput ? logsElements.sessionInput.value.trim() : "";
  logsElements.content.textContent = "Wird geladen …";
  hideLoadOlderButton();
  renderLogSourceErrors(null);
  renderLogsTruncationHint(false);
  if (logsElements.traceSummary) logsElements.traceSummary.hidden = true;

  // One merged, chronological timeline - structured JSONL entries
  // (mailbox/controller/experiment/chat/system) plus the
  // agent-api/nexus-mailbox/llama-server journals. Reading logs needs no
  // API key (DECISIONS.md D-012), so there is a single code path here
  // rather than a reduced unauthenticated fallback. `search` is handled
  // server-side across the whole retained history, not by filtering the
  // page that happens to be loaded.
  const params = new URLSearchParams({ limit: "500" });
  if (level) params.set("level", level);
  if (source) params.set("source", source);
  if (phase) params.set("phase", phase);
  if (search) params.set("search", search);
  if (experimentId) params.set("experiment_id", experimentId);
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
    renderLogEntries(entries);
    updateLogsSuccessStatus(entries);
    updateLoadOlderButton();
    renderLogSourceErrors(data.source_errors);
    renderLogsTruncationHint(data.truncated);
  } catch (error) {
    logsElements.count.textContent = "!";
    updateLogsErrorStatus();
    showError(logsElements.content, error.message);
  }
}

// "Auftrag verfolgen": resolve any reference (inbox filename, experiment
// id, mailbox task id, session/job id, or a remembered fragment) to the
// complete chain of what Nexus did with it, and render the summary above
// the timeline.
async function loadTrace(reference) {
  const value = (reference || "").trim();
  if (!value) return;
  if (logsElements.traceInput) logsElements.traceInput.value = value;
  logsElements.content.textContent = "Wird geladen …";
  hideLoadOlderButton();
  renderLogSourceErrors(null);
  renderLogsTruncationHint(false);
  try {
    const data = await requestJson(`/logs/trace/${encodeURI(value)}`, { headers: authHeaders() });
    const entries = data.entries || [];
    renderLogEntries(entries);
    renderTraceSummary(data);
    updateLogsSuccessStatus(entries);
    renderLogsTruncationHint(data.truncated);
  } catch (error) {
    logsElements.count.textContent = "!";
    updateLogsErrorStatus();
    showError(logsElements.content, error.message);
  }
}

function renderTraceSummary(data) {
  if (!logsElements.traceSummary) return;
  logsElements.traceSummary.replaceChildren();
  if (!data || !data.found) {
    logsElements.traceSummary.hidden = false;
    logsElements.traceSummary.textContent =
      `Kein Auftrag zu „${data ? data.reference : ""}" gefunden.`;
    return;
  }
  const summary = data.summary || {};
  const ids = data.correlation_ids || {};
  const parts = [
    `${summary.entry_count || 0} Einträge`,
    `von ${summary.started_at || "?"} bis ${summary.ended_at || "?"}`,
    `${summary.error_count || 0} Fehler`,
    `zuletzt: ${summary.last_event || "?"}`,
  ];
  const idParts = [];
  for (const [field, values] of Object.entries(ids)) {
    if (values && values.length) idParts.push(`${field}=${values.join(", ")}`);
  }

  const line = document.createElement("p");
  line.textContent = parts.join(" · ");
  logsElements.traceSummary.append(line);
  if (idParts.length) {
    const idLine = document.createElement("p");
    idLine.className = "muted";
    idLine.textContent = idParts.join(" | ");
    logsElements.traceSummary.append(idLine);
  }
  if ((summary.phases || []).length) {
    const phaseLine = document.createElement("p");
    phaseLine.className = "muted";
    phaseLine.textContent = `Phasen: ${summary.phases.join(" → ")}`;
    logsElements.traceSummary.append(phaseLine);
  }
  logsElements.traceSummary.hidden = false;
}

function renderLogsTruncationHint(truncated) {
  if (!logsElements.truncationHint) return;
  logsElements.truncationHint.hidden = !truncated;
  logsElements.truncationHint.textContent = truncated
    ? "Die Suche hat ihr Lesebudget erreicht - es können ältere Treffer existieren, die nicht gescannt wurden."
    : "";
}

logsElements.refreshButton.addEventListener("click", loadLogs);
if (logsElements.traceButton) {
  logsElements.traceButton.addEventListener("click", () => {
    loadTrace(logsElements.traceInput ? logsElements.traceInput.value : "");
  });
}
if (logsElements.traceInput) {
  logsElements.traceInput.addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
      event.preventDefault();
      loadTrace(logsElements.traceInput.value);
    }
  });
}
if (logsElements.loadOlderButton) logsElements.loadOlderButton.addEventListener("click", loadOlderLogs);
if (logsElements.levelSelect) logsElements.levelSelect.addEventListener("change", loadLogs);
if (logsElements.sourceSelect) logsElements.sourceSelect.addEventListener("change", loadLogs);
if (logsElements.phaseSelect) logsElements.phaseSelect.addEventListener("change", loadLogs);
logsElements.mailboxTaskInput.addEventListener("input", () => {
  clearTimeout(logsElements.mailboxTaskInput._debounce);
  logsElements.mailboxTaskInput._debounce = setTimeout(loadLogs, 400);
});
for (const input of [logsElements.searchInput, logsElements.experimentInput, logsElements.jobInput, logsElements.sessionInput]) {
  if (!input) continue;
  input.addEventListener("input", () => {
    clearTimeout(input._debounce);
    input._debounce = setTimeout(loadLogs, 400);
  });
}
