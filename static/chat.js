"use strict";

// ---------------------------------------------------------------------------
// Chat
// ---------------------------------------------------------------------------

function addMessage(text, type) {
  const message = document.createElement("div");
  message.className = `message ${type}`;
  message.textContent = text;
  elements.messages.append(message);
  elements.messages.scrollTop = elements.messages.scrollHeight;
}

async function sendMessage(event) {
  event.preventDefault();
  const message = elements.input.value.trim();
  if (!message || elements.send.disabled) return;
  if (!state.apiKey) {
    elements.chatState.textContent = "Bitte zuerst oben den API-Key eingeben.";
    return;
  }

  addMessage(message, "user");
  elements.input.value = "";
  elements.input.disabled = true;
  elements.send.disabled = true;
  elements.send.textContent = "Sendet …";
  elements.chatState.textContent = "Nexus wurde gestartet …";

  try {
    let response = await requestJson("/chat", {
      method: "POST",
      headers: authHeaders(),
      body: JSON.stringify({ message, session_id: state.chatSessionId }),
    });
    const jobId = response.job_id;
    if (!jobId) throw new Error("Chat-Job wurde ohne ID gestartet");

    while (response.status === "queued" || response.status === "running") {
      elements.chatState.textContent = response.status === "queued" ? "Nexus wartet …" : "Nexus arbeitet …";
      await new Promise((resolve) => setTimeout(resolve, 2000));
      response = await requestJson(`/chat/jobs/${encodeURIComponent(jobId)}`, {
        headers: authHeaders(),
      });
    }

    if (response.status === "failed") {
      throw new Error(response.error || "Nexus-Chat ist fehlgeschlagen");
    }
    if (response.session_id) {
      state.chatSessionId = response.session_id;
      sessionStorage.setItem(CHAT_SESSION_STORAGE_KEY, response.session_id);
    }
    addMessage(response.reply || "Nexus hat keine Antwort geliefert.", "agent");
    const details = [response.action, response.status].filter(Boolean);
    if (response.experiment_id) details.push(`Experiment ${response.experiment_id}`);
    elements.chatState.textContent = details.join(" · ");
  } catch (error) {
    addMessage(error.message, "error");
    elements.chatState.textContent = "Senden fehlgeschlagen";
  } finally {
    elements.input.disabled = false;
    elements.send.disabled = false;
    elements.send.textContent = "Senden";
    elements.input.focus();
  }
}

elements.form.addEventListener("submit", sendMessage);
elements.input.addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey) {
    event.preventDefault();
    elements.form.requestSubmit();
  }
});
