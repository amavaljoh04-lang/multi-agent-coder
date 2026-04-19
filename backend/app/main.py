"""FastAPI entrypoint: HTTP + WebSocket + static frontend."""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import desc, select

from .config import get_config, get_settings
from .database import SessionLocal, init_db
from .events import bus
from .models import Event, Project, ProjectFile, ProjectStatus, Task
from .ollama_client import get_router, shutdown_router
from .orchestrator import get_orchestrator
from .schemas import (
    ControlRequest,
    CreateProjectRequest,
    EventView,
    FileView,
    ProjectDetail,
    ProjectSummary,
    ServerStatus,
    TaskView,
)

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


@app.post("/api/projects/{project_id}/control")
async def control_project(project_id: str, req: ControlRequest) -> dict:
    orch = get_orchestrator()
    if req.action == "pause":
        await orch.pause(project_id)
    elif req.action in {"resume", "retry"}:
        await orch.enqueue(project_id)
    else:
        raise HTTPException(400, f"Unknown action {req.action}")
    return {"ok": True}


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
