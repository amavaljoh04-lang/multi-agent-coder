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
