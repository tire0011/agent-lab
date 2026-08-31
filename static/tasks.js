"use strict";

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
