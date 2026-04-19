"""Planner agent: turns a user prompt into an ordered task list."""

from __future__ import annotations

from typing import Any

from ..ollama_client import OllamaRouter
from .base import PLAN_SYSTEM, extract_json

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
  "run_command": "shell command to run the app or main script",
  "test_command": "shell command to run the tests (pytest, npm test, ...)",
  "install_command": "shell command to install deps (pip install -r requirements.txt, npm ci, ...)",
  "files": [
     {{"path": "relative/path.ext", "purpose": "what this file does"}}
  ],
  "tasks": [
     {{
       "id": "t1",
       "title": "short title",
       "description": "precise specification of what to implement in this task",
       "file_paths": ["relative/path.ext"],
       "depends_on": ["t0"]
     }}
  ]
}}

Rules:
- Every file referenced in tasks MUST be listed in "files".
- Tasks MUST form a DAG (no cycles). Ordered so dependencies come first.
- Include a test file task when possible (pytest / jest / go test ...).
- Include a README.md task.
- Do NOT include build artifacts, lockfiles, or binary files.
- Keep it minimal and runnable. Aim for <= 25 files unless the project truly needs more.

Project structure conventions:
- Python: keep ONE canonical import layout and stick to it. Either put the
  application as a package `<name>/__init__.py` + `<name>/main.py`, OR put
  modules flat at the repo root (e.g. `app.py`, `models.py`). Do not mix.
  Tests import with the exact path you chose (`from <name>.main import app`
  or `from app import app`). The sandbox adds the workspace root to
  PYTHONPATH, so flat-at-root imports just work from `tests/`.
- Python: "install_command" should install pytest if tests are present
  (usually `pip install -r requirements.txt` with pytest listed there).
- Python: "test_command" should be `pytest -q` or similar.
- Node: ensure package.json declares a "test" script.

Return ONLY the JSON object.
"""


async def run_planner(router: OllamaRouter, user_prompt: str, stream_callback: Any | None = None) -> dict[str, Any]:
    raw = await router.generate(
        "planner",
        PLAN_PROMPT.format(prompt=user_prompt),
        system=PLAN_SYSTEM,
        json_mode=True,
        stream_callback=stream_callback,
    )
    data = extract_json(raw)
    if not isinstance(data, dict):
        raise ValueError("Planner returned non-object JSON")
    data.setdefault("files", [])
    data.setdefault("tasks", [])
    return data
