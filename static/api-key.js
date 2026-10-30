"use strict";

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
  if (!document.querySelector("#tab-tasks").hidden) loadMailboxTasks();
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
