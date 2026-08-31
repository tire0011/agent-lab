"use strict";

// ---------------------------------------------------------------------------
// Inbox / Outbox
// ---------------------------------------------------------------------------

const mailboxElements = {
  refreshButton: document.querySelector("#mailbox-refresh-button"),
  inboxContent: document.querySelector("#mailbox-inbox-content"),
  inboxCount: document.querySelector("#mailbox-inbox-count"),
  outboxContent: document.querySelector("#mailbox-outbox-content"),
  outboxCount: document.querySelector("#mailbox-outbox-count"),
};

function renderMailboxEntries(container, entries, emptyMessage) {
  renderCardList(container, entries, emptyMessage, (li, entry) => {
    addTitle(li, `${entry.title} · ${entry.status}`);
    const details = [entry.timestamp];
    if (entry.sender) details.push(entry.sender);
    details.push(entry.type);
    addMetaLine(li, details.join(" · "));
    if (entry.preview) addMetaLine(li, entry.preview);
  });
}

async function loadMailboxMessages() {
  mailboxElements.inboxContent.textContent = "Wird geladen …";
  mailboxElements.outboxContent.textContent = "Wird geladen …";
  try {
    const data = await requestJson("/mailbox/messages?limit=20");
    mailboxElements.inboxCount.textContent = String(data.inbox_count);
    mailboxElements.outboxCount.textContent = String(data.outbox_count);
    renderMailboxEntries(mailboxElements.inboxContent, data.inbox, "Keine Inbox-Nachrichten vorhanden.");
    renderMailboxEntries(mailboxElements.outboxContent, data.outbox, "Keine Outbox-Nachrichten vorhanden.");
  } catch (error) {
    mailboxElements.inboxCount.textContent = "!";
    mailboxElements.outboxCount.textContent = "!";
    showError(mailboxElements.inboxContent, error.message);
    showError(mailboxElements.outboxContent, error.message);
  }
}

mailboxElements.refreshButton.addEventListener("click", loadMailboxMessages);
