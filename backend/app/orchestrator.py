"""Orchestrator: background worker that drives a project from prompt to ZIP.

State machine (persisted to the DB — a crash or reboot picks up where it
stopped):

    CREATED
       |
    PLANNING        (planner: splits the project into tasks)
       |
    ARCHITECTING    (architect: refines specs per file)
       |
    CODING <------+ (coder produces files for each task)
       |          |
    REVIEWING ----+ (reviewer either approves or sends back a revision list)
       |
    TESTING <-----+ (sandbox runs install + test)
       |          |
    FIXING -------+ (analyst + coder patch files; loop to TESTING)
       |
    PACKAGING       (zip the workspace)
       |
    COMPLETED
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import logging
import shutil
import zipfile
from pathlib import Path
from typing import Any

from sqlalchemy import select

from . import agents
from .config import get_config, get_settings
from .database import SessionLocal
from .events import bus
from .models import Event, Project, ProjectFile, ProjectStatus, Task, TaskStatus, TestRun
from .ollama_client import OllamaRouter, get_router
from .sandbox import Sandbox

log = logging.getLogger(__name__)


class Orchestrator:
    def __init__(self) -> None:
        self.cfg = get_config()
        self.settings = get_settings()
        self.router: OllamaRouter = get_router()
        self.sandbox = Sandbox(self.cfg.orchestrator)
        self._tasks: dict[str, asyncio.Task[Any]] = {}
        self._stop: dict[str, asyncio.Event] = {}

    # ------------------------------------------------------------------ API

    async def enqueue(self, project_id: str) -> None:
        """Start (or resume) the background worker for ``project_id``."""
        if project_id in self._tasks and not self._tasks[project_id].done():
            return
        stop = asyncio.Event()
        self._stop[project_id] = stop
        self._tasks[project_id] = asyncio.create_task(self._run(project_id, stop))

    async def pause(self, project_id: str) -> None:
        if project_id in self._stop:
            self._stop[project_id].set()
        await self._set_status(project_id, ProjectStatus.PAUSED)

    async def cancel(self, project_id: str) -> None:
        """Hard-stop a project's worker without persisting a PAUSED status
        (used by delete)."""
        stop = self._stop.get(project_id)
        if stop is not None:
            stop.set()
        task = self._tasks.get(project_id)
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._tasks.pop(project_id, None)
        self._stop.pop(project_id, None)

    async def resume_all(self) -> None:
        """Re-enqueue any project that was running when the server last died."""
        async with SessionLocal() as s:
            rows = (
                await s.execute(
                    select(Project).where(
                        Project.status.not_in(
                            [ProjectStatus.COMPLETED, ProjectStatus.FAILED, ProjectStatus.PAUSED]
                        )
                    )
                )
            ).scalars().all()
            ids = [p.id for p in rows]
        for pid in ids:
            await self.emit(pid, "info", "", "Resuming project after server restart")
            await self.enqueue(pid)

    # ------------------------------------------------------------- internals

    async def _run(self, project_id: str, stop: asyncio.Event) -> None:
        try:
            await self._drive(project_id, stop)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.exception("orchestrator crashed for %s", project_id)
            await self.emit(project_id, "error", "", f"Orchestrator crashed: {exc}")
            await self._set_status(project_id, ProjectStatus.FAILED, last_error=str(exc))

    async def _drive(self, project_id: str, stop: asyncio.Event) -> None:
        # 1. load project
        project = await self._get_project(project_id)
        if project is None:
            return

        workspace = Path(self.settings.workspaces_dir) / project_id
        workspace.mkdir(parents=True, exist_ok=True)
        if not project.workspace_path:
            await self._update(project_id, workspace_path=str(workspace.resolve()))

        plan = project.architecture or {}

        # 2. PLANNING
        if project.status in (ProjectStatus.CREATED, ProjectStatus.PLANNING):
            await self._set_status(project_id, ProjectStatus.PLANNING)
            await self.emit(project_id, "agent", "planner", "Planning project...")
            plan = await agents.run_planner(
                self.router,
                project.prompt,
                stream_callback=self._streamer(project_id, "planner"),
            )
            await self._save_plan(project_id, plan)
            await self._sync_tasks(project_id, plan)

        # 3. ARCHITECTING (merge specs back into plan)
        if project.status in (ProjectStatus.PLANNING, ProjectStatus.ARCHITECTING):
            await self._set_status(project_id, ProjectStatus.ARCHITECTING)
            await self.emit(project_id, "agent", "architect", "Designing file specs...")
            try:
                arch = await agents.run_architect(
                    self.router, plan, stream_callback=self._streamer(project_id, "architect")
                )
                plan.setdefault("specs", {})
                for f in arch.get("files", []):
                    if f.get("path"):
                        plan["specs"][f["path"]] = f.get("spec", "")
                await self._save_plan(project_id, plan)
            except Exception as exc:
                await self.emit(project_id, "warning", "architect", f"Architect skipped: {exc}")

        # 4. CODE + REVIEW loop until every task is DONE
        await self._code_review_loop(project_id, plan, workspace, stop)

        # 5. TEST / FIX loop until passing
        await self._test_fix_loop(project_id, plan, workspace, stop)

        # 6. PACKAGING
        await self._set_status(project_id, ProjectStatus.PACKAGING)
        zip_path = await self._package(project_id, workspace)
        await self._update(project_id, zip_path=str(zip_path.resolve()))

        await self._set_status(project_id, ProjectStatus.COMPLETED)
        await self.emit(
            project_id, "state", "", "Project completed", data={"zip_path": str(zip_path)}
        )

    async def _code_review_loop(
        self, project_id: str, plan: dict[str, Any], workspace: Path, stop: asyncio.Event
    ) -> None:
        max_reviews = self.cfg.orchestrator.max_review_retries

        while not stop.is_set():
            task = await self._next_pending_task(project_id)
            if task is None:
                return
            await self._set_status(project_id, ProjectStatus.CODING)
            existing = await self._collect_files(project_id)
            review_notes = task.review_notes or ""

            await self._set_task_status(task.id, TaskStatus.CODING)
            await self.emit(
                project_id, "agent", "coder",
                f"[{task.title}] coding {len(task.file_paths)} file(s)",
                data={"task_id": task.id, "files": task.file_paths},
            )
            try:
                task_dict = {
                    "id": task.id,
                    "title": task.title,
                    "description": task.description,
                    "file_paths": task.file_paths,
                }
                blocks = await agents.run_coder(
                    self.router,
                    plan=plan,
                    task=task_dict,
                    existing_files=existing,
                    review_notes=review_notes,
                    stream_callback=self._streamer(project_id, "coder"),
                )
            except Exception as exc:
                await self._bump_attempts(task.id, error=str(exc))
                await self.emit(project_id, "error", "coder", f"[{task.title}] {exc}")
                if task.attempts + 1 >= max_reviews:
                    await self._set_task_status(task.id, TaskStatus.FAILED)
                    await self._set_status(project_id, ProjectStatus.FAILED, last_error=str(exc))
                    return
                continue

            await self._write_files(project_id, workspace, blocks)
            await self._set_status(project_id, ProjectStatus.REVIEWING)
            await self._set_task_status(task.id, TaskStatus.REVIEWING)
            await self.emit(project_id, "agent", "reviewer", f"[{task.title}] reviewing...")

            try:
                review = await agents.run_reviewer(
                    self.router,
                    plan=plan,
                    task=task_dict,
                    files=blocks,
                    stream_callback=self._streamer(project_id, "reviewer"),
                )
            except Exception as exc:
                # Reviewer failing is not fatal — approve and move on.
                await self.emit(project_id, "warning", "reviewer", f"review skipped: {exc}")
                review = {"approved": True, "issues": [], "notes": "review unavailable"}

            if review.get("approved"):
                await self._set_task_status(task.id, TaskStatus.DONE)
                await self.emit(
                    project_id, "agent", "reviewer", f"[{task.title}] approved",
                    data={"notes": review.get("notes", "")},
                )
            else:
                notes = "\n".join(f"- {i}" for i in review.get("issues", []))
                await self._bump_attempts(task.id, review_notes=notes)
                # Use "warning" so the reviewer's issue list shows up in the
                # compact event log (expandable) rather than flashing through
                # the live agent row.
                await self.emit(
                    project_id, "warning", "reviewer", f"[{task.title}] rejected",
                    data={"issues": review.get("issues", [])},
                )
                if task.attempts + 1 >= max_reviews:
                    # Give up on review gate and move on so the test stage can catch it.
                    await self._set_task_status(task.id, TaskStatus.DONE)
                    await self.emit(
                        project_id, "warning", "reviewer",
                        f"[{task.title}] force-approved after {task.attempts + 1} attempts",
                    )

    async def _test_fix_loop(
        self, project_id: str, plan: dict[str, Any], workspace: Path, stop: asyncio.Event
    ) -> None:
        max_iter = self.cfg.orchestrator.max_iterations
        install_cmd = plan.get("install_command") or ""
        test_cmd = plan.get("test_command") or ""
        if not test_cmd:
            await self.emit(project_id, "warning", "tester", "No test_command in plan — skipping tests")
            return

        iteration = 0
        while not stop.is_set():
            iteration += 1
            await self._update(project_id, iteration=iteration)
            await self._set_status(project_id, ProjectStatus.TESTING)
            await self.emit(
                project_id, "test", "", f"Iteration {iteration}: running tests",
                data={"install": install_cmd, "test": test_cmd},
            )

            script_parts: list[str] = []
            if install_cmd:
                script_parts.append(install_cmd)
            script_parts.append(test_cmd)
            script = " && ".join(script_parts)

            result = await self.sandbox.run(workspace, script)
            await self._record_test(project_id, iteration, script, result)

            if result.exit_code == 0:
                await self.emit(project_id, "test", "", f"Iteration {iteration}: PASSED")
                return

            # Analyst + coder patch loop
            await self._set_status(project_id, ProjectStatus.FIXING)
            await self.emit(
                project_id, "test", "",
                f"Iteration {iteration}: FAILED (exit {result.exit_code}) — analysing",
                data={
                    "exit_code": result.exit_code,
                    "stdout": result.stdout[-4000:],
                    "stderr": result.stderr[-4000:],
                    "command": script,
                },
            )
            try:
                analysis = await agents.run_tester_analyst(
                    self.router,
                    exit_code=result.exit_code,
                    stdout=result.stdout,
                    stderr=result.stderr,
                    stream_callback=self._streamer(project_id, "tester"),
                )
            except Exception as exc:
                analysis = f"(analyst unavailable: {exc})"
            await self.emit(project_id, "agent", "tester", analysis)

            try:
                files_now = await self._collect_files(project_id)
                fix_blocks = await agents.run_fix(
                    self.router,
                    plan=plan,
                    stdout=result.stdout,
                    stderr=result.stderr,
                    analysis=analysis,
                    existing_files=files_now,
                    stream_callback=self._streamer(project_id, "coder"),
                )
                await self._write_files(project_id, workspace, fix_blocks)
                await self.emit(
                    project_id, "agent", "coder",
                    f"Iteration {iteration}: patched {len(fix_blocks)} file(s)",
                    data={"files": list(fix_blocks)},
                )
            except Exception as exc:
                await self.emit(project_id, "error", "coder", f"Patch failed: {exc}")
                await asyncio.sleep(5)

            if max_iter != -1 and iteration >= max_iter:
                await self._set_status(
                    project_id, ProjectStatus.FAILED,
                    last_error=f"max_iterations={max_iter} reached",
                )
                return

            # Heartbeat so watchers know we're still alive.
            await self._update(project_id, heartbeat_at=dt.datetime.now(dt.UTC))

    # ---------------------------------------------------------- persistence

    async def _get_project(self, project_id: str) -> Project | None:
        async with SessionLocal() as s:
            return await s.get(Project, project_id)

    async def _update(self, project_id: str, **fields: Any) -> None:
        async with SessionLocal() as s:
            proj = await s.get(Project, project_id)
            if proj is None:
                return
            for k, v in fields.items():
                setattr(proj, k, v)
            proj.updated_at = dt.datetime.now(dt.UTC)
            await s.commit()

    async def _set_status(
        self, project_id: str, status: ProjectStatus, *, last_error: str = ""
    ) -> None:
        await self._update(project_id, status=status, last_error=last_error)
        async with SessionLocal() as s:
            ev = Event(
                project_id=project_id,
                kind="state",
                role="",
                message=f"status={status.value}",
                data={"status": status.value, "last_error": last_error},
            )
            s.add(ev)
            await s.commit()
            event_id = ev.id
        await bus.publish(
            project_id,
            {
                "id": event_id,
                "kind": "state",
                "role": "",
                "message": f"status={status.value}",
                "data": {"status": status.value, "last_error": last_error},
            },
        )

    async def emit(
        self, project_id: str, kind: str, role: str, message: str, *, data: dict | None = None
    ) -> None:
        async with SessionLocal() as s:
            ev = Event(project_id=project_id, kind=kind, role=role, message=message, data=data)
            s.add(ev)
            await s.commit()
            event_id = ev.id
        await bus.publish(
            project_id,
            {
                "id": event_id,
                "kind": kind,
                "role": role,
                "message": message,
                "data": data or {},
            },
        )

    def _streamer(self, project_id: str, role: str):
        buf: list[str] = []

        async def _cb(delta: str) -> None:
            buf.append(delta)
            if len(buf) >= 8:  # batch ~every 8 tokens so the UI isn't flooded
                await bus.publish(
                    project_id,
                    {"kind": "token", "role": role, "message": "".join(buf), "data": {}},
                )
                buf.clear()

        return _cb

    async def _save_plan(self, project_id: str, plan: dict[str, Any]) -> None:
        await self._update(project_id, architecture=plan)

    async def _sync_tasks(self, project_id: str, plan: dict[str, Any]) -> None:
        tasks = plan.get("tasks") or []
        async with SessionLocal() as s:
            existing = (
                await s.execute(select(Task).where(Task.project_id == project_id))
            ).scalars().all()
            by_title = {t.title: t for t in existing}
            for idx, t in enumerate(tasks):
                title = t.get("title") or t.get("id") or f"task-{idx}"
                if title in by_title:
                    obj = by_title[title]
                    obj.description = t.get("description", obj.description)
                    obj.file_paths = t.get("file_paths", obj.file_paths)
                    obj.depends_on = t.get("depends_on", obj.depends_on)
                    obj.order_index = idx
                else:
                    s.add(
                        Task(
                            project_id=project_id,
                            order_index=idx,
                            title=title,
                            description=t.get("description", ""),
                            file_paths=t.get("file_paths", []),
                            depends_on=t.get("depends_on", []),
                        )
                    )
            await s.commit()

    async def _next_pending_task(self, project_id: str) -> Task | None:
        async with SessionLocal() as s:
            row = (
                await s.execute(
                    select(Task)
                    .where(Task.project_id == project_id)
                    .where(Task.status.in_([TaskStatus.PENDING, TaskStatus.CODING, TaskStatus.REVIEWING]))
                    .order_by(Task.order_index)
                )
            ).scalars().first()
            return row

    async def _set_task_status(self, task_id: str, status: TaskStatus) -> None:
        async with SessionLocal() as s:
            t = await s.get(Task, task_id)
            if t is None:
                return
            t.status = status
            t.updated_at = dt.datetime.now(dt.UTC)
            await s.commit()

    async def _bump_attempts(
        self, task_id: str, *, review_notes: str = "", error: str = ""
    ) -> None:
        async with SessionLocal() as s:
            t = await s.get(Task, task_id)
            if t is None:
                return
            t.attempts = (t.attempts or 0) + 1
            if review_notes:
                t.review_notes = review_notes
                t.status = TaskStatus.PENDING
            if error:
                t.last_error = error
            t.updated_at = dt.datetime.now(dt.UTC)
            await s.commit()

    async def _collect_files(self, project_id: str) -> dict[str, str]:
        async with SessionLocal() as s:
            rows = (
                await s.execute(
                    select(ProjectFile).where(ProjectFile.project_id == project_id)
                )
            ).scalars().all()
            return {f.path: f.content for f in rows}

    async def _write_files(
        self, project_id: str, workspace: Path, blocks: dict[str, str]
    ) -> None:
        async with SessionLocal() as s:
            existing = {
                f.path: f
                for f in (
                    await s.execute(
                        select(ProjectFile).where(ProjectFile.project_id == project_id)
                    )
                ).scalars().all()
            }
            for path, content in blocks.items():
                safe = _safe_relpath(path)
                if safe is None:
                    log.warning("rejecting unsafe path %r", path)
                    continue
                full = workspace / safe
                full.parent.mkdir(parents=True, exist_ok=True)
                full.write_text(content, encoding="utf-8")
                if safe in existing:
                    pf = existing[safe]
                    pf.content = content
                    pf.revision = (pf.revision or 0) + 1
                else:
                    s.add(ProjectFile(project_id=project_id, path=safe, content=content, revision=1))
            await s.commit()

    async def _record_test(
        self, project_id: str, iteration: int, command: str, result: Any
    ) -> None:
        async with SessionLocal() as s:
            s.add(
                TestRun(
                    project_id=project_id,
                    iteration=iteration,
                    command=command,
                    exit_code=result.exit_code,
                    stdout=result.stdout[-20000:],
                    stderr=result.stderr[-20000:],
                    passed=result.exit_code == 0,
                )
            )
            await s.commit()

    async def _package(self, project_id: str, workspace: Path) -> Path:
        zdir = Path(self.settings.zips_dir)
        zdir.mkdir(parents=True, exist_ok=True)
        zpath = zdir / f"{project_id}.zip"
        if zpath.exists():
            zpath.unlink()
        with zipfile.ZipFile(zpath, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for root in workspace.rglob("*"):
                if root.is_file():
                    arc = root.relative_to(workspace)
                    zf.write(root, arcname=str(arc))
        return zpath


def _safe_relpath(raw: str) -> str | None:
    """Reject absolute paths or paths that escape the workspace."""
    p = raw.strip().lstrip("/")
    if not p or ".." in Path(p).parts:
        return None
    return p


_orch: Orchestrator | None = None


def get_orchestrator() -> Orchestrator:
    global _orch
    if _orch is None:
        _orch = Orchestrator()
    return _orch


async def shutdown_workspaces() -> None:
    """Best-effort cleanup of scratch directories older than 7 days."""
    root = Path(get_settings().workspaces_dir)
    if not root.exists():
        return
    cutoff = dt.datetime.now(dt.UTC).timestamp() - 7 * 24 * 3600
    for child in root.iterdir():
        try:
            if child.stat().st_mtime < cutoff:
                shutil.rmtree(child, ignore_errors=True)
        except OSError:
            pass
