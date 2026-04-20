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

STEP 0 (internal — do NOT include in output, but DO use to drive the plan):
Read the user request above and make a mental checklist of EVERY explicit
requirement. Break each sentence apart. Every feature, every subcommand,
every CLI flag (e.g. --store, --format, --json), every data field
(e.g. id, status, created_at), every file requested (README, tests,
requirements.txt), every behavior (ANSI colors, JSON persistence,
tmp_path isolation in tests) is a separate requirement. Do NOT merge
multiple requirements into one task.

Produce a JSON object with this exact schema:

{{
  "name": "short project name, slug-friendly",
  "summary": "one paragraph describing what will be built",
  "language": "primary language (python, node, go, rust, ...)",
  "run_command": "shell command to run the app or main script",
  "test_command": "shell command to run the tests (pytest, npm test, ...)",
  "install_command": "shell command to install deps (pip install -r requirements.txt, npm ci, ...)",
  "requirements_coverage": [
     {{
       "requirement": "verbatim quote or close paraphrase of a single user-stated requirement",
       "covered_by_task_ids": ["t3", "t7"]
     }}
  ],
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
- "requirements_coverage" MUST enumerate EVERY distinct requirement in the
  user prompt and map each to the task(s) that implement it. No requirement
  may be left uncovered. If the user listed 5 subcommands, you have at
  least 5 entries here. If the user listed a flag, it is its own entry.
  Producing fewer entries than there are requirements in the prompt is
  treated as a planning failure by the pipeline.
- Do NOT under-specify. If the user wrote "subcommands add, list, done, rm,
  clear", ALL FIVE must appear in tasks and coverage — not a subset.
- Copy "install_command" VERBATIM from the user prompt when the user
  specified one (e.g. "install_command: pip install pytest"). Do NOT
  drop it, do NOT leave it empty when the user wrote one.
- "files" MUST list EVERY file that the final project will contain, WITH NO
  EXCEPTION. This includes: source files, test files, requirements.txt /
  pyproject.toml / package.json, README.md, __init__.py for any package
  directory, config files. A file that is not in this list will NOT be
  created by the pipeline — missing files cause tests to fail for the wrong
  reason (ImportError) and waste the fixing loop.
- Every file referenced in a task MUST be listed in "files". Conversely,
  every file in "files" MUST be covered by at least one task's "file_paths".
- Tasks MUST form a DAG (no cycles). Ordered so dependencies come first.
- Tests: include them when the project has ANY logic worth testing
  (functions that take inputs and return outputs, classes with state,
  HTTP endpoints, CLI subcommands, parsers, ...). For a TRULY trivial
  script (hello world, single print, one-shot glue code), tests are
  optional and you may leave "test_command" empty ("") so the pipeline
  skips the test phase entirely. DO NOT invent useless tests just to
  have tests.
- For a trivial script that has no tests, set "test_command" to an
  import-check instead of pytest: `python -c "import <main_module>"`
  (substitute the actual module). This validates the file parses and
  imports cleanly without forcing a test suite.
- When tests ARE included: tests run only AFTER all tasks are done and
  ALL listed files exist on disk, so tests can freely import from any
  other listed file.
- ALWAYS include a README.md task and a dependency-manifest task
  (requirements.txt or equivalent) EXCEPT for a single-file script
  with zero dependencies, where requirements.txt can be skipped.
- Do NOT include build artifacts, lockfiles, or binary files.
- Keep it minimal and runnable. Aim for <= 25 files unless the project truly needs more.

Project structure conventions:
- Python: keep ONE canonical import layout and stick to it. Either put the
  application as a package `<name>/__init__.py` + `<name>/main.py`, OR put
  modules flat at the repo root (e.g. `app.py`, `models.py`). Do not mix.
  Tests import with the exact path you chose (`from <name>.main import app`
  or `from app import app`). The sandbox adds the workspace root to
  PYTHONPATH, so flat-at-root imports just work from `tests/`.
- Python: "install_command" should install pytest only if tests are
  present (usually `pip install -r requirements.txt` with pytest
  listed there). For a single-file no-deps script, leave
  "install_command" empty.
- Python: "test_command" should be `pytest -q` when tests are
  present, OR `python -c "import <module>"` for a trivial script,
  OR "" to skip the test phase entirely.
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
