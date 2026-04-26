"""
title: Multi-Agent Coder
author: Johnny
author_url: https://github.com/amavaljoh04-lang
git_url: https://github.com/amavaljoh04-lang/multi-agent-coder.git
description: Pipeline multi-agent autonome pour Open-WebUI. Decris un projet, il le code, teste en boucle, et livre un ZIP. En mode chat normal sinon.
required_open_webui_version: 0.4.0
requirements: httpx
version: 2.0.0
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
# Helpers: JSON / code-block extraction
# ---------------------------------------------------------------------------

_JSON_BLOCK = re.compile(r"```(?:json)?\s*(\{.*?\}|\[.*?\])\s*```", re.DOTALL)
_FIRST_BRACE = re.compile(r"(\{.*\}|\[.*\])", re.DOTALL)
_THINK_BLOCK = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)

# Keywords that signal a project-generation request (FR + EN)
_BUILD_VERBS = re.compile(
    r"\b(cree|crée|créé|genere|génère|generer|générer|code|programme|"
    r"developpe|développe|construis|fais|build|create|generate|make|"
    r"write|develop|implement|implémente|implemente)\b",
    re.IGNORECASE,
)
_BUILD_NOUNS = re.compile(
    r"\b(projet|project|application|app|api|script|site|website|"
    r"programme|program|outil|tool|cli|bot|jeu|game|serveur|server|"
    r"library|lib|package|module|service|microservice|dashboard|"
    r"backend|frontend|fullstack|pipeline)\b",
    re.IGNORECASE,
)
# Explicit trigger prefix — always activates the pipeline
_TRIGGER_PREFIX = re.compile(r"^/(build|code|projet|project|generate)\b", re.IGNORECASE)


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


def _is_build_request(text: str) -> bool:
    """Return True if the message looks like a project-generation request."""
    if _TRIGGER_PREFIX.search(text):
        return True
    has_verb = bool(_BUILD_VERBS.search(text))
    has_noun = bool(_BUILD_NOUNS.search(text))
    return has_verb and has_noun


# ---------------------------------------------------------------------------
# System prompts
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

    In normal chat mode, forwards messages to the configured Ollama model.
    When a project-generation request is detected (or /build command used),
    it activates the full multi-agent pipeline:
    Plan -> Code -> Review -> Test+Fix loop -> ZIP download.
    """

    class Valves(BaseModel):
        OLLAMA_BASE_URL: str = Field(
            default="http://localhost:11434",
            description="URL de base de ton serveur Ollama",
        )
        CHAT_MODEL: str = Field(
            default="qwen2.5-coder:7b",
            description="Modele pour le chat normal (quand tu ne generes pas de projet)",
        )
        PLANNER_MODEL: str = Field(
            default="qwen2.5-coder:7b",
            description="Modele pour le planning",
        )
        CODER_MODEL: str = Field(
            default="qwen2.5-coder:14b",
            description="Modele principal pour coder",
        )
        REVIEWER_MODEL: str = Field(
            default="deepseek-coder:6.7b",
            description="Modele pour la review de code",
        )
        ANALYST_MODEL: str = Field(
            default="deepseek-coder:6.7b",
            description="Modele pour analyser les erreurs",
        )
        FIXER_MODEL: str = Field(
            default="qwen2.5-coder:14b",
            description="Modele pour fixer les bugs",
        )
        SANDBOX_TIMEOUT: int = Field(
            default=120,
            description="Timeout du sandbox en secondes",
        )
        MAX_FIX_ITERATIONS: int = Field(
            default=10,
            description="Nombre max d'iterations de fix (-1 = illimite)",
        )
        SANDBOX_MODE: str = Field(
            default="auto",
            description="Mode sandbox: 'docker', 'local', ou 'auto'",
        )
        DOCKER_IMAGE: str = Field(
            default="python:3.12-slim",
            description="Image Docker pour le sandbox",
        )
        NUM_CTX: int = Field(
            default=16384,
            description="Taille du contexte Ollama",
        )
        TEMPERATURE: float = Field(
            default=0.15,
            description="Temperature par defaut",
        )

    def __init__(self) -> None:
        self.valves = self.Valves()

    # ================================================================ emitters
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

    async def _emit_notification(
        self, emitter: Optional[Callable], content: str, level: str = "info"
    ) -> None:
        if emitter:
            await emitter(
                {"type": "notification", "data": {"type": level, "content": content}}
            )

    # ================================================================ Ollama
    async def _ollama_chat(
        self,
        model: str,
        messages: list[dict[str, str]],
        *,
        json_mode: bool = False,
        temperature: float | None = None,
        emitter: Optional[Callable] = None,
        stream_to_chat: bool = False,
    ) -> str:
        """Call Ollama /api/chat, stream tokens, return full text.

        Uses aiter_bytes + manual NDJSON splitting for maximum httpx compat.
        """
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
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(connect=15.0, read=600.0, write=60.0, pool=30.0)
            ) as client:
                async with client.stream("POST", url, json=payload) as resp:
                    resp.raise_for_status()
                    buf = b""
                    async for raw_chunk in resp.aiter_bytes():
                        buf += raw_chunk
                        while b"\n" in buf:
                            line_bytes, buf = buf.split(b"\n", 1)
                            line_str = line_bytes.decode("utf-8", errors="replace").strip()
                            if not line_str:
                                continue
                            try:
                                chunk = json.loads(line_str)
                            except json.JSONDecodeError:
                                continue
                            token = chunk.get("message", {}).get("content", "")
                            if token:
                                full_text += token
                                if stream_to_chat and emitter:
                                    await self._emit_message(emitter, token)
                            if chunk.get("done"):
                                break
        except httpx.HTTPStatusError as exc:
            err_body = ""
            try:
                err_body = exc.response.text[:500]
            except Exception:
                pass
            raise RuntimeError(
                f"Ollama HTTP {exc.response.status_code}: {err_body}"
            ) from exc
        except httpx.ConnectError as exc:
            raise RuntimeError(
                f"Impossible de se connecter a Ollama ({self.valves.OLLAMA_BASE_URL}). "
                f"Verifie que le serveur tourne. Erreur: {exc}"
            ) from exc

        return _strip_reasoning(full_text)

    async def _ollama_chat_simple(
        self,
        model: str,
        messages: list[dict[str, str]],
        emitter: Optional[Callable] = None,
    ) -> str:
        """Simple chat that streams every token to the user (for normal conversation)."""
        return await self._ollama_chat(
            model,
            messages,
            temperature=0.7,
            emitter=emitter,
            stream_to_chat=True,
        )

    # ================================================================ Sandbox
    async def _run_sandbox(
        self, workspace: Path, command: str
    ) -> tuple[int, str, str, bool]:
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
        try:
            stdout_b, stderr_b = await asyncio.wait_for(
                proc.communicate(), timeout=self.valves.SANDBOX_TIMEOUT
            )
        except TimeoutError:
            proc.kill()
            await proc.wait()
            return -1, "", f"Timeout after {self.valves.SANDBOX_TIMEOUT}s", True
        rc = proc.returncode if proc.returncode is not None else -1
        return rc, stdout_b.decode("utf-8", errors="replace"), stderr_b.decode("utf-8", errors="replace"), False

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
        try:
            stdout_b, stderr_b = await asyncio.wait_for(
                proc.communicate(), timeout=self.valves.SANDBOX_TIMEOUT
            )
        except TimeoutError:
            proc.kill()
            await proc.wait()
            return -1, "", f"Timeout after {self.valves.SANDBOX_TIMEOUT}s", True
        rc = proc.returncode if proc.returncode is not None else -1
        return rc, stdout_b.decode("utf-8", errors="replace"), stderr_b.decode("utf-8", errors="replace"), False

    # ================================================================ Agents

    async def _run_planner(self, prompt: str) -> dict[str, Any]:
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
            stream_to_chat=True,
        )
        blocks = _extract_code_blocks(raw)
        if not blocks:
            raise ValueError("Le coder n'a produit aucun bloc de fichier")
        return blocks

    async def _run_reviewer(
        self,
        plan: dict[str, Any],
        files: dict[str, str],
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
            stream_to_chat=True,
        )
        blocks = _extract_code_blocks(raw)
        if not blocks:
            raise ValueError("Le fixer n'a produit aucun bloc de fichier")
        return blocks

    # ================================================================ helpers

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
        if not files:
            return f"{project_name}/ (empty)"
        sorted_paths = sorted(files.keys())
        lines = [f"{project_name}/"]
        for i, p in enumerate(sorted_paths):
            is_last = i == len(sorted_paths) - 1
            symbol = "`-- " if is_last else "|-- "
            lines.append(f"{symbol}{p}")
        return "\n".join(lines)

    @staticmethod
    def _make_zip_base64(files: dict[str, str], project_name: str) -> str:
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

    # ================================================================ MAIN PIPE

    async def pipe(
        self,
        body: dict,
        __event_emitter__: Optional[Callable] = None,
        __user__: Optional[dict] = None,
    ) -> str:
        emit = __event_emitter__
        messages = body.get("messages", [])
        if not messages:
            return "Salut ! Decris un projet et je le construis pour toi, ou parle-moi normalement."

        user_prompt = messages[-1].get("content", "").strip()
        if not user_prompt:
            return "Envoie-moi un message !"

        # ---------------------------------------------------------------
        # Route: normal chat vs multi-agent pipeline
        # ---------------------------------------------------------------
        clean_prompt = _TRIGGER_PREFIX.sub("", user_prompt).strip()
        if not _is_build_request(user_prompt):
            # Normal chat mode — forward to Ollama and stream back
            await self._emit_status(emit, "Reflexion...", done=False)
            chat_messages = []
            for m in messages:
                role = m.get("role", "user")
                content = m.get("content", "")
                if role in ("user", "assistant", "system"):
                    chat_messages.append({"role": role, "content": content})
            await self._ollama_chat_simple(
                self.valves.CHAT_MODEL, chat_messages, emitter=emit
            )
            await self._emit_status(emit, "", done=True)
            return ""

        # ---------------------------------------------------------------
        # Multi-agent pipeline mode
        # ---------------------------------------------------------------
        prompt_for_plan = clean_prompt if clean_prompt else user_prompt

        md = ""

        async def append(text: str) -> None:
            nonlocal md
            md += text
            await self._emit_message(emit, text)

        try:
            # ==========================================================
            # PHASE 1: PLANNING
            # ==========================================================
            await self._emit_status(emit, "Phase 1/5 : Planification...")
            await append(
                "\n\n"
                "---\n\n"
                "# Multi-Agent Coder\n\n"
                "### Phase 1/5 : Planification\n"
                "> Analyse de ta demande...\n\n"
            )

            plan = await self._run_planner(prompt_for_plan)
            project_name = plan.get("name", "project")
            tasks = plan.get("tasks", [])
            files_spec = plan.get("files", [])
            test_cmd = plan.get("test_command", "")
            install_cmd = plan.get("install_command", "")

            await append(f"**Projet** : `{project_name}`\n\n")
            summary = plan.get("summary", "")
            if summary:
                await append(f"> {summary}\n\n")

            # Task table
            await append(
                "| # | Tache | Fichiers |\n"
                "|:--|:------|:---------|\n"
            )
            for t in tasks:
                fps = ", ".join(f"`{f}`" for f in t.get("file_paths", []))
                await append(f"| {t.get('id', '?')} | {t.get('title', '?')} | {fps} |\n")
            await append("\n")

            # File tree
            tree = self._build_file_tree(
                {f["path"]: "" for f in files_spec}, project_name
            )
            await append(f"```\n{tree}\n```\n\n")

            await self._emit_status(emit, "Phase 1/5 : Plan OK", done=True)
            await self._emit_notification(emit, f"Plan cree : {len(tasks)} taches, {len(files_spec)} fichiers", "info")

            # ==========================================================
            # PHASE 2: CODING
            # ==========================================================
            await self._emit_status(emit, "Phase 2/5 : Generation du code...")
            await append("---\n\n### Phase 2/5 : Codage\n\n")

            all_files: dict[str, str] = {}
            total_tasks = len(tasks)

            for idx, task in enumerate(tasks, 1):
                task_title = task.get("title", f"Task {idx}")
                await self._emit_status(
                    emit, f"Phase 2/5 : Tache {idx}/{total_tasks} — {task_title}"
                )
                await append(f"**[{idx}/{total_tasks}] {task_title}**\n\n")

                try:
                    new_blocks = await self._run_coder_task(plan, task, all_files, emit)
                    for fpath, content in new_blocks.items():
                        all_files[fpath] = content
                    wrote = ", ".join(f"`{f}`" for f in new_blocks)
                    await append(f"\n\nFichiers ecrits : {wrote}\n\n")
                except Exception as exc:
                    await append(f"\n\n> Erreur: {exc}\n\n")
                    log.warning("Coder task %s failed: %s", task_title, exc)

            await self._emit_status(
                emit,
                f"Phase 2/5 : {len(all_files)} fichiers generes",
                done=True,
            )

            # ==========================================================
            # PHASE 3: REVIEW
            # ==========================================================
            await self._emit_status(emit, "Phase 3/5 : Review...")
            await append("---\n\n### Phase 3/5 : Review\n\n")

            try:
                review = await self._run_reviewer(plan, all_files)
                approved = review.get("approved", True)
                issues = review.get("issues", [])
                if approved:
                    await append("> Code approuve.\n\n")
                else:
                    await append(f"> {len(issues)} probleme(s) detecte(s) :\n\n")
                    for iss in issues[:10]:
                        sev = iss.get("severity", "warning")
                        marker = "ERREUR" if sev == "error" else "Warning"
                        await append(
                            f"- **{marker}** `{iss.get('file', '?')}` : {iss.get('message', '?')}\n"
                        )
                    await append("\n")
            except Exception as exc:
                await append(f"> Review skippee ({exc})\n\n")
                log.warning("Review failed: %s", exc)

            await self._emit_status(emit, "Phase 3/5 : Review OK", done=True)

            # ==========================================================
            # PHASE 4: TEST + FIX LOOP
            # ==========================================================
            if not test_cmd:
                await append("---\n\n### Phase 4/5 : Tests\n> Pas de commande de test — skip.\n\n")
                await self._emit_status(emit, "Phase 4/5 : Pas de tests", done=True)
            else:
                await self._emit_status(emit, "Phase 4/5 : Tests...")
                await append("---\n\n### Phase 4/5 : Tests & Corrections\n\n")

                workspace = Path(tempfile.mkdtemp(prefix="mac_"))
                self._write_files_to_disk(workspace, all_files)

                full_install = self._infer_install_command(install_cmd, test_cmd, workspace)
                full_command = f"{full_install} && {test_cmd}" if full_install else test_cmd

                max_iter = self.valves.MAX_FIX_ITERATIONS
                iteration = 0
                tests_passed = False

                while True:
                    iteration += 1
                    if 0 < max_iter < iteration:
                        await append(f"\n> Limite de {max_iter} iterations atteinte.\n\n")
                        break

                    await self._emit_status(emit, f"Phase 4/5 : Test #{iteration}...")
                    await append(f"**Test #{iteration}**\n```\n$ {test_cmd}\n")

                    exit_code, stdout, stderr, timed_out = await self._run_sandbox(
                        workspace, full_command
                    )

                    combined = (stdout + "\n" + stderr).strip()
                    if len(combined) > 2000:
                        combined = combined[:800] + "\n...\n" + combined[-800:]
                    await append(f"{combined}\n```\n\n")

                    if exit_code == 0 and not timed_out:
                        tests_passed = True
                        await append("> **TOUS LES TESTS PASSENT !**\n\n")
                        await self._emit_notification(emit, "Tests OK !", "success")
                        break

                    if timed_out:
                        await append(f"> Timeout ({self.valves.SANDBOX_TIMEOUT}s)\n\n")
                    else:
                        await append(f"> Exit code {exit_code}\n\n")

                    # Analyse
                    await self._emit_status(emit, f"Phase 4/5 : Analyse erreur #{iteration}...")
                    try:
                        analysis = await self._run_analyst(test_cmd, stdout, stderr, all_files)
                        await append(f"> **Cause** : {analysis.get('root_cause', '?')}\n\n")
                    except Exception:
                        analysis = {
                            "root_cause": stderr[-500:] if stderr else "Erreur inconnue",
                            "files_to_fix": list(all_files.keys())[:3],
                            "fix_description": "Corriger selon la sortie d'erreur",
                        }

                    # Fix
                    await self._emit_status(emit, f"Phase 4/5 : Correction #{iteration}...")
                    await append(f"**Correction #{iteration}**\n\n")
                    try:
                        fixed_blocks = await self._run_fixer(
                            plan, analysis, stdout, stderr, all_files, emit
                        )
                        for fpath, content in fixed_blocks.items():
                            all_files[fpath] = content
                        fixed_names = ", ".join(f"`{f}`" for f in fixed_blocks)
                        await append(f"\n\nModifie : {fixed_names}\n\n")
                        self._write_files_to_disk(workspace, all_files)
                    except Exception as exc:
                        await append(f"\n\n> Erreur fixer: {exc}\n\n")
                        log.warning("Fixer failed: %s", exc)

                try:
                    shutil.rmtree(workspace, ignore_errors=True)
                except Exception:
                    pass

                status_msg = (
                    "Phase 4/5 : Tests OK !"
                    if tests_passed
                    else f"Phase 4/5 : Echec apres {iteration} iterations"
                )
                await self._emit_status(emit, status_msg, done=True)

            # ==========================================================
            # PHASE 5: ZIP
            # ==========================================================
            await self._emit_status(emit, "Phase 5/5 : Packaging ZIP...")
            await append("---\n\n### Phase 5/5 : Livraison\n\n")

            if not all_files:
                await append("> Aucun fichier genere — impossible de creer le ZIP.\n\n")
                await self._emit_status(emit, "Termine (aucun fichier)", done=True)
                return ""

            zip_b64 = self._make_zip_base64(all_files, project_name)
            zip_size_kb = len(base64.b64decode(zip_b64)) / 1024

            # Final file tree
            final_tree = self._build_file_tree(all_files, project_name)
            await append(f"```\n{final_tree}\n```\n\n")

            # Download button (HTML in markdown)
            data_url = f"data:application/zip;base64,{zip_b64}"
            await append(
                f'<a href="{data_url}" download="{project_name}.zip" '
                f'style="display:inline-block;padding:14px 28px;'
                f"background:linear-gradient(135deg,#10b981,#059669);"
                f"color:white;border-radius:10px;text-decoration:none;"
                f"font-weight:bold;font-size:16px;margin:12px 0;"
                f'box-shadow:0 4px 14px rgba(16,185,129,0.4);">'
                f"Telecharger {project_name}.zip ({zip_size_kb:.1f} Ko)</a>\n\n"
            )

            # Summary
            total_lines = sum(c.count("\n") for c in all_files.values())
            await append(
                "---\n\n"
                f"**{len(all_files)}** fichiers | "
                f"**{total_lines}** lignes | "
                f"**{len(tasks)}** taches | "
                f"**{zip_size_kb:.1f} Ko**\n"
            )

            await self._emit_status(emit, f"Projet {project_name} termine !", done=True)
            await self._emit_notification(emit, f"{project_name} pret !", "success")
            return ""

        except Exception as exc:
            error_detail = traceback.format_exc()
            log.exception("Pipeline failed")
            await self._emit_status(emit, f"Erreur: {exc}", done=True)
            await self._emit_notification(emit, f"Erreur pipeline: {exc}", "error")
            await append(
                "\n\n---\n\n"
                "### Erreur\n\n"
                f"```\n{error_detail}\n```\n\n"
                f"Verifie que Ollama tourne sur `{self.valves.OLLAMA_BASE_URL}` "
                f"et que les modeles sont installes.\n"
            )
            return ""
