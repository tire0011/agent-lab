from fastapi import APIRouter
from fastapi.responses import HTMLResponse

router = APIRouter()


@router.get("/", response_class=HTMLResponse)
def index() -> str:
    return """<!doctype html>
<html lang=\"en\">
<head>
  <meta charset=\"utf-8\">
  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">
  <title>agent-lab</title>
  <style>
    body { font-family: system-ui, sans-serif; max-width: 900px; margin: 0 auto; padding: 20px; background: #f6f7f9; color: #17191c; }
    .card { background: white; border: 1px solid #ddd; border-radius: 12px; padding: 16px; margin-bottom: 16px; }
    input, textarea, button { font: inherit; box-sizing: border-box; }
    input, textarea { width: 100%; padding: 10px; margin: 6px 0 10px; border: 1px solid #bbb; border-radius: 8px; }
    textarea { min-height: 90px; resize: vertical; }
    button { padding: 9px 14px; border: 0; border-radius: 8px; cursor: pointer; }
    .row { display: flex; gap: 8px; flex-wrap: wrap; }
    .muted { color: #666; font-size: .9rem; }
    .message { white-space: pre-wrap; padding: 10px; border-radius: 8px; margin: 8px 0; }
    .user { background: #eef3ff; }
    .agent { background: #eef8ef; }
    .error { background: #fff0f0; }
    .problem { border-top: 1px solid #eee; padding: 10px 0; }
    code { word-break: break-word; }
  </style>
</head>
<body>
  <h1>agent-lab</h1>

  <section class=\"card\">
    <label>API key</label>
    <input id=\"apiKey\" type=\"password\" placeholder=\"AGENT_API_KEY\">
    <div class=\"row\">
      <button onclick=\"saveKey()\">Use key</button>
      <button onclick=\"loadStatus()\">Status</button>
      <button onclick=\"loadProblems()\">Problems</button>
    </div>
    <div id=\"status\" class=\"muted\"></div>
  </section>

  <section class=\"card\">
    <h2>Chat</h2>
    <div id=\"chat\"></div>
    <textarea id=\"message\" placeholder=\"Ask the agent...\"></textarea>
    <button id=\"sendButton\" onclick=\"sendMessage()\">Send</button>
  </section>

  <section class=\"card\">
    <h2>Open problems</h2>
    <div id=\"problems\" class=\"muted\">Not loaded.</div>
  </section>

<script>
const apiKeyInput = document.getElementById('apiKey');
apiKeyInput.value = sessionStorage.getItem('agentApiKey') || '';

function saveKey() {
  sessionStorage.setItem('agentApiKey', apiKeyInput.value.trim());
  document.getElementById('status').textContent = 'Key stored for this browser tab.';
}

function headers() {
  const key = apiKeyInput.value.trim();
  if (key) sessionStorage.setItem('agentApiKey', key);
  return { 'x-api-key': key, 'Content-Type': 'application/json' };
}

async function api(path, options = {}) {
  const response = await fetch(path, { ...options, headers: { ...headers(), ...(options.headers || {}) } });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.detail || `HTTP ${response.status}`);
  return data;
}

async function loadStatus() {
  const output = document.getElementById('status');
  try {
    const data = await api('/status');
    output.textContent = `Agent: ${data.agent}, model: ${data.model}`;
  } catch (e) {
    output.textContent = `Error: ${e.message}`;
  }
}

function addMessage(text, type) {
  const item = document.createElement('div');
  item.className = `message ${type}`;
  item.textContent = text;
  document.getElementById('chat').appendChild(item);
}

async function sendMessage() {
  const box = document.getElementById('message');
  const button = document.getElementById('sendButton');
  const message = box.value.trim();
  if (!message) return;

  addMessage(message, 'user');
  box.value = '';
  button.disabled = true;

  try {
    const data = await api('/messages', {
      method: 'POST',
      body: JSON.stringify({ message })
    });
    addMessage(data.response, 'agent');
    document.getElementById('status').textContent = `${data.model} · ${data.total_tokens} tokens · ${data.tool_calls} tool calls`;
  } catch (e) {
    addMessage(`Error: ${e.message}`, 'error');
  } finally {
    button.disabled = false;
    box.focus();
  }
}

async function loadProblems() {
  const output = document.getElementById('problems');
  try {
    const data = await api('/problems');
    output.replaceChildren();
    if (!data.problems.length) {
      output.textContent = 'No open problems.';
      return;
    }
    for (const p of data.problems) {
      const item = document.createElement('div');
      item.className = 'problem';
      const title = document.createElement('strong');
      title.textContent = `[${p.severity}] ${p.title}`;
      const details = document.createElement('div');
      details.textContent = p.details || '';
      const meta = document.createElement('div');
      meta.className = 'muted';
      meta.textContent = `${p.source} · ${p.timestamp}`;
      item.append(title, details, meta);
      output.appendChild(item);
    }
  } catch (e) {
    output.textContent = `Error: ${e.message}`;
  }
}

document.getElementById('message').addEventListener('keydown', event => {
  if (event.key === 'Enter' && !event.shiftKey) {
    event.preventDefault();
    sendMessage();
  }
});
</script>
</body>
</html>"""
