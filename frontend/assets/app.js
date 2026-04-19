// Multi-Agent Coder — vanilla JS frontend.
// Keeps things simple: fetch + WebSocket, no build step.

const $ = (id) => document.getElementById(id);

const state = {
  currentId: null,
  ws: null,
  projects: [],
  tokenBuffers: {}, // role -> string, for streamed tokens
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
      chip.innerHTML = `<span class="dot"></span><strong>${s.label || s.name}</strong> · ${s.models.length} models`;
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
      <div class="status">${p.status} · ${new Date(p.created_at).toLocaleString()}</div>`;
    li.onclick = () => openProject(p.id);
    el.appendChild(li);
  }
}

// ---------- open + subscribe ------------------------------------------------

async function openProject(id) {
  state.currentId = id;
  state.tokenBuffers = {};
  $("empty-state").classList.add("hidden");
  $("project-view").classList.remove("hidden");
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
  if (msg.kind === "token") {
    appendToken(msg);
    return;
  }
  // flush any buffered stream from this role
  flushTokens(msg.role);
  appendLog(msg);
  if (msg.kind === "state") {
    const newStatus = msg.data?.status;
    if (newStatus) setStatus(newStatus);
    loadProject(id);
    refreshProjects();
  }
  if (msg.kind === "agent" || msg.kind === "test" || msg.kind === "error") {
    // Refresh tasks/files occasionally.
    loadProject(id);
  }
}

function appendToken(msg) {
  const role = msg.role || "model";
  if (!state.tokenBuffers[role]) state.tokenBuffers[role] = "";
  state.tokenBuffers[role] += msg.message || "";
  // Render "in progress" block
  let el = document.getElementById(`tok-${role}`);
  if (!el) {
    el = document.createElement("div");
    el.className = `log-line k-token`;
    el.id = `tok-${role}`;
    el.innerHTML = `<span class="log-role r-${role}">${role}</span><span class="stream"></span>`;
    $("logs").appendChild(el);
  }
  el.querySelector(".stream").textContent = state.tokenBuffers[role].slice(-600);
  $("logs").scrollTop = $("logs").scrollHeight;
}

function flushTokens(role) {
  if (!role) {
    for (const r of Object.keys(state.tokenBuffers)) flushTokens(r);
    return;
  }
  const el = document.getElementById(`tok-${role}`);
  if (el) el.remove();
  delete state.tokenBuffers[role];
}

function appendLog(msg) {
  const logs = $("logs");
  const div = document.createElement("div");
  div.className = `log-line k-${msg.kind}`;
  const time = new Date().toLocaleTimeString();
  const role = msg.role ? `<span class="log-role r-${msg.role}">${msg.role}</span>` : "";
  div.innerHTML = `<span class="log-time">${time}</span>${role}${escape(msg.message || "")}`;
  logs.appendChild(div);
  // Cap to 2000 lines
  while (logs.children.length > 2000) logs.removeChild(logs.firstChild);
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
