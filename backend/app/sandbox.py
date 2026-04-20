"""Sandboxed command execution.

Two backends:

* ``docker`` (default, recommended): runs each command in a throwaway container
  that only sees the project workspace. No network by default.
* ``local``: runs the command directly on the host. Only use this on a VM
  that is already isolated.

Both backends return the same :class:`RunResult` tuple.
"""

from __future__ import annotations

import asyncio
import logging
import shlex
import shutil
from dataclasses import dataclass
from pathlib import Path

from .config import OrchestratorConfig, get_config

log = logging.getLogger(__name__)


@dataclass
class RunResult:
    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool = False


class Sandbox:
    def __init__(self, cfg: OrchestratorConfig | None = None) -> None:
        self.cfg = cfg or get_config().orchestrator

    async def run(self, workspace: Path, command: str) -> RunResult:
        if self.cfg.sandbox == "local":
            return await self._run_local(workspace, command)
        return await self._run_docker(workspace, command)

    async def _run_local(self, workspace: Path, command: str) -> RunResult:
        # IMPORTANT: use ``export`` rather than the one-shot ``VAR=val cmd``
        # form. Our test command is typically ``pip install X && pytest``;
        # with a one-shot prefix, PYTHONPATH would only apply to ``pip``
        # and pytest would then fail with ``ModuleNotFoundError`` on the
        # project's own modules.
        ws = shlex.quote(str(workspace.resolve()))
        full = f"export PYTHONPATH={ws}:${{PYTHONPATH:-}}; {command}"
        return await _exec_shell(
            full, cwd=workspace, timeout=self.cfg.sandbox_timeout
        )

    async def _run_docker(self, workspace: Path, command: str) -> RunResult:
        if shutil.which("docker") is None:
            log.warning("docker not available, falling back to local execution")
            return await self._run_local(workspace, command)

        workspace = workspace.resolve()
        workspace.mkdir(parents=True, exist_ok=True)
        image = self.cfg.sandbox_image
        # ``--network host`` so pip/npm can install deps, but filesystem stays
        # scoped to the project via the single volume mount. If your threat
        # model requires fully offline sandbox, swap for ``--network none`` and
        # pre-bake the image with deps.
        docker_cmd = [
            "docker", "run", "--rm",
            "--network", "host",
            "--workdir", "/work",
            "-v", f"{workspace}:/work",
            "-e", "PIP_DISABLE_PIP_VERSION_CHECK=1",
            "-e", "PYTHONDONTWRITEBYTECODE=1",
            # Prepend the workspace to PYTHONPATH so pytest can resolve modules
            # living at the repo root without requiring every generated project
            # to ship a pyproject.toml / conftest.py just for this.
            "-e", "PYTHONPATH=/work",
            image,
            "bash", "-lc", command,
        ]
        return await _exec_argv(docker_cmd, timeout=self.cfg.sandbox_timeout)


async def _exec_shell(cmd: str, *, cwd: Path, timeout: int) -> RunResult:
    proc = await asyncio.create_subprocess_shell(
        cmd,
        cwd=str(cwd),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    return await _collect(proc, timeout=timeout, label=cmd)


async def _exec_argv(argv: list[str], *, timeout: int) -> RunResult:
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    return await _collect(proc, timeout=timeout, label=shlex.join(argv))


async def _collect(proc: asyncio.subprocess.Process, *, timeout: int, label: str) -> RunResult:
    try:
        stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        log.warning("command timed out after %ds: %s", timeout, label)
        return RunResult(exit_code=-1, stdout="", stderr=f"timeout after {timeout}s", timed_out=True)
    return RunResult(
        exit_code=proc.returncode if proc.returncode is not None else -1,
        stdout=stdout_b.decode("utf-8", errors="replace"),
        stderr=stderr_b.decode("utf-8", errors="replace"),
    )
