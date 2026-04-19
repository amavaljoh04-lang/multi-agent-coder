/* Multi-Agent Coder — UI client + neural-graph visualization. */

const $ = (id) => document.getElementById(id);

const ROLE_LABEL = {
  planner: "Planner",
  architect: "Architect",
  coder: "Coder",
  reviewer: "Reviewer",
  tester: "Tester",
  analyst: "Analyst",
};

const ROLE_COLORS = {
  planner: "#a78bfa",
  architect: "#38bdf8",
  coder: "#5eead4",
  reviewer: "#fbbf24",
  tester: "#4ade80",
  analyst: "#ff3ea5",
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
  graph: null,
  idleGraph: null,
  // per-file streaming visualisation
  stream: new Map(), // path -> { el, preEl, headerEl, statusEl, content }
  streamOrder: [],
};

const FILE_CAP = 8000; // max characters kept in the live pre-element per file

// ============================================================================
// Neural graph engine
// ============================================================================

class NeuralGraph {
  /**
   * Interactive neural-network visualization.
   * @param {HTMLCanvasElement} canvas
   * @param {object} opts
   */
  constructor(canvas, opts = {}) {
    this.canvas = canvas;
    this.ctx = canvas.getContext("2d");
    this.idle = !!opts.idle;
    this.nodes = []; // agent nodes
    this.ambient = []; // background particles
    this.edges = [];
    this.particles = []; // flowing impulses on edges
    this.size = { w: 0, h: 0 };
    this.raf = null;

    // Fixed normalized positions (0-1). Re-projected to pixels each frame.
    this._layout = [
      { key: "planner",   label: "Planner",   x: 0.18, y: 0.22 },
      { key: "architect", label: "Architect", x: 0.55, y: 0.12 },
      { key: "coder",     label: "Coder",     x: 0.5,  y: 0.5  },
      { key: "reviewer",  label: "Reviewer",  x: 0.85, y: 0.38 },
      { key: "tester",    label: "Tester",    x: 0.82, y: 0.78 },
      { key: "analyst",   label: "Analyst",   x: 0.18, y: 0.75 },
    ];
    this._edgeDefs = [
      ["planner", "architect"],
      ["architect", "coder"],
      ["coder", "reviewer"],
      ["reviewer", "coder"],
      ["coder", "tester"],
      ["tester", "analyst"],
      ["analyst", "coder"],
      ["planner", "coder"],
    ];

    this._build();
    this._onResize = this._onResize.bind(this);
    window.addEventListener("resize", this._onResize);
    this._onResize();
    this.start();

    // Ambient firing (always on, soft).
    this._ambientTimer = setInterval(() => this._maybeAmbientFire(), 260);
  }

  destroy() {
    cancelAnimationFrame(this.raf);
    clearInterval(this._ambientTimer);
    window.removeEventListener("resize", this._onResize);
  }

  _build() {
    // Agent nodes.
    for (const n of this._layout) {
      this.nodes.push({
        ...n,
        color: ROLE_COLORS[n.key] || "#5eead4",
        activity: 0, // 0-1 decaying
        radius: 22,
        orbit: Math.random() * Math.PI * 2,
      });
    }
    // Edges with directed information.
    for (const [a, b] of this._edgeDefs) {
      this.edges.push({ a, b, flow: 0 });
    }
    // Ambient "background neurons".
    const N = this.idle ? 110 : 65;
    for (let i = 0; i < N; i++) {
      this.ambient.push({
        x: Math.random(),
        y: Math.random(),
        r: 0.5 + Math.random() * 1.2,
        vx: (Math.random() - 0.5) * 0.00015,
        vy: (Math.random() - 0.5) * 0.00015,
        phase: Math.random() * Math.PI * 2,
        fire: 0,
      });
    }
  }

  _onResize() {
    const rect = this.canvas.getBoundingClientRect();
    const dpr = window.devicePixelRatio || 1;
    this.canvas.width = Math.max(1, Math.floor(rect.width * dpr));
    this.canvas.height = Math.max(1, Math.floor(rect.height * dpr));
    this.ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    this.size = { w: rect.width, h: rect.height };
  }

  _pos(n) {
    return { x: n.x * this.size.w, y: n.y * this.size.h };
  }
  _node(key) { return this.nodes.find((n) => n.key === key); }

  _maybeAmbientFire() {
    if (Math.random() < 0.7) {
      const i = (Math.random() * this.ambient.length) | 0;
      this.ambient[i].fire = 1;
    }
    // In idle mode, softly fire random agent nodes to keep it alive.
    if (this.idle && Math.random() < 0.35) {
      const n = this.nodes[(Math.random() * this.nodes.length) | 0];
      this.pulse(n.key, 0.5);
      if (Math.random() < 0.5) {
        const e = this.edges[(Math.random() * this.edges.length) | 0];
        this.emit(e.a, e.b);
      }
    }
  }

  /**
   * Register an activity on a role: glow the node AND emit particles outward
   * along every outgoing edge so you visually see the work "radiate" from it.
   */
  pulse(roleKey, intensity = 1) {
    const node = this._node(roleKey);
    if (!node) return;
    node.activity = Math.min(1, node.activity + intensity);
    node.emitAccum = (node.emitAccum || 0) + intensity;

    // outgoing edges light up (brighter than incoming) + particles shoot outward
    const outgoing = this.edges.filter((e) => e.a === roleKey);
    const incoming = this.edges.filter((e) => e.b === roleKey);
    for (const e of outgoing) {
      e.flow = Math.min(1, e.flow + 0.9 * intensity);
    }
    for (const e of incoming) {
      e.flow = Math.min(1, e.flow + 0.25 * intensity);
    }

    // Spawn particles radiating outward. Number scales with intensity.
    const n = Math.max(1, Math.min(4, Math.ceil(intensity * 3)));
    for (let i = 0; i < n; i++) {
      for (const e of outgoing) {
        setTimeout(() => this.emit(e.a, e.b, node.color), i * 55);
      }
    }
  }

  /** Emit a single particle along a directed edge. */
  emit(fromKey, toKey, color) {
    const from = this._node(fromKey);
    const to = this._node(toKey);
    if (!from || !to) return;
    this.particles.push({
      from,
      to,
      t: 0,
      speed: 0.008 + Math.random() * 0.006,
      color: color || to.color,
      size: 2 + Math.random() * 1.4,
    });
  }

  /** Record a state transition: burst of particles along the pipeline edge. */
  transition(status) {
    const map = {
      planning:     [[null, "planner"]],
      architecting: [["planner", "architect"]],
      coding:       [["architect", "coder"]],
      reviewing:    [["coder", "reviewer"]],
      testing:      [["coder", "tester"]],
      fixing:       [["tester", "analyst"], ["analyst", "coder"]],
      packaging:    [["coder", "tester"]],
    };
    const pairs = map[status] || [];
    for (const [a, b] of pairs) {
      if (a) {
        const from = this._node(a);
        // big burst of particles from predecessor to successor
        for (let i = 0; i < 8; i++) {
          setTimeout(() => this.emit(a, b, from ? from.color : null), i * 70);
        }
      }
      this.pulse(b, 0.9);
    }
  }

  start() {
    const loop = () => {
      this._draw();
      this.raf = requestAnimationFrame(loop);
    };
    loop();
  }

  _draw() {
    const { ctx } = this;
    const { w, h } = this.size;
    if (w === 0 || h === 0) return;
    ctx.clearRect(0, 0, w, h);

    // Ambient neurons.
    for (const p of this.ambient) {
      p.x = (p.x + p.vx + 1) % 1;
      p.y = (p.y + p.vy + 1) % 1;
      p.phase += 0.02;
      const x = p.x * w;
      const y = p.y * h;
      const base = 0.22 + 0.18 * Math.sin(p.phase);
      const glow = p.fire;
      ctx.fillStyle = `rgba(120, 180, 220, ${base * 0.4})`;
      ctx.beginPath();
      ctx.arc(x, y, p.r, 0, Math.PI * 2);
      ctx.fill();
      if (glow > 0.02) {
        ctx.fillStyle = `rgba(94, 234, 212, ${glow * 0.8})`;
        ctx.beginPath();
        ctx.arc(x, y, p.r + glow * 2.2, 0, Math.PI * 2);
        ctx.fill();
        p.fire *= 0.9;
      }
    }

    // Ambient faint connection web to nearest 2 neighbours (decorative).
    ctx.strokeStyle = "rgba(94, 234, 212, 0.05)";
    ctx.lineWidth = 0.6;
    for (let i = 0; i < this.ambient.length; i++) {
      for (let j = i + 1; j < Math.min(i + 4, this.ambient.length); j++) {
        const a = this.ambient[i];
        const b = this.ambient[j];
        const dx = (a.x - b.x) * w;
        const dy = (a.y - b.y) * h;
        const d = Math.hypot(dx, dy);
        if (d < 110) {
          ctx.beginPath();
          ctx.moveTo(a.x * w, a.y * h);
          ctx.lineTo(b.x * w, b.y * h);
          ctx.stroke();
        }
      }
    }

    // Main edges.
    for (const e of this.edges) {
      const a = this._node(e.a);
      const b = this._node(e.b);
      const pa = this._pos(a);
      const pb = this._pos(b);
      const baseAlpha = 0.14;
      const flowAlpha = 0.55 * e.flow;
      const grad = ctx.createLinearGradient(pa.x, pa.y, pb.x, pb.y);
      grad.addColorStop(0, this._rgba(a.color, baseAlpha + flowAlpha));
      grad.addColorStop(1, this._rgba(b.color, baseAlpha + flowAlpha));
      ctx.strokeStyle = grad;
      ctx.lineWidth = 1.2 + 1.8 * e.flow;
      ctx.beginPath();
      ctx.moveTo(pa.x, pa.y);
      ctx.lineTo(pb.x, pb.y);
      ctx.stroke();
      e.flow *= 0.96;
    }

    // Particles.
    this.particles = this.particles.filter((p) => p.t <= 1);
    for (const p of this.particles) {
      p.t += p.speed;
      const pa = this._pos(p.from);
      const pb = this._pos(p.to);
      const x = pa.x + (pb.x - pa.x) * p.t;
      const y = pa.y + (pb.y - pa.y) * p.t;
      ctx.fillStyle = this._rgba(p.color, 0.95);
      ctx.beginPath();
      ctx.arc(x, y, p.size, 0, Math.PI * 2);
      ctx.fill();
      // soft trail
      ctx.fillStyle = this._rgba(p.color, 0.18);
      ctx.beginPath();
      ctx.arc(x, y, p.size * 4, 0, Math.PI * 2);
      ctx.fill();
    }

    // Agent nodes.
    for (const n of this.nodes) {
      const p = this._pos(n);
      n.orbit += 0.025 + n.activity * 0.06;

      // outer halo
      const halo = 0.2 + 0.55 * n.activity;
      const rOuter = n.radius + 18 + 12 * n.activity;
      const g = ctx.createRadialGradient(p.x, p.y, n.radius * 0.3, p.x, p.y, rOuter);
      g.addColorStop(0, this._rgba(n.color, halo));
      g.addColorStop(1, this._rgba(n.color, 0));
      ctx.fillStyle = g;
      ctx.beginPath();
      ctx.arc(p.x, p.y, rOuter, 0, Math.PI * 2);
      ctx.fill();

      // orbit ring (decorative)
      ctx.strokeStyle = this._rgba(n.color, 0.15 + n.activity * 0.45);
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.arc(p.x, p.y, n.radius + 8, 0, Math.PI * 2);
      ctx.stroke();

      // rotating dashes when active
      if (n.activity > 0.05) {
        ctx.save();
        ctx.translate(p.x, p.y);
        ctx.rotate(n.orbit);
        ctx.strokeStyle = this._rgba(n.color, 0.6 * n.activity);
        ctx.lineWidth = 2;
        ctx.setLineDash([4, 10]);
        ctx.beginPath();
        ctx.arc(0, 0, n.radius + 6, 0, Math.PI * 2);
        ctx.stroke();
        ctx.setLineDash([]);
        ctx.restore();
      }

      // core
      ctx.fillStyle = "rgba(3, 6, 13, 1)";
      ctx.beginPath();
      ctx.arc(p.x, p.y, n.radius, 0, Math.PI * 2);
      ctx.fill();
      ctx.strokeStyle = this._rgba(n.color, 0.8 + n.activity * 0.2);
      ctx.lineWidth = 1.5 + n.activity * 1.5;
      ctx.beginPath();
      ctx.arc(p.x, p.y, n.radius, 0, Math.PI * 2);
      ctx.stroke();

      // inner dot
      ctx.fillStyle = this._rgba(n.color, 0.55 + n.activity * 0.45);
      ctx.beginPath();
      ctx.arc(p.x, p.y, 3 + 2 * n.activity, 0, Math.PI * 2);
      ctx.fill();

      // label
      ctx.fillStyle = n.activity > 0.3 ? n.color : "rgba(220, 230, 255, 0.8)";
      ctx.font = "600 10px 'Share Tech Mono', monospace";
      ctx.textAlign = "center";
      ctx.textBaseline = "middle";
      ctx.fillText(n.label.toUpperCase(), p.x, p.y + n.radius + 14);

      n.activity *= 0.97;
    }
  }

  _rgba(hex, a) {
    if (hex.startsWith("rgb")) return hex;
    const h = hex.replace("#", "");
    const n = parseInt(h.length === 3 ? h.split("").map((c) => c + c).join("") : h, 16);
    const r = (n >> 16) & 255;
    const g = (n >> 8) & 255;
    const b = n & 255;
    return `rgba(${r}, ${g}, ${b}, ${a})`;
  }
}

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
    _ensureIdleGraph();
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
  $("empty-state").classList.add("hidden");
  $("project-view").classList.remove("hidden");
  $("logs").innerHTML = "";
  $("file-preview").textContent = "";
  $("current-action").textContent = "initialisation…";
  $("events-count").textContent = "0";
  _resetStream();
  if (state.idleGraph) { state.idleGraph.destroy(); state.idleGraph = null; }
  _ensureGraph();
  _populateLegend();
  if (window.innerWidth <= 860) $("sidebar").classList.remove("open");
  await Promise.all([loadProject(id), subscribe(id)]);
  refreshProjects();
}

function _ensureGraph() {
  if (!state.graph) {
    state.graph = new NeuralGraph($("graph"), { idle: false });
  }
}

function _ensureIdleGraph() {
  if (!state.idleGraph) {
    state.idleGraph = new NeuralGraph($("idle-graph"), { idle: true });
  }
}

function _populateLegend() {
  const leg = $("graph-legend");
  leg.innerHTML = "";
  for (const key of ["planner", "architect", "coder", "reviewer", "tester", "analyst"]) {
    const el = document.createElement("span");
    el.className = "leg";
    el.innerHTML = `<span class="d" style="background:${ROLE_COLORS[key]};box-shadow:0 0 6px ${ROLE_COLORS[key]}"></span>${ROLE_LABEL[key]}`;
    leg.appendChild(el);
  }
}

async function loadProject(id) {
  const r = await fetch(`/api/projects/${id}`);
  if (!r.ok) return;
  const p = await r.json();
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
      <div class="meta">${t.status} · ${t.attempts} attempt(s) · ${t.file_paths.length} files</div>
      ${notes}`;
    tasks.appendChild(li);
  }
  $("tasks-count").textContent = p.tasks.length;

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
  $("files-count").textContent = p.files.length;
}

function setStatus(status) {
  const el = $("proj-status");
  el.className = `status-pill s-${status}`;
  el.textContent = status;
  if (state.graph) state.graph.transition(status);
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
       <span class="stream-status" title="en écriture">◉</span>
       <span class="stream-path">${escape(path)}</span>
       <span class="stream-agent r-${escape(role || "coder")}">${escape((role || "coder").toUpperCase())}</span>
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
  entry.statusEl.textContent = "◉";
  entry.statusEl.title = "en écriture";
  // Bring this file into view
  entry.el.scrollIntoView({ behavior: "smooth", block: "nearest" });
}

function streamFileChunk(path, delta) {
  const entry = _ensureFileEntry(path, "coder");
  entry.content += delta;
  if (entry.content.length > FILE_CAP) {
    entry.content = "…" + entry.content.slice(-FILE_CAP);
  }
  // Append incrementally instead of replacing the whole textContent so the
  // browser keeps the scroll position if the user isn't at the bottom.
  entry.preEl.textContent = entry.content;
  entry.preEl.scrollTop = entry.preEl.scrollHeight;
}

function streamFileEnd(path) {
  const entry = state.stream.get(path);
  if (!entry) return;
  entry.el.className = "stream-file written";
  entry.statusEl.textContent = "◎";
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
  entry.statusEl.textContent = approved ? "✓" : "!";
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

  // Every event drives the graph: the working agent emits particles outward.
  if (msg.role && state.graph) {
    const intensity = msg.kind === "token" ? 0.25 : msg.kind === "agent" ? 0.75 : 0.55;
    state.graph.pulse(msg.role, intensity);
  }

  // File-level streaming events drive the "Stream" panel (live file writing
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
    // update the "current action" line with the live tail
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
  state.eventCount += 1;
  $("events-count").textContent = state.eventCount;
  while (logs.children.length > 200) logs.removeChild(logs.firstChild);
  logs.scrollTop = logs.scrollHeight;
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
    alert("Creation failed");
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

_ensureIdleGraph();
refreshServers();
refreshProjects();
setInterval(refreshServers, 15000);
setInterval(refreshProjects, 10000);
