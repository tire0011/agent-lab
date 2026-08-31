"use strict";

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
