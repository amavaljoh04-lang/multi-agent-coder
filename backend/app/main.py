"""FastAPI entrypoint: HTTP + WebSocket + static frontend."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import shutil
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import desc, select

from .config import get_config, get_settings
from .database import SessionLocal, init_db
from .events import bus
from .models import Event, Project, ProjectFile, ProjectNote, ProjectStatus, Task
from .ollama_client import get_router, shutdown_router
from .orchestrator import get_orchestrator
from .schemas import (
    ChatRequest,
    ChatResponse,
    ControlRequest,
    CreateNoteRequest,
    CreateProjectRequest,
    EventView,
    FileView,
    NoteView,
    ProjectDetail,
    ProjectSummary,
    ServerStatus,
    TaskView,
)
from .web_search import search_and_format

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s :: %(message)s",
)
log = logging.getLogger("mac.main")

REPO_ROOT = Path(__file__).resolve().parents[2]
FRONTEND_DIR = REPO_ROOT / "frontend"


_background: set[asyncio.Task] = set()


def _spawn(coro) -> None:
    task = asyncio.create_task(coro)
    _background.add(task)
    task.add_done_callback(_background.discard)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    router = get_router()
    # Warm up probes in background.
    _spawn(router.probe_all())
    orch = get_orchestrator()
    # Resume in-flight projects after a crash / reboot.
    _spawn(orch.resume_all())
    try:
        yield
    finally:
        await shutdown_router()


app = FastAPI(
    title="Multi-Agent Coder",
    version="0.1.0",
    lifespan=lifespan,
)


# ---------------------------------------------------------------- API routes


@app.get("/api/health")
async def health() -> dict:
    return {"ok": True}


@app.get("/api/config")
async def view_config() -> dict:
    cfg = get_config()
    return {
        "servers": {k: v.model_dump() for k, v in cfg.servers.items()},
        "roles": {k: v.model_dump() for k, v in cfg.roles.items()},
        "orchestrator": cfg.orchestrator.model_dump(),
    }


@app.get("/api/servers", response_model=list[ServerStatus])
async def list_servers() -> list[ServerStatus]:
    cfg = get_config()
    router = get_router()
    probes = await router.probe_all()
    result: list[ServerStatus] = []
    for p in probes:
        label = cfg.servers[p["name"]].label
        result.append(
            ServerStatus(
                name=p["name"],
                label=label,
                url=p["url"],
                online=p["online"],
                models=p["models"],
                error=p["error"],
            )
        )
    return result


INTENT_SYSTEM = (
    "You are an intent classifier. The user sends a message in a chat. "
    "Decide if they want to GENERATE a coding project, or just CHAT. "
    "Respond with ONLY valid JSON, no markdown, no prose.\n"
    '{"intent":"chat","reply":"your conversational reply here"}\n'
    "OR\n"
    '{"intent":"generate","project_name":"short-name","project_prompt":"full spec","reply":"brief ack"}\n'
    "Rules:\n"
    "- Greetings like 'salut', 'hello', 'bonjour', 'ça va' → intent=chat\n"
    "- Questions about you, tech topics, general discussion → intent=chat\n"
    "- Explicit requests like 'crée', 'génère', 'build', 'code', 'make me' → intent=generate\n"
    "- Reply in the same language as the user.\n"
    "- Keep replies short and friendly for chat intent.\n"
    "- For generate intent, extract a clear project_name (slug) and project_prompt (full spec)."
)

CHAT_SYSTEM = (
    "You are a friendly AI assistant embedded in Multi-Agent Coder, a tool "
    "that generates full coding projects via an AI pipeline. When the user "
    "chats casually, respond helpfully and concisely. Reply in the same "
    "language as the user. Keep responses short (2-4 sentences max).\n"
    "IMPORTANT: If the user asks a factual question you are not sure about, "
    "or asks about current events, recent technologies, APIs, libraries, "
    "or anything you might not have accurate information on, you MUST "
    "reply with EXACTLY: [SEARCH:your search query here]\n"
    "Example: User asks 'What is the latest version of React?' → "
    "reply '[SEARCH:latest React version 2026]'\n"
    "Only use [SEARCH:...] when you genuinely lack confidence. "
    "For greetings, opinions, or things you know well, answer directly."
)

_SEARCH_RE = re.compile(r"\[SEARCH:(.+?)\]", re.IGNORECASE)

CHAT_WITH_CONTEXT_SYSTEM = (
    "You are a friendly AI assistant embedded in Multi-Agent Coder. "
    "Answer the user's question using the web search results provided below. "
    "Be concise (2-4 sentences). Cite sources when relevant. "
    "Reply in the same language as the user."
)


@app.post("/api/chat", response_model=ChatResponse)
async def chat(req: ChatRequest) -> ChatResponse:
    router = get_router()

    # Build messages for intent classification.
    messages: list[dict[str, str]] = [{"role": "system", "content": INTENT_SYSTEM}]
    for h in req.history[-6:]:
        messages.append({"role": h.get("role", "user"), "content": h.get("content", "")})
    messages.append({"role": "user", "content": req.message})

    try:
        raw = await router.chat("dispatcher", messages, json_mode=True)
        data = json.loads(raw)
        intent = data.get("intent", "chat")
        reply = data.get("reply", "")
        if intent == "generate":
            return ChatResponse(
                reply=reply or "C'est parti, je lance le pipeline !",
                intent="generate",
                project_name=data.get("project_name", "project"),
                project_prompt=data.get("project_prompt", req.message),
            )
    except Exception:
        # Fallback: intent detection failed, try a simple chat response.
        intent = "chat"
        reply = ""

    if not reply:
        chat_messages: list[dict[str, str]] = [{"role": "system", "content": CHAT_SYSTEM}]
        for h in req.history[-6:]:
            chat_messages.append({"role": h.get("role", "user"), "content": h.get("content", "")})
        chat_messages.append({"role": "user", "content": req.message})
        try:
            reply = await router.chat("dispatcher", chat_messages)
        except Exception:
            reply = "Désolé, je n'arrive pas à joindre le modèle pour le moment. Réessaye dans quelques instants."

    # Web search fallback: if the model signals it needs to search.
    search_match = _SEARCH_RE.search(reply)
    if search_match:
        query = search_match.group(1).strip()
        log.info("Chat triggered web search: %r", query)
        search_context = await search_and_format(query)
        if search_context:
            augmented_messages: list[dict[str, str]] = [
                {"role": "system", "content": CHAT_WITH_CONTEXT_SYSTEM},
            ]
            for h in req.history[-4:]:
                augmented_messages.append({"role": h.get("role", "user"), "content": h.get("content", "")})
            augmented_messages.append(
                {"role": "user", "content": f"{req.message}\n\n{search_context}"}
            )
            try:
                reply = await router.chat("dispatcher", augmented_messages)
            except Exception:
                reply = reply.replace(search_match.group(0), "(recherche web indisponible)")
        else:
            reply = reply.replace(search_match.group(0), "(aucun résultat trouvé)")

    return ChatResponse(reply=reply, intent="chat")


@app.post("/api/projects", response_model=ProjectSummary)
async def create_project(req: CreateProjectRequest) -> ProjectSummary:
    async with SessionLocal() as s:
        project = Project(name=req.name, prompt=req.prompt, status=ProjectStatus.CREATED)
        s.add(project)
        await s.commit()
        await s.refresh(project)
    await get_orchestrator().enqueue(project.id)
    return ProjectSummary.model_validate(project)


@app.get("/api/projects", response_model=list[ProjectSummary])
async def list_projects() -> list[ProjectSummary]:
    async with SessionLocal() as s:
        rows = (
            await s.execute(select(Project).order_by(desc(Project.created_at)))
        ).scalars().all()
        return [ProjectSummary.model_validate(r) for r in rows]


@app.get("/api/projects/{project_id}", response_model=ProjectDetail)
async def get_project(project_id: str) -> ProjectDetail:
    async with SessionLocal() as s:
        project = await s.get(Project, project_id)
        if project is None:
            raise HTTPException(404, "Project not found")
        tasks = (
            await s.execute(
                select(Task).where(Task.project_id == project_id).order_by(Task.order_index)
            )
        ).scalars().all()
        files = (
            await s.execute(
                select(ProjectFile).where(ProjectFile.project_id == project_id).order_by(ProjectFile.path)
            )
        ).scalars().all()
        return ProjectDetail(
            id=project.id,
            name=project.name,
            prompt=project.prompt,
            status=project.status,
            iteration=project.iteration,
            created_at=project.created_at,
            updated_at=project.updated_at,
            workspace_path=project.workspace_path,
            zip_path=project.zip_path,
            last_error=project.last_error,
            tasks=[TaskView.model_validate(t) for t in tasks],
            files=[FileView.model_validate(f) for f in files],
        )


@app.get("/api/projects/{project_id}/events", response_model=list[EventView])
async def project_events(project_id: str, limit: int = 500) -> list[EventView]:
    async with SessionLocal() as s:
        rows = (
            await s.execute(
                select(Event)
                .where(Event.project_id == project_id)
                .order_by(Event.created_at)
                .limit(limit)
            )
        ).scalars().all()
        return [EventView.model_validate(r) for r in rows]


@app.get("/api/projects/{project_id}/files/{file_path:path}")
async def get_file_content(project_id: str, file_path: str) -> JSONResponse:
    async with SessionLocal() as s:
        rows = (
            await s.execute(
                select(ProjectFile)
                .where(ProjectFile.project_id == project_id, ProjectFile.path == file_path)
            )
        ).scalars().first()
        if rows is None:
            raise HTTPException(404, "File not found")
        return JSONResponse({"path": rows.path, "content": rows.content, "revision": rows.revision})


@app.delete("/api/projects/{project_id}")
async def delete_project(project_id: str) -> dict:
    orch = get_orchestrator()
    await orch.cancel(project_id)
    async with SessionLocal() as s:
        project = await s.get(Project, project_id)
        if project is None:
            raise HTTPException(404, "Project not found")
        workspace_path = project.workspace_path
        zip_path = project.zip_path
        await s.delete(project)
        await s.commit()
    # Best-effort filesystem cleanup (workspace dir + ZIP).
    for p in (workspace_path, zip_path):
        if not p:
            continue
        path = Path(p)
        try:
            if path.is_dir():
                shutil.rmtree(path, ignore_errors=True)
            elif path.exists():
                path.unlink(missing_ok=True)
        except OSError as exc:
            log.warning("cleanup of %s failed: %s", p, exc)
    return {"ok": True}


@app.post("/api/projects/{project_id}/control")
async def control_project(project_id: str, req: ControlRequest) -> dict:
    orch = get_orchestrator()
    if req.action == "pause":
        await orch.pause(project_id)
    elif req.action in {"resume", "retry"}:
        await orch.enqueue(project_id)
    elif req.action == "stop":
        # Hard-stop: cancel the asyncio task and mark the project FAILED so
        # the user can see why it stopped. Project row is kept (unlike
        # delete) so the user can inspect what was produced.
        await orch.stop(project_id)
    else:
        raise HTTPException(400, f"Unknown action {req.action}")
    return {"ok": True}


@app.get("/api/projects/{project_id}/notes", response_model=list[NoteView])
async def list_notes(project_id: str) -> list[NoteView]:
    async with SessionLocal() as s:
        rows = (
            await s.execute(
                select(ProjectNote)
                .where(ProjectNote.project_id == project_id)
                .order_by(ProjectNote.created_at)
            )
        ).scalars().all()
        return [
            NoteView(
                id=r.id,
                content=r.content,
                acknowledged=bool(r.acknowledged),
                created_at=r.created_at,
            )
            for r in rows
        ]


@app.post("/api/projects/{project_id}/notes", response_model=NoteView)
async def add_note(project_id: str, req: CreateNoteRequest) -> NoteView:
    async with SessionLocal() as s:
        project = await s.get(Project, project_id)
        if project is None:
            raise HTTPException(404, "Project not found")
        note = ProjectNote(project_id=project_id, content=req.content.strip())
        s.add(note)
        await s.commit()
        await s.refresh(note)
    # Surface the note in the event stream so it appears in the Journal.
    await get_orchestrator().emit(
        project_id,
        "info",
        "user",
        f"Note de l'utilisateur : {req.content.strip()[:200]}",
    )
    return NoteView(
        id=note.id,
        content=note.content,
        acknowledged=bool(note.acknowledged),
        created_at=note.created_at,
    )


@app.get("/api/projects/{project_id}/zip")
async def download_zip(project_id: str) -> FileResponse:
    async with SessionLocal() as s:
        project = await s.get(Project, project_id)
        if project is None:
            raise HTTPException(404, "Project not found")
        if not project.zip_path or not Path(project.zip_path).exists():
            raise HTTPException(409, "ZIP not ready yet")
        return FileResponse(
            project.zip_path,
            media_type="application/zip",
            filename=f"{project.name or project.id}.zip",
        )


# --------------------------------------------------------------- WebSocket


@app.websocket("/ws/projects/{project_id}")
async def ws_project(websocket: WebSocket, project_id: str) -> None:
    await websocket.accept()
    q = await bus.subscribe(project_id)
    try:
        # Replay recent history so newly connected clients see something.
        async with SessionLocal() as s:
            rows = (
                await s.execute(
                    select(Event)
                    .where(Event.project_id == project_id)
                    .order_by(Event.created_at.desc())
                    .limit(200)
                )
            ).scalars().all()
        for ev in reversed(rows):
            await websocket.send_json(
                {
                    "id": ev.id,
                    "kind": ev.kind,
                    "role": ev.role,
                    "message": ev.message,
                    "data": ev.data or {},
                    "replay": True,
                }
            )
        while True:
            try:
                event = await asyncio.wait_for(q.get(), timeout=20.0)
                await websocket.send_json(event)
            except TimeoutError:
                await websocket.send_json({"kind": "ping", "role": "", "message": "", "data": {}})
    except WebSocketDisconnect:
        pass
    finally:
        await bus.unsubscribe(project_id, q)


# ------------------------------------------------------------ static frontend


if FRONTEND_DIR.exists():
    app.mount("/assets", StaticFiles(directory=str(FRONTEND_DIR / "assets")), name="assets")

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(str(FRONTEND_DIR / "index.html"))

    @app.get("/favicon.ico")
    async def favicon() -> FileResponse:
        f = FRONTEND_DIR / "assets" / "favicon.ico"
        if f.exists():
            return FileResponse(str(f))
        raise HTTPException(404)


def run() -> None:
    import uvicorn

    settings = get_settings()
    uvicorn.run(
        "app.main:app",
        host=settings.host,
        port=settings.port,
        log_level="info",
        reload=False,
    )


if __name__ == "__main__":
    run()
