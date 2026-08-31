"use strict";

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
