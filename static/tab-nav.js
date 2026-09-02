"use strict";

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
    loadJournalctlLogs();
  }
}

for (const button of tabButtons) {
  button.addEventListener("click", () => activateTab(button.dataset.tab));
}
