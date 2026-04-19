"""Coder agent: writes full files, one task at a time."""

from __future__ import annotations

import json
from typing import Any

from ..ollama_client import OllamaRouter
from .base import CODE_SYSTEM, extract_code_blocks

CODE_PROMPT = """\
Project context (JSON):
---
{plan}
---

Current task:
Title: {title}
Description:
{description}

Files to produce in this task (write EVERY one, fully, no placeholders):
{file_list}

{existing_files_block}

{review_notes_block}

Output format — one fenced block per file, header MUST be `path=<relative path>`:

```path=relative/path.ext
<full file content>
```

Rules:
- Output EVERY listed file, even if small.
- No prose outside fenced blocks.
- No "..." or "TODO". Code must be complete and runnable.
- Respect imports / interfaces declared elsewhere in the project.
"""

FIX_PROMPT = """\
You are the FIXER. Your ONLY job is to apply the SMALLEST possible change to
make the failing tests pass. You are NOT allowed to redesign, rewrite from
scratch, or "improve" anything that isn't directly related to the failure.

Project context (JSON):
---
{plan}
---

We ran the test command and it FAILED. Here is the output:

STDOUT:
{stdout}

STDERR:
{stderr}

Analyst notes (these identify the root cause — trust them):
{analysis}

Current files (only those likely to need changes):
{existing_files_block}

Output format — one fenced block per file you modify:

```path=relative/path.ext
<full updated file content>
```

ABSOLUTE RULES (violate any of these and the fix is wrong):
1. Touch the FEWEST possible files. Ideally 1. Never more than 3 unless the
   analyst explicitly says so.
2. Preserve EVERYTHING unrelated to the failure: function signatures, public
   APIs, docstrings, comments, style, ordering of imports, existing tests.
3. Do NOT rewrite a file from scratch. Copy the current content verbatim and
   change ONLY the lines that fix the error.
4. Do NOT flip between import layouts between iterations. Look at the actual
   file tree above, decide ONCE what the canonical layout is, and stick to it
   on every future fix.
5. The sandbox adds the workspace root to PYTHONPATH. A flat `module.py` at
   the root imports as `import module`. Do NOT add sys.path hacks,
   conftest.py, or __init__.py just to make imports work.
6. Missing dependency → add it to requirements.txt (or package.json /
   Cargo.toml). Do NOT delete the import.
7. Do NOT delete tests to make them pass. If a test is wrong, say so in a
   comment, but prefer fixing the code it tests.
8. No prose outside fenced blocks. Complete files only, not diffs.
"""


def _fmt_existing(files: dict[str, str], limit_chars: int = 40000) -> str:
    if not files:
        return ""
    out = ["Existing relevant files:"]
    used = 0
    for path, content in files.items():
        header = f"\n--- {path} ---\n"
        chunk = header + content
        if used + len(chunk) > limit_chars:
            out.append(f"\n--- {path} --- [truncated due to context size]\n")
            used += 80
            continue
        out.append(chunk)
        used += len(chunk)
    return "".join(out)


async def run_coder(
    router: OllamaRouter,
    *,
    plan: dict[str, Any],
    task: dict[str, Any],
    existing_files: dict[str, str],
    review_notes: str = "",
    role: str = "coder",
    stream_callback: Any | None = None,
) -> dict[str, str]:
    file_list = "\n".join(f"- {p}" for p in task.get("file_paths", []))
    prompt = CODE_PROMPT.format(
        plan=json.dumps(plan, indent=2, ensure_ascii=False),
        title=task.get("title", ""),
        description=task.get("description", ""),
        file_list=file_list or "(see description)",
        existing_files_block=_fmt_existing(existing_files),
        review_notes_block=(f"Reviewer asked for these changes:\n{review_notes}" if review_notes else ""),
    )
    text = await router.generate(role, prompt, system=CODE_SYSTEM, stream_callback=stream_callback)
    blocks = extract_code_blocks(text)
    if not blocks:
        raise ValueError("Coder did not produce any fenced file blocks")
    return blocks


async def run_fix(
    router: OllamaRouter,
    *,
    plan: dict[str, Any],
    stdout: str,
    stderr: str,
    analysis: str,
    existing_files: dict[str, str],
    stream_callback: Any | None = None,
) -> dict[str, str]:
    prompt = FIX_PROMPT.format(
        plan=json.dumps(plan, indent=2, ensure_ascii=False),
        stdout=stdout[-8000:],
        stderr=stderr[-8000:],
        analysis=analysis,
        existing_files_block=_fmt_existing(existing_files, limit_chars=60000),
    )
    text = await router.generate("fixer", prompt, system=CODE_SYSTEM, stream_callback=stream_callback)
    blocks = extract_code_blocks(text)
    if not blocks:
        raise ValueError("Fixer did not produce any fenced file blocks")
    return blocks
