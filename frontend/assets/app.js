/* Multi-Agent Coder — UI client.
   Sober, functional, no animations. */

const $ = (id) => document.getElementById(id);

const ROLE_LABEL = {
  planner: "Planner",
  architect: "Architect",
  coder: "Coder",
  fixer: "Fixer",
  reviewer: "Reviewer",
  tester: "Tester",
  analyst: "Analyst",
};

const state = {
  projects: [],
  currentId: null,
  ws: null,
  seenEvents: new Set(),
  lastLogKey: null,
  lastLogCount: 1,
  lastLogCountEl: null,
  eventCount: 0,
  stream: new Map(), // path -> { el, preEl, statusEl, content, role }
  streamOrder: [],
  currentProject: null, // last-loaded snapshot for progress counters
  selectedFile: null,
};

const FILE_CAP = 8000; // max characters kept in the live pre per file

// ============================================================================
// Servers status chips
// ============================================================================

async function refreshServers() {
  try {
    const r = await fetch("/api/servers");
    const servers = await r.json();
    const el = $("servers");
    el.innerHTML = "";
    for (const s of servers) {
      const chip = document.createElement("div");
      chip.className = "server-chip" + (s.online ? " online" : "");
      chip.title = s.error || s.models.join("\n");
      chip.innerHTML =
        `<span class="dot"></span><strong>${escape(s.label || s.name)}</strong><span>${s.models.length}</span>`;
      el.appendChild(chip);
    }
  } catch (_) {
    /* offline: leave previous chips */
  }
}

// ============================================================================
// Projects list
// ============================================================================

async function refreshProjects() {
  try {
    const r = await fetch("/api/projects");
    state.projects = await r.json();
  } catch (_) {
    return;
  }
  const el = $("projects");
  el.innerHTML = "";
  for (const p of state.projects) {
    const li = document.createElement("li");
    li.className = "project-item" + (p.id === state.currentId ? " active" : "");
    li.innerHTML = `<div class="name">${escape(p.name)}</div>
      <div class="status">${escape(p.status)} · ${new Date(p.created_at).toLocaleDateString()}</div>
      <button class="del" title="Supprimer">×</button>`;
    li.addEventListener("click", (e) => {
      if (e.target.classList.contains("del")) return;
      openProject(p.id);
    });
    li.querySelector(".del").addEventListener("click", async (e) => {
      e.stopPropagation();
      if (!confirm(`Supprimer « ${p.name} » ?`)) return;
      await deleteProject(p.id);
    });
    el.appendChild(li);
  }
  const c = $("projects-count");
  if (c) c.textContent = state.projects.length;
}

async function deleteProject(id) {
  const r = await fetch(`/api/projects/${id}`, { method: "DELETE" });
  if (!r.ok) { alert("Suppression échouée"); return; }
  if (state.currentId === id) {
    state.currentId = null;
    if (state.ws) { try { state.ws.close(); } catch {} state.ws = null; }
    $("project-view").classList.add("hidden");
    $("empty-state").classList.remove("hidden");
  }
  await refreshProjects();
}

// ============================================================================
// Open project
// ============================================================================

async function openProject(id) {
  state.currentId = id;
  state.seenEvents = new Set();
  state.lastLogKey = null;
  state.lastLogCountEl = null;
  state.lastLogCount = 1;
  state.eventCount = 0;
  state.currentProject = null;
  state.selectedFile = null;
  $("empty-state").classList.add("hidden");
  $("project-view").classList.remove("hidden");
  $("logs").innerHTML = "";
  $("file-preview").textContent = "Sélectionne un fichier pour voir son contenu.";
  $("current-action").textContent = "Initialisation…";
  $("events-count").textContent = "0";
  $("proj-progress").innerHTML = "";
  _resetStream();
  if (window.innerWidth <= 860) $("sidebar").classList.remove("open");
  await Promise.all([loadProject(id), loadNotes(id), subscribe(id)]);
  refreshProjects();
}

async function loadProject(id) {
  const r = await fetch(`/api/projects/${id}`);
  if (!r.ok) return;
  const p = await r.json();
  state.currentProject = p;
  $("proj-title").textContent = p.name;
  setStatus(p.status);
  $("btn-zip").href = `/api/projects/${id}/zip`;
  $("btn-zip").classList.toggle("hidden", !p.zip_path);
  const banner = $("zip-banner");
  if (banner) {
    banner.href = `/api/projects/${id}/zip`;
    banner.classList.toggle("hidden", !p.zip_path);
  }

  const tasks = $("tasks");
  tasks.innerHTML = "";
  for (const t of p.tasks) {
    const li = document.createElement("li");
    li.className = `task t-${t.status}`;
    const notes = t.review_notes ? `<div class="notes">${escape(t.review_notes)}</div>` : "";
    li.innerHTML = `<div class="title">${escape(t.title)}</div>
      <div class="meta">${t.status} · ${t.attempts} essai(s) · ${t.file_paths.length} fichier(s)</div>
      ${notes}`;
    tasks.appendChild(li);
  }
  $("tasks-count").textContent = p.tasks.length;

  renderFileTree(id, p.files);
  $("files-count").textContent = p.files.length;
  updateProgress();
}

// ============================================================================
// Progress counters (header chip)
// ============================================================================

function updateProgress() {
  const p = state.currentProject;
  const el = $("proj-progress");
  if (!p) { el.innerHTML = ""; return; }
  const totalTasks = p.tasks.length;
  const doneTasks = p.tasks.filter((t) => t.status === "done").length;
  const plannedFiles = new Set();
  for (const t of p.tasks) for (const fp of (t.file_paths || [])) plannedFiles.add(fp);
  const writtenFiles = p.files.length;
  const totalFiles = Math.max(plannedFiles.size, writtenFiles);
  el.innerHTML =
    `<b>${writtenFiles}</b>/${totalFiles || "?"} fichiers` +
    `<span class="sep">·</span>` +
    `<b>${doneTasks}</b>/${totalTasks || "?"} tâches`;
}

// ============================================================================
// File tree (Livrables tab)
// ============================================================================

function renderFileTree(projectId, files) {
  const root = $("files-tree");
  root.innerHTML = "";
  if (!files.length) {
    const empty = document.createElement("div");
    empty.className = "tree-node";
    empty.style.color = "var(--muted)";
    empty.style.fontStyle = "italic";
    empty.textContent = "Aucun fichier écrit pour l'instant.";
    root.appendChild(empty);
    return;
  }
  // Build an object tree from flat paths.
  const tree = {};
  for (const f of files) {
    const parts = f.path.split("/");
    let node = tree;
    for (let i = 0; i < parts.length - 1; i++) {
      node[parts[i]] = node[parts[i]] || {};
      node = node[parts[i]];
    }
    node[parts[parts.length - 1]] = { __file: f };
  }
  const walk = (node, depth, prefix) => {
    const entries = Object.entries(node).sort(([a, av], [b, bv]) => {
      const aIsDir = !av.__file;
      const bIsDir = !bv.__file;
      if (aIsDir !== bIsDir) return aIsDir ? -1 : 1;
      return a.localeCompare(b);
    });
    for (const [name, child] of entries) {
      const div = document.createElement("div");
      div.style.paddingLeft = `${depth * 14 + 4}px`;
      if (child.__file) {
        div.className = "tree-node file";
        const f = child.__file;
        div.innerHTML = `📄 ${escape(name)}<span class="rev">r${f.revision}</span>`;
        div.addEventListener("click", () => selectFile(projectId, f.path, div));
        if (state.selectedFile === f.path) div.classList.add("active");
      } else {
        div.className = "tree-node dir";
        div.textContent = `📁 ${name}/`;
      }
      root.appendChild(div);
      if (!child.__file) walk(child, depth + 1, prefix + name + "/");
    }
  };
  walk(tree, 0, "");
}

async function selectFile(projectId, path, divEl) {
  state.selectedFile = path;
  for (const x of document.querySelectorAll(".tree-node.file")) x.classList.remove("active");
  divEl.classList.add("active");
  const r = await fetch(`/api/projects/${projectId}/files/${encodeURI(path)}`);
  if (r.ok) {
    const d = await r.json();
    $("file-preview").textContent = d.content;
  }
}

function setStatus(status) {
  const el = $("proj-status");
  el.className = `status-pill s-${status}`;
  el.textContent = status;
}

// ============================================================================
// WebSocket
// ============================================================================

function subscribe(id) {
  if (state.ws) { try { state.ws.close(); } catch {} }
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
    if (state.currentId === id) setTimeout(() => subscribe(id), 2000);
  };
  return Promise.resolve();
}

// ============================================================================
// Live file-streaming panel
// ============================================================================

function _streamListEl() { return $("stream"); }
function _streamEmptyEl() { return $("stream-empty"); }

function _ensureFileEntry(path, role) {
  let entry = state.stream.get(path);
  if (entry) return entry;

  const list = _streamListEl();
  const emptyEl = _streamEmptyEl();
  if (emptyEl) emptyEl.classList.add("hidden");

  const li = document.createElement("li");
  li.className = "stream-file writing";
  li.innerHTML =
    `<header class="stream-head">
       <span class="stream-status" title="en écriture">●</span>
       <span class="stream-path">${escape(path)}</span>
       <span class="stream-agent">${escape((role || "coder").toUpperCase())}</span>
     </header>
     <pre class="stream-body"></pre>`;
  list.appendChild(li);

  entry = {
    el: li,
    preEl: li.querySelector(".stream-body"),
    statusEl: li.querySelector(".stream-status"),
    content: "",
    role: role || "coder",
  };
  state.stream.set(path, entry);
  state.streamOrder.push(path);
  $("stream-count").textContent = state.streamOrder.length;
  return entry;
}

function streamFileStart(path, role) {
  const entry = _ensureFileEntry(path, role);
  entry.content = "";
  entry.preEl.textContent = "";
  entry.role = role || entry.role;
  entry.el.className = "stream-file writing";
  entry.statusEl.textContent = "●";
  entry.statusEl.title = "en écriture";
  entry.el.scrollIntoView({ behavior: "smooth", block: "nearest" });
}

function streamFileChunk(path, delta) {
  const entry = _ensureFileEntry(path, "coder");
  entry.content += delta;
  if (entry.content.length > FILE_CAP) {
    entry.content = "…" + entry.content.slice(-FILE_CAP);
  }
  entry.preEl.textContent = entry.content;
  entry.preEl.scrollTop = entry.preEl.scrollHeight;
}

function streamFileEnd(path) {
  const entry = state.stream.get(path);
  if (!entry) return;
  entry.el.className = "stream-file written";
  entry.statusEl.textContent = "○";
  entry.statusEl.title = "écriture terminée";
}

function streamFileReviewStart(path) {
  const entry = _ensureFileEntry(path, "reviewer");
  entry.el.className = "stream-file reviewing";
  entry.statusEl.textContent = "?";
  entry.statusEl.title = "en relecture";
}

function streamFileReviewEnd(path, approved) {
  const entry = state.stream.get(path);
  if (!entry) return;
  entry.el.className = "stream-file " + (approved ? "approved" : "rejected");
  entry.statusEl.textContent = approved ? "✓" : "✗";
  entry.statusEl.title = approved ? "approuvé" : "rejeté";
}

function _resetStream() {
  const list = _streamListEl();
  if (list) list.innerHTML = "";
  state.stream.clear();
  state.streamOrder = [];
  $("stream-count").textContent = "0";
  const emptyEl = _streamEmptyEl();
  if (emptyEl) emptyEl.classList.remove("hidden");
}

function handleEvent(id, msg) {
  if (msg.kind === "ping") return;
  if (msg.id) {
    if (state.seenEvents.has(msg.id)) return;
    state.seenEvents.add(msg.id);
  }

  // File-level streaming events drive the "Fichiers" panel (live file writing
  // + review verdicts). They're silent on the event log so they don't spam.
  if (msg.kind === "file_start") {
    const path = msg.data?.path || msg.message;
    if (path) streamFileStart(path, msg.role || "coder");
    return;
  }
  if (msg.kind === "file_chunk") {
    const path = msg.data?.path;
    const delta = msg.data?.delta || "";
    if (path) streamFileChunk(path, delta);
    return;
  }
  if (msg.kind === "file_end") {
    const path = msg.data?.path || msg.message;
    if (path) streamFileEnd(path);
    return;
  }
  if (msg.kind === "file_review_start") {
    const path = msg.data?.path || msg.message;
    if (path) streamFileReviewStart(path);
    return;
  }
  if (msg.kind === "file_review_end") {
    const path = msg.data?.path || msg.message;
    const ok = msg.data?.approved !== false;
    if (path) streamFileReviewEnd(path, ok);
    return;
  }

  if (msg.kind === "state") {
    const s = msg.data?.status;
    if (s) setStatus(s);
    loadProject(id);
    refreshProjects();
    appendLog(msg);
    return;
  }

  if (msg.kind === "token") {
    const line = $("current-action");
    const cur = line.dataset.role === msg.role ? line.dataset.buf || "" : "";
    const buf = (cur + (msg.message || "")).slice(-180);
    line.dataset.role = msg.role;
    line.dataset.buf = buf;
    line.textContent = `${(ROLE_LABEL[msg.role] || msg.role || "").toUpperCase()} · ${buf}`;
    return;
  }

  if (msg.kind === "agent") {
    $("current-action").textContent =
      `${(ROLE_LABEL[msg.role] || msg.role || "").toUpperCase()} · ${msg.message || ""}`;
    $("current-action").dataset.buf = "";
    return;
  }

  appendLog(msg);
  if (msg.kind === "test" || msg.kind === "error") loadProject(id);
}

// ============================================================================
// Event log (events tab)
// ============================================================================

function appendLog(msg) {
  const logs = $("logs");
  const key = `${msg.kind}|${msg.role || ""}|${msg.message || ""}`;
  if (key === state.lastLogKey && state.lastLogCountEl) {
    state.lastLogCount += 1;
    state.lastLogCountEl.textContent = ` ×${state.lastLogCount}`;
    return;
  }
  const div = document.createElement("div");
  div.className = `log-line k-${msg.kind}` + (msg.role ? ` r-${msg.role}` : "");
  const time = new Date().toLocaleTimeString();
  const role = msg.role ? `<span class="log-role">${escape(msg.role)}</span>` : "";

  const data = msg.data || {};
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
  const detailsText = parts.join("\n\n");

  div.innerHTML =
    `<span class="log-time">${time}</span>${role}` +
    `<span class="log-msg">${escape(msg.message || "")}</span>` +
    `<span class="log-count"></span>`;

  if (detailsText) {
    const details = document.createElement("pre");
    // Inline by default (no click-to-expand) so pytest / stderr output is
    // immediately visible. Colored red for errors so failures pop.
    let cls = "log-details";
    if (msg.kind === "error") cls += " err";
    else if (msg.kind === "warning") cls += " warn";
    details.className = cls;
    details.textContent = detailsText;
    div.appendChild(details);
  }

  logs.appendChild(div);
  state.lastLogKey = key;
  state.lastLogCount = 1;
  state.lastLogCountEl = div.querySelector(".log-count");
  state.eventCount += 1;
  $("events-count").textContent = state.eventCount;
  while (logs.children.length > 200) logs.removeChild(logs.firstChild);
  logs.scrollTop = logs.scrollHeight;
}

// ============================================================================
// Notes mailbox
// ============================================================================

async function loadNotes(id) {
  try {
    const r = await fetch(`/api/projects/${id}/notes`);
    if (!r.ok) return;
    const notes = await r.json();
    for (const n of notes) {
      appendLog({
        kind: "info",
        role: "user",
        message: `Note de l'utilisateur : ${n.content}`,
      });
    }
  } catch (_) { /* silent */ }
}

async function postNote(id, content) {
  const r = await fetch(`/api/projects/${id}/notes`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ content }),
  });
  return r.ok;
}

// ============================================================================
// UI wiring
// ============================================================================

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
    alert("Création échouée");
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
$("btn-stop").addEventListener("click", async () => {
  if (!state.currentId) return;
  if (!confirm("Arrêter le projet en cours ? Les fichiers déjà écrits sont conservés.")) return;
  await fetch(`/api/projects/${state.currentId}/control`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ action: "stop" }),
  });
});
$("note-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  if (!state.currentId) return;
  const ta = $("note-input");
  const content = ta.value.trim();
  if (!content) return;
  const ok = await postNote(state.currentId, content);
  if (ok) {
    ta.value = "";
    // Event stream will surface it as an "info" log (server-side emit).
  } else {
    alert("Envoi de la note échoué");
  }
});
// Submit on Ctrl+Enter / Cmd+Enter so users don't have to reach for the button.
$("note-input").addEventListener("keydown", (e) => {
  if ((e.ctrlKey || e.metaKey) && e.key === "Enter") {
    e.preventDefault();
    $("note-form").dispatchEvent(new Event("submit", { cancelable: true }));
  }
});
$("btn-delete").addEventListener("click", async () => {
  if (!state.currentId) return;
  const current = state.projects.find((p) => p.id === state.currentId);
  const label = current ? current.name : state.currentId;
  if (!confirm(`Supprimer « ${label} » ?`)) return;
  await deleteProject(state.currentId);
});

// Drawer tabs
for (const tab of document.querySelectorAll(".drawer-tab")) {
  tab.addEventListener("click", () => {
    for (const t of document.querySelectorAll(".drawer-tab")) t.classList.remove("active");
    for (const p of document.querySelectorAll(".pane")) p.classList.remove("active");
    tab.classList.add("active");
    $("pane-" + tab.dataset.tab).classList.add("active");
  });
}

// Mobile sidebar toggle
$("btn-menu").addEventListener("click", () => {
  $("sidebar").classList.toggle("open");
});

function escape(s) {
  return String(s).replace(/[&<>]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" })[c]);
}

// ============================================================================
// Boot
// ============================================================================

refreshServers();
refreshProjects();
setInterval(refreshServers, 15000);
setInterval(refreshProjects, 10000);
