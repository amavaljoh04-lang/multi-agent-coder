"""Tester analyst: reads raw test output and distills what needs to be fixed."""

from __future__ import annotations

from typing import Any

from ..ollama_client import OllamaRouter
from .base import strip_reasoning

ANALYSIS_PROMPT = """\
A command was run to test a generated project. Here is the raw output.

Exit code: {exit_code}

STDOUT:
{stdout}

STDERR:
{stderr}

In 10 bullet points MAX, explain:
1. Which files almost certainly need to be modified.
2. What the root cause of the failure is (syntax, missing import, wrong logic, missing dep, flaky test, ...).
3. The smallest change that would likely make the tests pass.

Known project conventions (the sandbox already takes care of these — do NOT
suggest fixes that duplicate what already works):
- The workspace root is on PYTHONPATH, so `from <module_at_root> import X`
  WORKS from tests when `<module_at_root>.py` sits at the repo root.
- If a pytest run fails with `ModuleNotFoundError` or `ImportError` mentioning
  the application module, the fix is almost NEVER to keep rewriting the import
  statement. Instead, check:
    1. Does a file with that exact module name actually exist at the path the
       import expects? If not, rename the file OR adjust the import to match
       the real file layout (decide ONCE based on the file tree, do not toggle).
    2. Is the attribute (`app`, `main`, etc.) actually exported at the top
       level of that module?
    3. Does the package need a missing `__init__.py`?
  Stop flipping between `from X import app` and `from X.Y import app` —
  pick ONE that matches the real file structure and explain why.
- Missing dependencies: suggest adding them to requirements.txt / pyproject.toml
  rather than deleting the import.

Keep it short and technical. No fluff.
"""


async def run_tester_analyst(
    router: OllamaRouter,
    *,
    exit_code: int,
    stdout: str,
    stderr: str,
    stream_callback: Any | None = None,
) -> str:
    prompt = ANALYSIS_PROMPT.format(
        exit_code=exit_code,
        stdout=stdout[-6000:],
        stderr=stderr[-6000:],
    )
    text = await router.generate("tester_analyst", prompt, stream_callback=stream_callback)
    return strip_reasoning(text)
