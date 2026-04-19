// Multi-Agent Coder — vanilla JS frontend.
// Keeps things simple: fetch + WebSocket, no build step.

const $ = (id) => document.getElementById(id);

const state = {
  currentId: null,
  ws: null,
  projects: [],
  tokenBuffers: {}, // role -> string, for streamed tokens
  seenEvents: new Set(), // dedupe replay on WebSocket reconnect
  agentRows: {}, // role -> DOM node (in-place "current activity" line)
  lastLogKey: null, // (kind|role|message) of last appended event, to collapse repeats
  lastLogCountEl: null,
  lastLogCount: 1,
};

const ROLE_LABEL = {
  planner: "Planner",
  architect: "Architect",
  coder: "Coder",
  reviewer: "Reviewer",
  tester: "Tester",
  analyst: "Analyst",
};

// ---------- server status ---------------------------------------------------

async function refreshServers() {
  try {
    const r = await fetch("/api/servers");
    const data = await r.json();
    const el = $("servers");
    el.innerHTML = "";
    for (const s of data) {
      const chip = document.createElement("div");
      chip.className = "server-chip" + (s.online ? " online" : "");
      chip.title = s.error || s.models.join("\n");
      chip.innerHTML = `<span class="dot"></span><strong>${escape(s.label || s.name)}</strong> · ${s.models.length} models`;
      el.appendChild(chip);
    }
  } catch (e) {
    console.error("server probe failed", e);
  }
}

// ---------- project list ----------------------------------------------------

async function refreshProjects() {
  const r = await fetch("/api/projects");
  state.projects = await r.json();
  const el = $("projects");
  el.innerHTML = "";
  for (const p of state.projects) {
    const li = document.createElement("li");
    li.className = "project-item" + (p.id === state.currentId ? " active" : "");
    li.innerHTML = `<div class="name">${escape(p.name)}</div>
      <div class="status">${escape(p.status)} · ${new Date(p.created_at).toLocaleString()}</div>`;
    li.onclick = () => openProject(p.id);
    el.appendChild(li);
  }
}

// ---------- open + subscribe ------------------------------------------------

async function openProject(id) {
  state.currentId = id;
  state.tokenBuffers = {};
  state.seenEvents = new Set();
  state.agentRows = {};
  state.lastLogKey = null;
  state.lastLogCountEl = null;
  state.lastLogCount = 1;
  $("empty-state").classList.add("hidden");
  $("project-view").classList.remove("hidden");
  $("agents-live").innerHTML = "";
  $("logs").innerHTML = "";
  $("file-preview").textContent = "";
  await Promise.all([loadProject(id), subscribe(id)]);
  refreshProjects();
}

async function loadProject(id) {
  const r = await fetch(`/api/projects/${id}`);
  if (!r.ok) return;
  const p = await r.json();
  $("proj-title").textContent = p.name;
  $("proj-meta").textContent = `${p.id} · iteration ${p.iteration} · ${p.files.length} fichiers`;
  setStatus(p.status);
  $("btn-zip").href = `/api/projects/${id}/zip`;
  $("btn-zip").classList.toggle("hidden", !p.zip_path);

  const tasks = $("tasks");
  tasks.innerHTML = "";
  for (const t of p.tasks) {
    const li = document.createElement("li");
    li.className = `task t-${t.status}`;
    const notes = t.review_notes
      ? `<div class="notes">${escape(t.review_notes)}</div>`
      : "";
    li.innerHTML = `<div class="title">${escape(t.title)}</div>
      <div class="meta">${t.status} · ${t.attempts} attempt(s) · ${t.file_paths.length} files</div>
      ${notes}`;
    tasks.appendChild(li);
  }

  const files = $("files");
  files.innerHTML = "";
  for (const f of p.files) {
    const li = document.createElement("li");
    li.textContent = `${f.path}  r${f.revision}`;
    li.onclick = async () => {
      const r2 = await fetch(`/api/projects/${id}/files/${encodeURI(f.path)}`);
      if (r2.ok) {
        const d = await r2.json();
        $("file-preview").textContent = d.content;
        for (const x of files.children) x.classList.remove("active");
        li.classList.add("active");
      }
    };
    files.appendChild(li);
  }
}

function setStatus(status) {
  const el = $("proj-status");
  el.className = `status-pill s-${status}`;
  el.textContent = status;
}

// ---------- websocket -------------------------------------------------------

function subscribe(id) {
  if (state.ws) {
    try { state.ws.close(); } catch (_) {}
  }
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${proto}://${location.host}/ws/projects/${id}`);
  state.ws = ws;
  ws.onmessage = (ev) => {
    try {
      const msg = JSON.parse(ev.data);
      handleEvent(id, msg);
    } catch (_) {}
  };
  ws.onclose = () => {
    if (state.currentId === id) {
      setTimeout(() => subscribe(id), 2000);
    }
  };
  return Promise.resolve();
}

function handleEvent(id, msg) {
  if (msg.kind === "ping") return;
  // Dedupe replayed events across reconnects using the DB event id.
  if (msg.id) {
    if (state.seenEvents.has(msg.id)) return;
    state.seenEvents.add(msg.id);
  }

  if (msg.kind === "token") {
    appendToken(msg);
    return;
  }

  if (msg.kind === "state") {
    const newStatus = msg.data?.status;
    if (newStatus) setStatus(newStatus);
    loadProject(id);
    refreshProjects();
    appendLog(msg); // state transitions DO go in the compact event log
    return;
  }

  // Flush any running token stream for this role (the agent moved on).
  flushTokens(msg.role);

  // "agent" events describe what the agent is doing right now → in-place row.
  if (msg.kind === "agent") {
    updateAgentRow(msg);
    return;
  }

  // error / warning / test / info → compact event log.
  appendLog(msg);
  if (msg.kind === "test" || msg.kind === "error") {
    loadProject(id);
  }
}

// ---------- agents-live (one row per agent, replaced in place) -------------

function updateAgentRow(msg) {
  const role = msg.role || "model";
  let row = state.agentRows[role];
  if (!row) {
    row = document.createElement("div");
    row.className = `agent-row r-${role}`;
    row.innerHTML = `
      <span class="agent-role r-${role}">${escape(ROLE_LABEL[role] || role)}</span>
      <span class="agent-msg"></span>
      <span class="agent-spinner"></span>`;
    state.agentRows[role] = row;
    $("agents-live").appendChild(row);
  }
  row.querySelector(".agent-msg").textContent = msg.message || "";
  row.classList.add("active");
  // Clear "active" after 3s of no updates so the spinner stops spinning when idle.
  clearTimeout(row._idleTimer);
  row._idleTimer = setTimeout(() => row.classList.remove("active"), 3000);
}

// ---------- streaming tokens (inline inside the agent row) -----------------

function appendToken(msg) {
  const role = msg.role || "model";
  if (!state.tokenBuffers[role]) state.tokenBuffers[role] = "";
  state.tokenBuffers[role] += msg.message || "";

  let row = state.agentRows[role];
  if (!row) {
    // Synthesize an empty agent row so the stream has somewhere to live.
    updateAgentRow({ role, message: "…" });
    row = state.agentRows[role];
  }
  const tail = state.tokenBuffers[role].slice(-240).replace(/\s+/g, " ");
  row.querySelector(".agent-msg").textContent = tail;
  row.classList.add("active");
  clearTimeout(row._idleTimer);
  row._idleTimer = setTimeout(() => row.classList.remove("active"), 3000);
}

function flushTokens(role) {
  if (!role) {
    for (const r of Object.keys(state.tokenBuffers)) flushTokens(r);
    return;
  }
  delete state.tokenBuffers[role];
  // Leave the agent row in place — it already shows the last message.
}

// ---------- compact event log (state / error / warning / test) ------------

function appendLog(msg) {
  const logs = $("logs");
  const key = `${msg.kind}|${msg.role || ""}|${msg.message || ""}`;
  if (key === state.lastLogKey && state.lastLogCountEl) {
    state.lastLogCount += 1;
    state.lastLogCountEl.textContent = ` ×${state.lastLogCount}`;
    return;
  }
  const div = document.createElement("div");
  div.className = `log-line k-${msg.kind}`;
  const time = new Date().toLocaleTimeString();
  const role = msg.role ? `<span class="log-role r-${msg.role}">${escape(msg.role)}</span>` : "";

  const data = msg.data || {};
  const hasDetails =
    (data.stderr && data.stderr.length) ||
    (data.stdout && data.stdout.length) ||
    (data.command && data.command.length) ||
    (Array.isArray(data.files) && data.files.length) ||
    (Array.isArray(data.issues) && data.issues.length);

  const marker = hasDetails ? '<span class="log-toggle">▸</span>' : "";
  div.innerHTML =
    `<span class="log-time">${time}</span>${role}${marker}` +
    `<span class="log-msg">${escape(msg.message || "")}</span>` +
    `<span class="log-count"></span>`;

  if (hasDetails) {
    const details = document.createElement("pre");
    details.className = "log-details hidden";
    const parts = [];
    if (data.command) parts.push(`$ ${data.command}`);
    if (data.exit_code !== undefined) parts.push(`exit code: ${data.exit_code}`);
    if (Array.isArray(data.files) && data.files.length) {
      parts.push(`files patched:\n  - ${data.files.join("\n  - ")}`);
    }
    if (Array.isArray(data.issues) && data.issues.length) {
      parts.push(`issues:\n  - ${data.issues.join("\n  - ")}`);
    }
    if (data.stdout) parts.push(`--- stdout ---\n${data.stdout}`);
    if (data.stderr) parts.push(`--- stderr ---\n${data.stderr}`);
    details.textContent = parts.join("\n\n");
    div.appendChild(details);
    div.querySelector(".log-toggle").style.cursor = "pointer";
    div.querySelector(".log-msg").style.cursor = "pointer";
    const toggle = () => {
      details.classList.toggle("hidden");
      div.querySelector(".log-toggle").textContent = details.classList.contains("hidden") ? "▸" : "▾";
    };
    div.querySelector(".log-toggle").addEventListener("click", toggle);
    div.querySelector(".log-msg").addEventListener("click", toggle);
  }

  logs.appendChild(div);
  state.lastLogKey = key;
  state.lastLogCount = 1;
  state.lastLogCountEl = div.querySelector(".log-count");
  // Cap to 200 lines so the log never takes "un mètre de page".
  while (logs.children.length > 200) logs.removeChild(logs.firstChild);
  logs.scrollTop = logs.scrollHeight;
}

// ---------- create project form --------------------------------------------

$("new-project").addEventListener("submit", async (e) => {
  e.preventDefault();
  const name = $("proj-name").value.trim();
  const prompt = $("proj-prompt").value.trim();
  if (!name || !prompt) return;
  const r = await fetch("/api/projects", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name, prompt }),
  });
  if (r.ok) {
    const p = await r.json();
    $("proj-name").value = "";
    $("proj-prompt").value = "";
    await refreshProjects();
    openProject(p.id);
  } else {
    alert("Erreur création projet: " + r.status);
  }
});

$("btn-pause").addEventListener("click", async () => {
  if (!state.currentId) return;
  await fetch(`/api/projects/${state.currentId}/control`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ action: "pause" }),
  });
});
$("btn-resume").addEventListener("click", async () => {
  if (!state.currentId) return;
  await fetch(`/api/projects/${state.currentId}/control`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ action: "resume" }),
  });
});

function escape(s) {
  return String(s).replace(/[&<>]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" })[c]);
}

// ---------- boot -----------------------------------------------------------

refreshServers();
refreshProjects();
setInterval(refreshServers, 15000);
setInterval(refreshProjects, 10000);
