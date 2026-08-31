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
