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
import re
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
        heartbeat = asyncio.create_task(self._heartbeat_loop(project_id, stop))
        budget = asyncio.create_task(self._budget_loop(project_id, stop))
        try:
            await self._drive(project_id, stop)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.exception("orchestrator crashed for %s", project_id)
            await self.emit(project_id, "error", "", f"Orchestrator crashed: {exc}")
            await self._set_status(project_id, ProjectStatus.FAILED, last_error=str(exc))
        finally:
            for t in (heartbeat, budget):
                t.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await t

    async def _heartbeat_loop(self, project_id: str, stop: asyncio.Event) -> None:
        """Periodically update ``heartbeat_at`` so long-running projects
        (potentially days) are obviously alive in the UI. Doubles as a
        checkpoint marker: a crash-restart can see when we last wrote."""
        interval = max(5, self.cfg.orchestrator.heartbeat_interval)
        try:
            while not stop.is_set():
                await self._update(project_id, heartbeat_at=dt.datetime.now(dt.UTC))
                await asyncio.sleep(interval)
        except asyncio.CancelledError:
            return

    async def _budget_loop(self, project_id: str, stop: asyncio.Event) -> None:
        """Enforce ``max_wall_seconds`` if configured (>0).

        Projects are allowed to iterate as long as needed by default
        (``max_wall_seconds=-1``). With a budget set, the worker is asked
        to stop cleanly once the cumulative wall-clock time exceeds it.
        """
        budget = self.cfg.orchestrator.max_wall_seconds
        if budget <= 0:
            return
        project = await self._get_project(project_id)
        if project is None:
            return
        started = project.created_at or dt.datetime.now(dt.UTC)
        deadline = started + dt.timedelta(seconds=budget)
        try:
            while not stop.is_set():
                now = dt.datetime.now(dt.UTC)
                if now >= deadline:
                    await self.emit(
                        project_id, "warning", "",
                        f"Wall-clock budget ({budget}s) exceeded; stopping.",
                    )
                    stop.set()
                    await self._set_status(
                        project_id, ProjectStatus.FAILED,
                        last_error=f"budget_exceeded after {budget}s",
                    )
                    return
                await asyncio.sleep(min(30, (deadline - now).total_seconds()))
        except asyncio.CancelledError:
            return

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
        """Dispatch pending tasks to up to ``parallel_coders`` workers.

        A task is "ready" when every entry in its ``depends_on`` list is
        resolved (either matches the title/id of a DONE task, or doesn't
        match any known task at all — the latter treated permissively so a
        weakly-typed planner dep doesn't block forever).
        """
        parallelism = max(1, self.cfg.orchestrator.parallel_coders)
        in_flight: dict[str, asyncio.Task[None]] = {}
        await self._set_status(project_id, ProjectStatus.CODING)

        while not stop.is_set():
            # Fill worker slots with any ready tasks.
            while len(in_flight) < parallelism:
                ready = await self._next_ready_task(
                    project_id, skip_ids=set(in_flight.keys())
                )
                if ready is None:
                    break
                await self._set_task_status(ready.id, TaskStatus.CODING)
                worker = asyncio.create_task(
                    self._run_one_task(project_id, ready, plan, workspace, stop),
                    name=f"task-{ready.id[:8]}",
                )
                in_flight[ready.id] = worker

            if not in_flight:
                # Nothing in flight and nothing ready. Either we're done, or
                # some tasks are blocked on a dep that's still pending but
                # no worker picked it up (shouldn't happen). Exit.
                return

            done, _pending = await asyncio.wait(
                in_flight.values(), return_when=asyncio.FIRST_COMPLETED
            )
            # Remove finished workers and surface fatal errors.
            for task in done:
                for tid, wt in list(in_flight.items()):
                    if wt is task:
                        del in_flight[tid]
                        break
                if task.cancelled():
                    continue
                exc = task.exception()
                if exc is not None:
                    # Propagate: cancel siblings and bubble up.
                    for other in in_flight.values():
                        other.cancel()
                    raise exc

    async def _run_one_task(
        self,
        project_id: str,
        task: Task,
        plan: dict[str, Any],
        workspace: Path,
        stop: asyncio.Event,
    ) -> None:
        """Code → write → review cycle for a single task.

        Runs concurrently with sibling tasks. Each worker holds its own
        reference to the Task it started with, but always reads/writes the
        canonical row from SQLite so attempts and status are consistent.
        """
        max_reviews = self.cfg.orchestrator.max_review_retries
        existing = await self._collect_files(project_id)
        review_notes = task.review_notes or ""

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
            coder_role = self._coder_role_for_files(task.file_paths)
            blocks = await agents.run_coder(
                self.router,
                plan=plan,
                task=task_dict,
                existing_files=existing,
                review_notes=review_notes,
                role=coder_role,
                stream_callback=self._coder_streamer(project_id, "coder"),
            )
        except Exception as exc:
            await self._bump_attempts(task.id, error=str(exc))
            await self.emit(project_id, "error", "coder", f"[{task.title}] {exc}")
            if task.attempts + 1 >= max_reviews:
                await self._set_task_status(task.id, TaskStatus.FAILED)
            return

        await self._write_files(project_id, workspace, blocks)
        await self._set_task_status(task.id, TaskStatus.REVIEWING)

        # In-loop static check: compile() + ruff on the files we just
        # wrote. If anything fails we give the FIXER one immediate pass
        # (temperature 0, narrow prompt) to patch the issues, then
        # re-check. This avoids paying for a 30s LLM review round-trip
        # just to be told "you have a NameError on line 12".
        static_issues, bad_paths = await self._static_check_python_files(
            workspace, list(blocks.keys())
        )
        if static_issues:
            await self.emit(
                project_id, "warning", "fixer",
                f"[{task.title}] static check failed "
                f"({len(static_issues)} issue(s)) — running in-loop fix",
                data={"issues": static_issues, "files": sorted(bad_paths)},
            )
            try:
                fix_existing = await self._collect_files(project_id)
                # Restrict to siblings of the failing files to keep the
                # prompt tight.
                relevant_paths = set(blocks.keys()) | bad_paths
                fix_existing = {
                    p: c for p, c in fix_existing.items()
                    if p in relevant_paths
                }
                fixed = await agents.run_static_fix(
                    self.router,
                    plan=plan,
                    task=task_dict,
                    issues=static_issues,
                    existing_files=fix_existing,
                    stream_callback=self._coder_streamer(project_id, "fixer"),
                )
            except Exception as exc:
                fixed = {}
                await self.emit(
                    project_id, "warning", "fixer",
                    f"[{task.title}] in-loop fix skipped: {exc}",
                )

            if fixed:
                await self._write_files(project_id, workspace, fixed)
                # Re-check on the union of originally-written files and
                # files the fixer actually touched.
                recheck_paths = list(set(blocks.keys()) | set(fixed.keys()))
                static_issues, bad_paths = (
                    await self._static_check_python_files(
                        workspace, recheck_paths
                    )
                )

        if static_issues:
            for _rpath in blocks:
                if _rpath in bad_paths:
                    await self.emit(
                        project_id, "file_review_end", "reviewer", _rpath,
                        data={"path": _rpath, "approved": False},
                    )
                else:
                    # File was fine — don't mark it rejected just because a
                    # sibling in the same task failed the check.
                    await self.emit(
                        project_id, "file_review_end", "reviewer", _rpath,
                        data={"path": _rpath, "approved": True},
                    )
            notes = (
                "Static checks still fail after in-loop fix:\n"
                + "\n".join(f"- {i}" for i in static_issues[:20])
            )
            await self._bump_attempts(task.id, review_notes=notes)
            await self.emit(
                project_id, "warning", "reviewer",
                f"[{task.title}] static check rejected "
                f"({len(static_issues)} issue(s))",
                data={"issues": static_issues},
            )
            if task.attempts + 1 >= max_reviews:
                await self._set_task_status(task.id, TaskStatus.DONE)
                await self.emit(
                    project_id, "warning", "reviewer",
                    f"[{task.title}] force-approved after static-check loop",
                )
            return

        await self.emit(project_id, "agent", "reviewer", f"[{task.title}] reviewing...")
        for _rpath in blocks:
            await self.emit(
                project_id, "file_review_start", "reviewer", _rpath,
                data={"path": _rpath},
            )

        try:
            review = await agents.run_reviewer(
                self.router,
                plan=plan,
                task=task_dict,
                files=blocks,
                stream_callback=self._streamer(project_id, "reviewer"),
            )
        except Exception as exc:
            await self.emit(project_id, "warning", "reviewer", f"review skipped: {exc}")
            review = {"approved": True, "issues": [], "notes": "review unavailable"}

        issues_text = " ".join(review.get("issues", []))
        approved_overall = bool(review.get("approved"))
        for _rpath in blocks:
            file_ok = approved_overall and (_rpath not in issues_text)
            await self.emit(
                project_id, "file_review_end", "reviewer", _rpath,
                data={"path": _rpath, "approved": file_ok},
            )

        if approved_overall:
            await self._set_task_status(task.id, TaskStatus.DONE)
            await self.emit(
                project_id, "agent", "reviewer", f"[{task.title}] approved",
                data={"notes": review.get("notes", "")},
            )
            return

        notes = "\n".join(f"- {i}" for i in review.get("issues", []))
        await self._bump_attempts(task.id, review_notes=notes)
        await self.emit(
            project_id, "warning", "reviewer", f"[{task.title}] rejected",
            data={"issues": review.get("issues", [])},
        )
        if task.attempts + 1 >= max_reviews:
            await self._set_task_status(task.id, TaskStatus.DONE)
            await self.emit(
                project_id, "warning", "reviewer",
                f"[{task.title}] force-approved after {task.attempts + 1} attempts",
            )

    async def _gate_all_files_exist(
        self, project_id: str, plan: dict[str, Any], workspace: Path, stop: asyncio.Event
    ) -> bool:
        """Ensure every file listed in plan["files"] actually exists on disk.

        If some are missing, ask the coder to produce just the missing ones
        (up to 3 rounds). Returns True if all files are present when we exit,
        False if we gave up.
        """
        planned = [
            f.get("path")
            for f in plan.get("files", [])
            if isinstance(f, dict) and f.get("path")
        ]
        if not planned:
            return True

        for attempt in range(3):
            if stop.is_set():
                return False
            missing = [p for p in planned if not (workspace / p).exists()]
            if not missing:
                return True

            await self._set_status(project_id, ProjectStatus.CODING)
            await self.emit(
                project_id, "warning", "coder",
                f"Gate: {len(missing)} planned file(s) missing — filling before tests",
                data={"files": missing},
            )
            synthetic_task = {
                "id": f"gate-fill-{attempt}",
                "title": "Fill missing planned files",
                "description": (
                    "The test gate found that these files listed in the plan do "
                    "not exist yet. Produce them now with minimal but complete "
                    "content consistent with the rest of the project."
                ),
                "file_paths": missing,
            }
            try:
                existing = await self._collect_files(project_id)
                blocks = await agents.run_coder(
                    self.router,
                    plan=plan,
                    task=synthetic_task,
                    existing_files=existing,
                    stream_callback=self._coder_streamer(project_id, "coder"),
                )
                await self._write_files(project_id, workspace, blocks)
            except Exception as exc:
                await self.emit(project_id, "error", "coder", f"Gate fill failed: {exc}")
                await asyncio.sleep(3)

        remaining = [p for p in planned if not (workspace / p).exists()]
        if remaining:
            await self.emit(
                project_id, "warning", "coder",
                f"Gate: proceeding despite {len(remaining)} missing file(s)",
                data={"files": remaining},
            )
        return True

    async def _test_fix_loop(
        self, project_id: str, plan: dict[str, Any], workspace: Path, stop: asyncio.Event
    ) -> None:
        max_iter = self.cfg.orchestrator.max_iterations
        install_cmd = plan.get("install_command") or ""
        test_cmd = plan.get("test_command") or ""
        if not test_cmd:
            await self.emit(project_id, "warning", "tester", "No test_command in plan — skipping tests")
            return

        # Gate: every file listed in the plan must exist on disk before we
        # bother running pytest. Otherwise tests fail for the wrong reason
        # (imports of files that were never written) and the fixer wastes
        # iterations on phantom bugs.
        if not await self._gate_all_files_exist(project_id, plan, workspace, stop):
            return

        # Absolute safety cap, independent of max_iterations. max_iterations
        # may be -1 (unlimited) but the test/fix loop is still bounded: if
        # we haven't converged after this many attempts, something is
        # structurally wrong (wrong test command, impossible spec, missing
        # dependency) and no amount of retrying will help.
        HARD_CAP = 25
        # Stop after this many consecutive iterations with no actual file
        # change from the fixer. This catches the "fixer keeps producing
        # empty blocks / identical output" infinite-loop scenario.
        NO_PROGRESS_LIMIT = 3
        # Minimum wall-clock time per iteration. Prevents CPU-spinning when
        # the test command fails instantly and the fixer also errors out
        # (caught exception path). 10s is enough to notice in the UI.
        MIN_ITERATION_SECONDS = 10.0

        iteration = 0
        no_progress = 0
        last_stderr_sig: str | None = None
        while not stop.is_set():
            iteration_start = asyncio.get_event_loop().time()
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

            # pytest exit code 5 means "no tests collected". For our purposes
            # that is not a failure — it means the project has no tests yet
            # (or the test_command was a bare-import sanity check that ran
            # clean). Treat it as PASS so we can package and deliver.
            noop_pass = (
                result.exit_code == 5
                and "pytest" in script
                and ("no tests ran" in (result.stdout or "").lower()
                     or "no tests ran" in (result.stderr or "").lower()
                     or "collected 0 items" in (result.stdout or "").lower())
            )
            if result.exit_code == 0 or noop_pass:
                msg = (
                    f"Iteration {iteration}: PASSED"
                    if not noop_pass
                    else f"Iteration {iteration}: PASSED (no tests collected)"
                )
                await self.emit(project_id, "test", "", msg)
                return

            # Signature of this failure (last 500 chars of stderr). Used to
            # detect "same error as last iteration" even if the fixer did
            # touch files but didn't actually fix anything.
            stderr_sig = (result.stderr or "")[-500:].strip()
            same_error_as_before = (
                last_stderr_sig is not None and stderr_sig == last_stderr_sig
            )

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

            files_before = await self._collect_files(project_id)
            fix_blocks: dict[str, str] = {}
            try:
                fix_blocks = await agents.run_fix(
                    self.router,
                    plan=plan,
                    stdout=result.stdout,
                    stderr=result.stderr,
                    analysis=analysis,
                    existing_files=files_before,
                    stream_callback=self._coder_streamer(project_id, "fixer"),
                )
                await self._write_files(project_id, workspace, fix_blocks)
                await self.emit(
                    project_id, "agent", "coder",
                    f"Iteration {iteration}: patched {len(fix_blocks)} file(s)",
                    data={"files": list(fix_blocks)},
                )
            except Exception as exc:
                await self.emit(
                    project_id, "error", "coder", f"Patch failed: {exc}",
                )

            # Did the fixer actually change anything on disk?
            actually_changed = any(
                files_before.get(p) != c for p, c in fix_blocks.items()
            )

            if not actually_changed or same_error_as_before:
                no_progress += 1
                await self.emit(
                    project_id, "warning", "fixer",
                    f"Iteration {iteration}: no progress "
                    f"({no_progress}/{NO_PROGRESS_LIMIT}) — "
                    + (
                        "fixer returned no file changes"
                        if not actually_changed
                        else "same stderr as previous iteration"
                    ),
                )
            else:
                no_progress = 0
            last_stderr_sig = stderr_sig

            # Bail conditions, in order of priority.
            if no_progress >= NO_PROGRESS_LIMIT:
                await self._set_status(
                    project_id, ProjectStatus.FAILED,
                    last_error=(
                        f"test/fix loop stalled: {no_progress} iterations "
                        f"without progress. Last stderr:\n{stderr_sig}"
                    ),
                )
                return
            if iteration >= HARD_CAP:
                await self._set_status(
                    project_id, ProjectStatus.FAILED,
                    last_error=(
                        f"test/fix loop hit hard cap ({HARD_CAP} iterations) "
                        f"without passing tests. Last stderr:\n{stderr_sig}"
                    ),
                )
                return
            if max_iter != -1 and iteration >= max_iter:
                await self._set_status(
                    project_id, ProjectStatus.FAILED,
                    last_error=f"max_iterations={max_iter} reached",
                )
                return

            # Heartbeat so watchers know we're still alive.
            await self._update(project_id, heartbeat_at=dt.datetime.now(dt.UTC))

            # Minimum iteration duration: if everything failed fast (e.g.
            # analyst threw, fixer threw, test command instantly returned
            # exit 1) we'd otherwise spin at CPU speed. Sleep the remainder
            # so the UI stays legible and we don't burn the CPU.
            elapsed = asyncio.get_event_loop().time() - iteration_start
            if elapsed < MIN_ITERATION_SECONDS:
                await asyncio.sleep(MIN_ITERATION_SECONDS - elapsed)

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
        """Generic token streamer: batches deltas so the UI isn't flooded."""
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

    def _coder_streamer(self, project_id: str, role: str):
        """Streamer for the coder/fixer that ALSO detects `path=...` fenced
        file boundaries in the token flow and emits structured file events:

        - ``file_start``  when a new ``\u0060\u0060\u0060path=<path>`` header appears,
        - ``file_chunk``  with each delta inside a file body,
        - ``file_end``    when the matching closing fence is seen.

        The UI uses these to render a live "file being written" panel next to
        the graph, so you visibly see each file filling in one after the other.
        """
        # Rolling buffer of all text we've seen so we can locate headers even
        # if a token straddles a boundary.
        seen: list[str] = []
        token_buf: list[str] = []
        in_file = False
        current_path: str | None = None
        pending_body = ""  # bytes emitted for the current file (for trimming fence)
        header_re = re.compile(
            r"```(?:[a-zA-Z0-9_+\-]+)?\s*path\s*[:=]\s*([^\n`]+)\n", re.DOTALL
        )

        async def flush_tokens() -> None:
            if not token_buf:
                return
            await bus.publish(
                project_id,
                {"kind": "token", "role": role, "message": "".join(token_buf), "data": {}},
            )
            token_buf.clear()

        async def _cb(delta: str) -> None:
            nonlocal in_file, current_path, pending_body
            seen.append(delta)
            token_buf.append(delta)
            if len(token_buf) >= 8:
                await flush_tokens()

            # Process the full seen buffer for boundary transitions. We only
            # scan the last ~2KB window to keep it cheap.
            text = "".join(seen)
            # Detect file_start: find a header we haven't handled yet.
            if not in_file:
                m = header_re.search(text)
                if m:
                    path = m.group(1).strip().strip("\"'`")
                    if path and ("/" in path or "." in path):
                        current_path = path
                        in_file = True
                        pending_body = text[m.end():]
                        # Drop everything up to and including the header so
                        # the next search starts at the new file body.
                        seen.clear()
                        seen.append(pending_body)
                        await flush_tokens()
                        await bus.publish(
                            project_id,
                            {
                                "kind": "file_start",
                                "role": role,
                                "message": path,
                                "data": {"path": path},
                            },
                        )
                        # Stream the initial body chunk we already have.
                        if pending_body:
                            await bus.publish(
                                project_id,
                                {
                                    "kind": "file_chunk",
                                    "role": role,
                                    "message": "",
                                    "data": {"path": path, "delta": pending_body},
                                },
                            )
                return

            # We are inside a file body: stream chunks until we hit a closing
            # fence ``` on its own (end of file).
            # The delta is the new bytes; append to pending_body and check.
            pending_body += delta
            close_idx = pending_body.find("\n```")
            if close_idx == -1:
                # Still writing the body: emit the delta as a file_chunk.
                await bus.publish(
                    project_id,
                    {
                        "kind": "file_chunk",
                        "role": role,
                        "message": "",
                        "data": {"path": current_path, "delta": delta},
                    },
                )
                return

            # Found the closing fence. Emit the tail up to the fence, then
            # file_end, then reset state.
            tail = pending_body[: close_idx]
            # We've been streaming deltas live, but the LAST delta contained
            # the closing fence; send only the part of that delta up to the
            # fence so the UI doesn't include "```" in the file body.
            trim = len(delta) - (len(pending_body) - close_idx)
            if trim > 0:
                await bus.publish(
                    project_id,
                    {
                        "kind": "file_chunk",
                        "role": role,
                        "message": "",
                        "data": {"path": current_path, "delta": delta[:trim]},
                    },
                )
            await bus.publish(
                project_id,
                {
                    "kind": "file_end",
                    "role": role,
                    "message": current_path or "",
                    "data": {"path": current_path},
                },
            )
            # Reset for the next file.
            remaining = pending_body[close_idx + 4:]  # skip "\n```"
            in_file = False
            current_path = None
            pending_body = ""
            seen.clear()
            seen.append(remaining)
            _ = tail  # acknowledged

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

    def _coder_role_for_files(self, paths: list[str]) -> str:
        """Pick the most specialised coder role available for ``paths``.

        Looks at file extensions and returns a role like ``coder_py`` or
        ``coder_web``. Falls back to the generic ``coder`` role when the
        specialised role isn't configured, or when the task mixes
        languages.
        """
        if not paths:
            return "coder"
        groups = {
            "coder_py": {".py", ".pyi"},
            "coder_web": {".js", ".jsx", ".ts", ".tsx", ".html", ".css", ".vue", ".svelte"},
            "coder_rust": {".rs"},
            "coder_go": {".go"},
        }
        matched: set[str] = set()
        for p in paths:
            ext = "." + p.rsplit(".", 1)[-1].lower() if "." in p else ""
            for role, exts in groups.items():
                if ext in exts:
                    matched.add(role)
                    break
        # Exactly one recognised language → use its specialised role
        # (if the role is actually configured).
        if len(matched) == 1:
            role = next(iter(matched))
            if role in self.router.cfg.roles:
                return role
        return "coder"

    async def _static_check_python_files(
        self, workspace: Path, paths: list[str]
    ) -> tuple[list[str], set[str]]:
        """Run local static checks on Python files and return (issues, bad_paths).

        Two passes, in order:

        1. ``compile()`` — Python's own parser. Catches SyntaxError and
           IndentationError even when ruff isn't installed. This is the
           single most valuable check: it's what matters for the code to
           be importable.
        2. ``ruff check`` with a narrow selector (E9/F63/F7/F82/F821).
           Runs only on files that passed the compile step, so we don't
           drown a syntax error in extra noise.

        ``ruff`` is not strictly required — if it's missing we still
        report compile errors.
        """
        py_files = [p for p in paths if p.endswith(".py")]
        if not py_files:
            return [], set()

        issues: list[str] = []
        bad: set[str] = set()

        # Pass 1 — compile() on every file.
        compile_ok: list[str] = []
        for rel in py_files:
            abs_path = workspace / rel
            if not abs_path.exists():
                continue
            try:
                source = abs_path.read_text(encoding="utf-8", errors="replace")
            except OSError as exc:
                issues.append(f"{rel}:1:1: IOError reading file: {exc}")
                bad.add(rel)
                continue
            try:
                compile(source, rel, "exec")
                compile_ok.append(rel)
            except SyntaxError as exc:
                line = exc.lineno or 1
                col = exc.offset or 1
                msg = exc.msg or "syntax error"
                issues.append(f"{rel}:{line}:{col}: SyntaxError: {msg}")
                bad.add(rel)

        # Pass 2 — ruff on files that compile cleanly.
        if compile_ok:
            rel_by_abs = {
                str((workspace / p).resolve()): p for p in compile_ok
            }
            selected = "E9,F63,F7,F82,F821"
            cmd = [
                "ruff", "check",
                "--select", selected,
                "--output-format", "concise",
                "--no-fix",
                *rel_by_abs.keys(),
            ]
            try:
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                stdout, _ = await asyncio.wait_for(
                    proc.communicate(), timeout=30
                )
            except (FileNotFoundError, TimeoutError):
                return issues, bad
            if proc.returncode != 0:
                for line in stdout.decode(errors="replace").splitlines():
                    line = line.strip()
                    if (
                        not line
                        or line.startswith("Found ")
                        or line.startswith("[*]")
                    ):
                        continue
                    for abs_path, rel_path in rel_by_abs.items():
                        if line.startswith(abs_path):
                            bad.add(rel_path)
                            line = rel_path + line[len(abs_path):]
                            break
                    issues.append(line)
        return issues, bad

    # Kept as an alias so any downstream caller doesn't break.
    _lint_python_files = _static_check_python_files

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

    async def _next_ready_task(
        self, project_id: str, skip_ids: set[str] | None = None
    ) -> Task | None:
        """Find the next PENDING task whose dependencies are satisfied.

        ``Task.depends_on`` entries may be planner-style short ids ("t0"),
        titles, or DB uuids. We resolve by title or id; any dep that matches
        no known task is treated as satisfied (permissive) so a noisy planner
        doesn't deadlock the pipeline.
        """
        skip_ids = skip_ids or set()
        async with SessionLocal() as s:
            rows = (
                await s.execute(
                    select(Task)
                    .where(Task.project_id == project_id)
                    .order_by(Task.order_index)
                )
            ).scalars().all()

        done_keys: set[str] = set()
        all_keys: set[str] = set()
        for t in rows:
            all_keys.add(t.title)
            all_keys.add(t.id)
            if t.status == TaskStatus.DONE:
                done_keys.add(t.title)
                done_keys.add(t.id)

        for t in rows:
            if t.id in skip_ids:
                continue
            if t.status != TaskStatus.PENDING:
                continue
            blocked = False
            for dep in t.depends_on or []:
                if dep in all_keys and dep not in done_keys:
                    blocked = True
                    break
            if not blocked:
                return t
        return None

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
