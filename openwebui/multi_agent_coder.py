"""
title: Multi-Agent Coder
author: Johnny
author_url: https://github.com/amavaljoh04-lang
git_url: https://github.com/amavaljoh04-lang/multi-agent-coder.git
description: Pipeline multi-agent autonome pour Open-WebUI. Donne un prompt, il decompose en projet, code, teste en boucle dans un sandbox, et livre un ZIP. Affichage riche en temps reel dans le chat.
required_open_webui_version: 0.4.0
requirements: httpx
version: 1.0.0
licence: MIT
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
import shutil
import tempfile
import traceback
import zipfile
from io import BytesIO
from pathlib import Path
from typing import Any, Callable, Optional

from pydantic import BaseModel, Field

log = logging.getLogger("multi_agent_coder")

# ---------------------------------------------------------------------------
# JSON / code-block extraction (same battle-tested logic as the backend)
# ---------------------------------------------------------------------------

_JSON_BLOCK = re.compile(r"```(?:json)?\s*(\{.*?\}|\[.*?\])\s*```", re.DOTALL)
_FIRST_BRACE = re.compile(r"(\{.*\}|\[.*\])", re.DOTALL)
_THINK_BLOCK = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)


def _strip_reasoning(text: str) -> str:
    return _THINK_BLOCK.sub("", text).strip()


def _extract_json(text: str) -> Any:
    cleaned = _strip_reasoning(text)
    m = _JSON_BLOCK.search(cleaned)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass
    m2 = _FIRST_BRACE.search(cleaned)
    if m2:
        snippet = m2.group(1)
        try:
            return json.loads(snippet)
        except json.JSONDecodeError:
            fixed = re.sub(r",(\s*[}\]])", r"\1", snippet)
            return json.loads(fixed)
    raise ValueError("No JSON payload found in model response")


def _extract_code_blocks(text: str) -> dict[str, str]:
    blocks: dict[str, str] = {}
    pattern = re.compile(
        r"```(?:[a-zA-Z0-9_+\-]+)?\s*path\s*[:=]\s*([^\n`]+)\n(.*?)```",
        re.DOTALL,
    )
    for hdr, body in pattern.findall(text):
        path = hdr.strip().strip("\"'`")
        if not path or ("/" not in path and "." not in path):
            continue
        blocks[path] = body.rstrip() + "\n"
    return blocks


# ---------------------------------------------------------------------------
# System prompts for each agent role
# ---------------------------------------------------------------------------

PLAN_SYSTEM = (
    "You are a senior software architect. "
    "You decompose user project requests into a concrete set of files and "
    "atomic implementation tasks that together deliver EVERY requirement. "
    "Output ONLY valid JSON, no prose, no markdown fences leaking outside."
)

CODE_SYSTEM = (
    "You are a senior software engineer. "
    "You write complete, production-quality files. "
    "Never output partial code or placeholders like '...' or 'TODO'. "
    "Every file MUST compile/run as-is."
)

REVIEW_SYSTEM = (
    "You are a ruthless senior code reviewer. "
    "You only approve code that runs, handles errors, and matches the spec. "
    "Be terse. Output JSON only."
)

FIX_SYSTEM = (
    "You are a senior fixer. Apply the SMALLEST possible change to make "
    "failing tests pass. Do NOT rewrite from scratch."
)

ANALYST_SYSTEM = (
    "You are a test-failure analyst. Read the stacktrace and identify the "
    "root cause. Output JSON with fields: root_cause, files_to_fix, fix_description."
)

# ---------------------------------------------------------------------------
# Prompt templates
# ---------------------------------------------------------------------------

PLAN_PROMPT = """\
User project request:
---
{prompt}
---

Produce a JSON object with this exact schema:
{{
  "name": "short project name, slug-friendly",
  "summary": "one paragraph describing what will be built",
  "language": "primary language (python, node, go, rust, ...)",
  "run_command": "shell command to run the app",
  "test_command": "shell command to run tests (pytest, npm test, ...)",
  "install_command": "shell command to install deps",
  "files": [
     {{"path": "relative/path.ext", "purpose": "what this file does"}}
  ],
  "tasks": [
     {{
       "id": "t1",
       "title": "short title",
       "description": "precise specification",
       "file_paths": ["relative/path.ext"],
       "depends_on": []
     }}
  ]
}}

Rules:
- Cover EVERY requirement from the user prompt.
- "files" MUST list EVERY file the project needs (source, tests, requirements.txt, README.md).
- Tasks form a DAG. Order dependencies first.
- Keep it minimal and runnable. Aim for <= 25 files.
- Always include README.md and a dependency manifest (requirements.txt etc).
- Output ONLY the JSON object, nothing else.
"""

CODE_PROMPT = """\
Project context (JSON):
---
{plan}
---

Current task:
Title: {title}
Description:
{description}

Files to produce (write EVERY one, fully, no placeholders):
{file_list}

{existing_files_block}

Output format -- one fenced block per file, header MUST be `path=<relative path>`:

```path=relative/path.ext
<full file content>
```

Rules:
- Output EVERY listed file, even if small.
- No prose outside fenced blocks.
- No "..." or "TODO". Code must be complete and runnable.
"""

FIX_PROMPT = """\
Project context (JSON):
---
{plan}
---

Test command failed. Output:

STDOUT:
{stdout}

STDERR:
{stderr}

Analyst notes:
{analysis}

Current files that likely need changes:
{existing_files_block}

Output format -- one fenced block per file you modify:

```path=relative/path.ext
<full updated file content>
```

Rules:
1. Touch FEWEST possible files.
2. Preserve everything unrelated to the failure.
3. Do NOT rewrite from scratch -- change only broken lines.
4. No prose outside fenced blocks. Complete files only.
"""

REVIEW_PROMPT = """\
Project plan:
---
{plan}
---

Files produced:
{files_block}

Review all files. Output JSON:
{{
  "approved": true/false,
  "issues": [
    {{"file": "path", "line": 0, "severity": "error|warning", "message": "description"}}
  ]
}}

Only reject if there are actual errors that will prevent the code from running or
failing tests. Style issues are warnings, not rejections.
"""

ANALYST_PROMPT = """\
Test command: {test_command}

STDOUT:
{stdout}

STDERR:
{stderr}

Current files:
{files_block}

Identify the root cause. Output JSON:
{{
  "root_cause": "description of what went wrong",
  "files_to_fix": ["file1.py", "file2.py"],
  "fix_description": "what needs to change"
}}
"""


# ===================================================================
# PIPE FUNCTION -- importable as-is into Open-WebUI
# ===================================================================


class Pipe:
    """Multi-Agent Coder Pipeline for Open-WebUI.

    Appears as a selectable model. Send it a project description and it will:
    1. Plan the project (tasks, files, dependencies)
    2. Architect the structure
    3. Code every file
    4. Review for quality
    5. Test in a sandbox (subprocess)
    6. Fix in a loop until all tests pass
    7. Deliver a downloadable ZIP
    """

    class Valves(BaseModel):
        OLLAMA_BASE_URL: str = Field(
            default="http://localhost:11434",
            description="URL de base de ton serveur Ollama (ex: http://192.168.0.224:11434)",
        )
        PLANNER_MODEL: str = Field(
            default="qwen2.5-coder:7b",
            description="Modele pour le planning (decomposition du projet)",
        )
        ARCHITECT_MODEL: str = Field(
            default="qwen2.5-coder:7b",
            description="Modele pour l'architecture (specs par fichier)",
        )
        CODER_MODEL: str = Field(
            default="qwen2.5-coder:14b",
            description="Modele principal pour coder (le plus intelligent)",
        )
        REVIEWER_MODEL: str = Field(
            default="deepseek-coder:6.7b",
            description="Modele pour la review de code",
        )
        ANALYST_MODEL: str = Field(
            default="deepseek-coder:6.7b",
            description="Modele pour analyser les erreurs de tests",
        )
        FIXER_MODEL: str = Field(
            default="qwen2.5-coder:14b",
            description="Modele pour fixer les bugs (meme qualite que le coder)",
        )
        SANDBOX_TIMEOUT: int = Field(
            default=120,
            description="Timeout du sandbox en secondes",
        )
        MAX_FIX_ITERATIONS: int = Field(
            default=15,
            description="Nombre max d'iterations de fix (-1 = illimite)",
        )
        SANDBOX_MODE: str = Field(
            default="auto",
            description="Mode sandbox: 'docker', 'local', ou 'auto' (docker si disponible)",
        )
        DOCKER_IMAGE: str = Field(
            default="python:3.12-slim",
            description="Image Docker pour le sandbox",
        )
        NUM_CTX: int = Field(
            default=16384,
            description="Taille du contexte Ollama (num_ctx)",
        )
        TEMPERATURE: float = Field(
            default=0.15,
            description="Temperature par defaut pour la generation",
        )

    def __init__(self) -> None:
        self.valves = self.Valves()

    # ------------------------------------------------------------------ helpers
    async def _emit_status(
        self,
        emitter: Optional[Callable],
        description: str,
        done: bool = False,
    ) -> None:
        if emitter:
            await emitter(
                {
                    "type": "status",
                    "data": {"description": description, "done": done, "hidden": False},
                }
            )

    async def _emit_message(self, emitter: Optional[Callable], content: str) -> None:
        if emitter:
            await emitter(
                {"type": "chat:message:delta", "data": {"content": content}}
            )

    async def _emit_replace(self, emitter: Optional[Callable], content: str) -> None:
        if emitter:
            await emitter(
                {"type": "chat:message", "data": {"content": content}}
            )

    async def _emit_notification(
        self, emitter: Optional[Callable], content: str, level: str = "info"
    ) -> None:
        if emitter:
            await emitter(
                {"type": "notification", "data": {"type": level, "content": content}}
            )

    # ------------------------------------------------------------------ Ollama
    async def _ollama_chat(
        self,
        model: str,
        messages: list[dict[str, str]],
        *,
        json_mode: bool = False,
        temperature: float | None = None,
        emitter: Optional[Callable] = None,
        stream_label: str = "",
    ) -> str:
        """Call Ollama /api/chat with streaming, pushing tokens to the chat."""
        import httpx

        url = f"{self.valves.OLLAMA_BASE_URL.rstrip('/')}/api/chat"
        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "stream": True,
            "options": {
                "num_ctx": self.valves.NUM_CTX,
                "temperature": temperature if temperature is not None else self.valves.TEMPERATURE,
            },
        }
        if json_mode:
            payload["format"] = "json"

        full_text = ""
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(connect=15.0, read=None, write=60.0, pool=30.0)) as client:
                async with client.stream("POST", url, json=payload) as resp:
                    resp.raise_for_status()
                    async for line in resp.aiter_lines():
                        if not line.strip():
                            continue
                        try:
                            chunk = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        token = chunk.get("message", {}).get("content", "")
                        if token:
                            full_text += token
                            if emitter and stream_label:
                                await self._emit_message(emitter, token)
                        if chunk.get("done"):
                            break
        except httpx.HTTPStatusError as exc:
            raise RuntimeError(
                f"Ollama returned {exc.response.status_code}: {exc.response.text[:500]}"
            ) from exc
        except httpx.ConnectError as exc:
            raise RuntimeError(
                f"Cannot connect to Ollama at {self.valves.OLLAMA_BASE_URL}. "
                f"Verifie que le serveur est lance. ({exc})"
            ) from exc

        return _strip_reasoning(full_text)

    # ------------------------------------------------------------------ Sandbox
    async def _run_sandbox(
        self, workspace: Path, command: str
    ) -> tuple[int, str, str, bool]:
        """Run a command in the sandbox. Returns (exit_code, stdout, stderr, timed_out)."""
        mode = self.valves.SANDBOX_MODE
        if mode == "auto":
            mode = "docker" if shutil.which("docker") else "local"

        if mode == "docker":
            return await self._run_docker(workspace, command)
        return await self._run_local(workspace, command)

    async def _run_local(
        self, workspace: Path, command: str
    ) -> tuple[int, str, str, bool]:
        ws = str(workspace.resolve())
        full_cmd = f"export PYTHONPATH={ws}:${{PYTHONPATH:-}}; cd {ws} && {command}"
        proc = await asyncio.create_subprocess_shell(
            full_cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=ws,
        )
        timed_out = False
        try:
            stdout_b, stderr_b = await asyncio.wait_for(
                proc.communicate(), timeout=self.valves.SANDBOX_TIMEOUT
            )
        except TimeoutError:
            proc.kill()
            await proc.wait()
            timed_out = True
            return -1, "", f"Timeout after {self.valves.SANDBOX_TIMEOUT}s", True
        rc = proc.returncode if proc.returncode is not None else -1
        return rc, stdout_b.decode("utf-8", errors="replace"), stderr_b.decode("utf-8", errors="replace"), timed_out

    async def _run_docker(
        self, workspace: Path, command: str
    ) -> tuple[int, str, str, bool]:
        workspace = workspace.resolve()
        workspace.mkdir(parents=True, exist_ok=True)
        docker_cmd = [
            "docker", "run", "--rm",
            "--network", "host",
            "--workdir", "/work",
            "-v", f"{workspace}:/work",
            "-e", "PIP_DISABLE_PIP_VERSION_CHECK=1",
            "-e", "PYTHONDONTWRITEBYTECODE=1",
            "-e", "PYTHONPATH=/work",
            self.valves.DOCKER_IMAGE,
            "bash", "-lc", command,
        ]
        proc = await asyncio.create_subprocess_exec(
            *docker_cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        timed_out = False
        try:
            stdout_b, stderr_b = await asyncio.wait_for(
                proc.communicate(), timeout=self.valves.SANDBOX_TIMEOUT
            )
        except TimeoutError:
            proc.kill()
            await proc.wait()
            return -1, "", f"Timeout after {self.valves.SANDBOX_TIMEOUT}s", True
        rc = proc.returncode if proc.returncode is not None else -1
        return rc, stdout_b.decode("utf-8", errors="replace"), stderr_b.decode("utf-8", errors="replace"), timed_out

    # ------------------------------------------------------------------ Agents

    async def _run_planner(
        self, prompt: str, emitter: Optional[Callable]
    ) -> dict[str, Any]:
        messages = [
            {"role": "system", "content": PLAN_SYSTEM},
            {"role": "user", "content": PLAN_PROMPT.format(prompt=prompt)},
        ]
        raw = await self._ollama_chat(
            self.valves.PLANNER_MODEL,
            messages,
            json_mode=True,
            temperature=0.2,
        )
        return _extract_json(raw)

    async def _run_coder_task(
        self,
        plan: dict[str, Any],
        task: dict[str, Any],
        existing_files: dict[str, str],
        emitter: Optional[Callable],
    ) -> dict[str, str]:
        file_list = "\n".join(f"- {p}" for p in task.get("file_paths", []))
        existing_block = self._fmt_existing(existing_files)
        prompt = CODE_PROMPT.format(
            plan=json.dumps(plan, indent=2, ensure_ascii=False),
            title=task.get("title", ""),
            description=task.get("description", ""),
            file_list=file_list or "(see description)",
            existing_files_block=existing_block,
        )
        messages = [
            {"role": "system", "content": CODE_SYSTEM},
            {"role": "user", "content": prompt},
        ]
        raw = await self._ollama_chat(
            self.valves.CODER_MODEL,
            messages,
            emitter=emitter,
            stream_label="coder",
        )
        blocks = _extract_code_blocks(raw)
        if not blocks:
            raise ValueError("Coder did not produce any fenced file blocks")
        return blocks

    async def _run_reviewer(
        self,
        plan: dict[str, Any],
        files: dict[str, str],
        emitter: Optional[Callable],
    ) -> dict[str, Any]:
        files_block = self._fmt_existing(files)
        prompt = REVIEW_PROMPT.format(
            plan=json.dumps(plan, indent=2, ensure_ascii=False),
            files_block=files_block,
        )
        messages = [
            {"role": "system", "content": REVIEW_SYSTEM},
            {"role": "user", "content": prompt},
        ]
        raw = await self._ollama_chat(
            self.valves.REVIEWER_MODEL,
            messages,
            json_mode=True,
            temperature=0.1,
        )
        return _extract_json(raw)

    async def _run_analyst(
        self,
        test_command: str,
        stdout: str,
        stderr: str,
        files: dict[str, str],
        emitter: Optional[Callable],
    ) -> dict[str, Any]:
        files_block = self._fmt_existing(files)
        prompt = ANALYST_PROMPT.format(
            test_command=test_command,
            stdout=stdout[-3000:] if len(stdout) > 3000 else stdout,
            stderr=stderr[-3000:] if len(stderr) > 3000 else stderr,
            files_block=files_block,
        )
        messages = [
            {"role": "system", "content": ANALYST_SYSTEM},
            {"role": "user", "content": prompt},
        ]
        raw = await self._ollama_chat(
            self.valves.ANALYST_MODEL,
            messages,
            json_mode=True,
            temperature=0.1,
        )
        return _extract_json(raw)

    async def _run_fixer(
        self,
        plan: dict[str, Any],
        analysis: dict[str, Any],
        stdout: str,
        stderr: str,
        files: dict[str, str],
        emitter: Optional[Callable],
    ) -> dict[str, str]:
        existing_block = self._fmt_existing(files)
        prompt = FIX_PROMPT.format(
            plan=json.dumps(plan, indent=2, ensure_ascii=False),
            stdout=stdout[-2000:] if len(stdout) > 2000 else stdout,
            stderr=stderr[-2000:] if len(stderr) > 2000 else stderr,
            analysis=json.dumps(analysis, indent=2, ensure_ascii=False),
            existing_files_block=existing_block,
        )
        messages = [
            {"role": "system", "content": FIX_SYSTEM},
            {"role": "user", "content": prompt},
        ]
        raw = await self._ollama_chat(
            self.valves.FIXER_MODEL,
            messages,
            emitter=emitter,
            stream_label="fixer",
        )
        blocks = _extract_code_blocks(raw)
        if not blocks:
            raise ValueError("Fixer did not produce any fenced file blocks")
        return blocks

    # ------------------------------------------------------------------ helpers

    @staticmethod
    def _fmt_existing(files: dict[str, str], limit_chars: int = 40000) -> str:
        if not files:
            return ""
        out = ["Existing files:"]
        used = 0
        for path, content in files.items():
            header = f"\n--- {path} ---\n"
            chunk = header + content
            if used + len(chunk) > limit_chars:
                out.append(f"\n--- {path} --- [truncated]\n")
                used += 80
                continue
            out.append(chunk)
            used += len(chunk)
        return "".join(out)

    @staticmethod
    def _build_file_tree(files: dict[str, str], project_name: str) -> str:
        """Build a visual file tree from file paths."""
        if not files:
            return ""
        paths = sorted(files.keys())
        lines = [f"{project_name}/"]
        for i, p in enumerate(paths):
            parts = p.split("/")
            prefix = "    " * (len(parts) - 1)
            connector = "`-- " if i == len(paths) - 1 else "|-- "
            lines.append(f"{prefix}{connector}{parts[-1]}")
        return "\n".join(lines)

    @staticmethod
    def _make_zip_base64(files: dict[str, str], project_name: str) -> str:
        """Create a ZIP in memory and return base64-encoded content."""
        buf = BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for path, content in sorted(files.items()):
                zf.writestr(f"{project_name}/{path}", content)
        buf.seek(0)
        return base64.b64encode(buf.read()).decode("ascii")

    @staticmethod
    def _write_files_to_disk(workspace: Path, files: dict[str, str]) -> None:
        for rel_path, content in files.items():
            fp = workspace / rel_path
            fp.parent.mkdir(parents=True, exist_ok=True)
            fp.write_text(content, encoding="utf-8")

    @staticmethod
    def _read_files_from_disk(workspace: Path) -> dict[str, str]:
        files: dict[str, str] = {}
        for fp in sorted(workspace.rglob("*")):
            if fp.is_file() and not any(
                part.startswith(".") or part == "__pycache__" or part == "node_modules"
                for part in fp.relative_to(workspace).parts
            ):
                try:
                    files[str(fp.relative_to(workspace))] = fp.read_text(
                        encoding="utf-8", errors="replace"
                    )
                except Exception:
                    pass
        return files

    def _infer_install_command(
        self, install_cmd: str, test_cmd: str, workspace: Path
    ) -> str:
        parts: list[str] = []
        req = workspace / "requirements.txt"
        req_has_pytest = False
        if req.exists():
            try:
                content = req.read_text(encoding="utf-8", errors="replace")
            except OSError:
                content = ""
            non_comment = [
                line.strip()
                for line in content.splitlines()
                if line.strip() and not line.lstrip().startswith("#")
            ]
            if non_comment:
                parts.append("pip install --quiet --no-input -r requirements.txt")
                req_has_pytest = any(
                    line.split("==")[0].split(">=")[0].split("<=")[0].strip().lower()
                    == "pytest"
                    for line in non_comment
                )
        needs_pytest = (
            "pytest" in test_cmd and "pytest" not in install_cmd and not req_has_pytest
        )
        if needs_pytest:
            parts.append("pip install --quiet --no-input pytest")
        if install_cmd:
            parts.append(install_cmd)
        return " && ".join(parts)

    # ------------------------------------------------------------------ MAIN

    async def pipe(
        self,
        body: dict,
        __event_emitter__: Optional[Callable] = None,
        __user__: Optional[dict] = None,
    ) -> str:
        """Main pipeline entry point called by Open-WebUI."""
        emit = __event_emitter__
        messages = body.get("messages", [])
        if not messages:
            return "Envoie-moi une description de projet et je le construis pour toi."

        user_prompt = messages[-1].get("content", "").strip()
        if not user_prompt:
            return "Envoie-moi une description de projet et je le construis pour toi."

        # Accumulated markdown for the full chat message
        md = ""

        async def append(text: str) -> None:
            nonlocal md
            md += text
            await self._emit_message(emit, text)

        try:
            # ============================================================
            # PHASE 1: PLANNING
            # ============================================================
            await self._emit_status(emit, "Phase 1/6 : Planification du projet...")
            await append(
                "## Multi-Agent Coder Pipeline\n\n"
                "---\n\n"
                "### Phase 1/6 : Planification\n\n"
                "> Analyse de ta demande et decomposition en taches...\n\n"
            )

            plan = await self._run_planner(user_prompt, emit)
            project_name = plan.get("name", "project")
            tasks = plan.get("tasks", [])
            files_spec = plan.get("files", [])
            test_cmd = plan.get("test_command", "")
            install_cmd = plan.get("install_command", "")

            # Display plan
            await append(f"**Projet : `{project_name}`**\n\n")
            await append(f"> {plan.get('summary', '')}\n\n")

            # Task table
            await append("| # | Tache | Fichiers | Statut |\n")
            await append("|---|-------|----------|--------|\n")
            for t in tasks:
                fps = ", ".join(f"`{f}`" for f in t.get("file_paths", []))
                await append(
                    f"| {t.get('id', '?')} | {t.get('title', '?')} | {fps} | En attente |\n"
                )
            await append("\n")

            await self._emit_status(emit, "Phase 1/6 : Planification terminee", done=True)

            # ============================================================
            # PHASE 2: ARCHITECTURE (file tree)
            # ============================================================
            await self._emit_status(emit, "Phase 2/6 : Architecture...")
            await append(
                "---\n\n"
                "### Phase 2/6 : Architecture\n\n"
            )

            tree = self._build_file_tree(
                {f["path"]: "" for f in files_spec}, project_name
            )
            await append(f"```\n{tree}\n```\n\n")
            await self._emit_status(emit, "Phase 2/6 : Architecture terminee", done=True)

            # ============================================================
            # PHASE 3: CODING
            # ============================================================
            await self._emit_status(emit, "Phase 3/6 : Generation du code...")
            await append(
                "---\n\n"
                "### Phase 3/6 : Codage\n\n"
            )

            all_files: dict[str, str] = {}
            total_tasks = len(tasks)

            for idx, task in enumerate(tasks, 1):
                task_title = task.get("title", f"Task {idx}")
                await self._emit_status(
                    emit, f"Phase 3/6 : Codage -- tache {idx}/{total_tasks} : {task_title}"
                )
                await append(f"**Tache {idx}/{total_tasks} : {task_title}**\n\n")

                try:
                    new_blocks = await self._run_coder_task(plan, task, all_files, emit)
                except Exception as exc:
                    await append(f"\n> Erreur sur cette tache: {exc}\n\n")
                    continue

                for fpath, content in new_blocks.items():
                    all_files[fpath] = content
                    line_count = content.count("\n")
                    await append(f"\n`{fpath}` ({line_count} lignes)\n\n")

            await self._emit_status(
                emit,
                f"Phase 3/6 : Codage termine -- {len(all_files)} fichiers generes",
                done=True,
            )

            # ============================================================
            # PHASE 4: REVIEW
            # ============================================================
            await self._emit_status(emit, "Phase 4/6 : Revue de code...")
            await append(
                "---\n\n"
                "### Phase 4/6 : Revue de code\n\n"
            )

            try:
                review = await self._run_reviewer(plan, all_files, emit)
                approved = review.get("approved", True)
                issues = review.get("issues", [])

                if approved:
                    await append("> Code approuve par le reviewer.\n\n")
                else:
                    await append(f"> **{len(issues)} probleme(s) detecte(s) :**\n\n")
                    for iss in issues[:10]:
                        sev = iss.get("severity", "warning")
                        icon = "!!!" if sev == "error" else "!"
                        await append(
                            f"- [{icon}] `{iss.get('file', '?')}` : {iss.get('message', '?')}\n"
                        )
                    await append("\n")
            except Exception as exc:
                await append(f"> Review skippee (erreur: {exc})\n\n")

            await self._emit_status(emit, "Phase 4/6 : Revue terminee", done=True)

            # ============================================================
            # PHASE 5: TESTING (loop until green)
            # ============================================================
            if not test_cmd:
                await append(
                    "---\n\n"
                    "### Phase 5/6 : Tests\n\n"
                    "> Aucune commande de test definie -- skip.\n\n"
                )
                await self._emit_status(emit, "Phase 5/6 : Pas de tests", done=True)
            else:
                await self._emit_status(emit, "Phase 5/6 : Execution des tests...")
                await append(
                    "---\n\n"
                    "### Phase 5/6 : Tests & Fix Loop\n\n"
                )

                # Write files to a temp workspace
                workspace = Path(tempfile.mkdtemp(prefix="mac_"))
                self._write_files_to_disk(workspace, all_files)

                # Build install + test command
                full_install = self._infer_install_command(
                    install_cmd, test_cmd, workspace
                )
                full_command = (
                    f"{full_install} && {test_cmd}"
                    if full_install
                    else test_cmd
                )

                max_iter = self.valves.MAX_FIX_ITERATIONS
                iteration = 0
                tests_passed = False

                while True:
                    iteration += 1
                    if max_iter > 0 and iteration > max_iter:
                        await append(
                            f"\n> Limite de {max_iter} iterations atteinte. Arret de la boucle.\n\n"
                        )
                        break

                    await self._emit_status(
                        emit,
                        f"Phase 5/6 : Test run #{iteration}...",
                    )
                    await append(f"**Test run #{iteration}**\n\n")
                    await append(f"```bash\n$ {full_command}\n")

                    exit_code, stdout, stderr, timed_out = await self._run_sandbox(
                        workspace, full_command
                    )

                    # Show output (truncated)
                    combined = (stdout + "\n" + stderr).strip()
                    if len(combined) > 3000:
                        combined = combined[:1500] + "\n...[tronque]...\n" + combined[-1500:]
                    await append(f"{combined}\n```\n\n")

                    if exit_code == 0 and not timed_out:
                        tests_passed = True
                        await append("> **TOUS LES TESTS PASSENT !**\n\n")
                        await self._emit_notification(emit, "Tests passes !", "success")
                        break

                    # Show failure
                    if timed_out:
                        await append(f"> Timeout apres {self.valves.SANDBOX_TIMEOUT}s\n\n")
                    else:
                        await append(f"> Exit code: {exit_code}\n\n")

                    # Analyze failure
                    await self._emit_status(
                        emit,
                        f"Phase 5/6 : Analyse de l'echec #{iteration}...",
                    )
                    await append("**Analyse de l'erreur...**\n\n")

                    try:
                        analysis = await self._run_analyst(
                            test_cmd, stdout, stderr, all_files, emit
                        )
                        root_cause = analysis.get("root_cause", "Unknown")
                        await append(f"> Cause racine : {root_cause}\n\n")
                    except Exception:
                        analysis = {
                            "root_cause": stderr[-500:] if stderr else "Unknown",
                            "files_to_fix": list(all_files.keys())[:3],
                            "fix_description": "Fix the failing tests based on the error output",
                        }
                        await append("> Analyse automatique (fallback)\n\n")

                    # Fix
                    await self._emit_status(
                        emit,
                        f"Phase 5/6 : Fix iteration #{iteration}...",
                    )
                    await append(f"**Fix #{iteration}**\n\n")

                    try:
                        fixed_blocks = await self._run_fixer(
                            plan, analysis, stdout, stderr, all_files, emit
                        )
                        for fpath, content in fixed_blocks.items():
                            all_files[fpath] = content
                            await append(f"\n`{fpath}` (modifie)\n")
                        await append("\n")

                        # Write updated files
                        self._write_files_to_disk(workspace, all_files)
                    except Exception as exc:
                        await append(f"\n> Erreur du fixer: {exc}\n\n")

                # Cleanup workspace
                try:
                    shutil.rmtree(workspace, ignore_errors=True)
                except Exception:
                    pass

                status_msg = (
                    "Phase 5/6 : Tests passes !"
                    if tests_passed
                    else f"Phase 5/6 : Tests echoues apres {iteration} iterations"
                )
                await self._emit_status(emit, status_msg, done=True)

            # ============================================================
            # PHASE 6: PACKAGING (ZIP)
            # ============================================================
            await self._emit_status(emit, "Phase 6/6 : Creation du ZIP...")
            await append(
                "---\n\n"
                "### Phase 6/6 : Livraison\n\n"
            )

            zip_b64 = self._make_zip_base64(all_files, project_name)
            zip_size_kb = len(base64.b64decode(zip_b64)) / 1024

            await append(f"**{project_name}.zip** ({zip_size_kb:.1f} Ko)\n\n")

            # Show final file tree
            final_tree = self._build_file_tree(all_files, project_name)
            await append(f"```\n{final_tree}\n```\n\n")

            # Provide download via JavaScript execution in the browser
            js_download = (
                f"(function(){{"
                f"var a=document.createElement('a');"
                f"a.href='data:application/zip;base64,{zip_b64}';"
                f"a.download='{project_name}.zip';"
                f"document.body.appendChild(a);a.click();document.body.removeChild(a);"
                f"return '{project_name}.zip telecharge!';"
                f"}})()"
            )

            # Emit the download trigger
            if emit:
                await emit(
                    {
                        "type": "execute",
                        "data": {"code": js_download},
                    }
                )

            # Also provide a clickable data URL as fallback
            data_url = f"data:application/zip;base64,{zip_b64}"
            await append(
                f'<a href="{data_url}" download="{project_name}.zip" '
                f'style="display:inline-block;padding:12px 24px;background:#10b981;'
                f"color:white;border-radius:8px;text-decoration:none;font-weight:bold;"
                f'font-size:16px;margin:8px 0;">'
                f"Telecharger {project_name}.zip</a>\n\n"
            )

            await append("---\n\n")

            # Summary
            total_lines = sum(c.count("\n") for c in all_files.values())
            await append(
                f"**Resume :**\n"
                f"- {len(all_files)} fichiers generes\n"
                f"- {total_lines} lignes de code\n"
                f"- {len(tasks)} taches completees\n"
                f"- {zip_size_kb:.1f} Ko (ZIP)\n\n"
            )

            await self._emit_status(emit, "Pipeline terminee !", done=True)
            await self._emit_notification(emit, f"Projet {project_name} termine !", "success")

            return ""

        except Exception as exc:
            error_detail = traceback.format_exc()
            log.exception("Pipeline failed")
            await self._emit_status(emit, f"Erreur: {exc}", done=True)
            await self._emit_notification(emit, f"Erreur: {exc}", "error")
            error_msg = (
                f"\n\n---\n\n"
                f"### Erreur Pipeline\n\n"
                f"```\n{error_detail}\n```\n\n"
                f"Verifie que ton serveur Ollama est accessible a "
                f"`{self.valves.OLLAMA_BASE_URL}` et que les modeles sont installes."
            )
            await append(error_msg)
            return ""
