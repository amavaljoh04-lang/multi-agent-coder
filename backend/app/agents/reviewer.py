"""Reviewer agent: sanity-checks coder output."""

from __future__ import annotations

import json
from typing import Any

from ..ollama_client import OllamaRouter
from .base import REVIEW_SYSTEM, extract_json

REVIEW_PROMPT = """\
Project plan (JSON):
---
{plan}
---

Task just completed:
Title: {title}
Description: {description}

Files produced (review each one):
{files_block}

Return JSON:

{{
  "approved": true | false,
  "issues": ["list of concrete issues to fix, one per item"],
  "notes": "short overall comment"
}}

REJECT ONLY for objective correctness problems:
- A requested file is missing.
- A file still contains placeholders, TODOs, "..." or stub bodies like `pass`.
- Imports or syntax are clearly broken.
- A declared function / class / interface from the plan is missing or has a wrong signature.
- A critical bug (obvious logic error, unreachable code, wrong return type).

DO NOT reject for subjective or cosmetic reasons. In particular, do NOT reject because:
- Documentation is "incomplete" (missing Contributing / Usage / Examples sections).
- The language of comments or docs is French instead of English (or vice versa).
- Formatting, naming preferences, style choices, or comment density.
- You personally would have structured the code differently.
- The README could be "more detailed".

If only cosmetic/stylistic issues remain, approve. Tests will catch the rest.

Return ONLY the JSON object.
"""


def _fmt_files(files: dict[str, str], limit_chars: int = 40000) -> str:
    out = []
    used = 0
    for path, content in files.items():
        header = f"\n--- {path} ---\n"
        chunk = header + content
        if used + len(chunk) > limit_chars:
            out.append(f"\n--- {path} --- [truncated]\n")
            used += 40
            continue
        out.append(chunk)
        used += len(chunk)
    return "".join(out)


async def run_reviewer(
    router: OllamaRouter,
    *,
    plan: dict[str, Any],
    task: dict[str, Any],
    files: dict[str, str],
    stream_callback: Any | None = None,
) -> dict[str, Any]:
    prompt = REVIEW_PROMPT.format(
        plan=json.dumps(plan, indent=2, ensure_ascii=False),
        title=task.get("title", ""),
        description=task.get("description", ""),
        files_block=_fmt_files(files),
    )
    raw = await router.generate(
        "reviewer", prompt, system=REVIEW_SYSTEM, json_mode=True, stream_callback=stream_callback
    )
    data = extract_json(raw)
    if not isinstance(data, dict):
        raise ValueError("Reviewer returned non-object JSON")
    data.setdefault("approved", False)
    data.setdefault("issues", [])
    data.setdefault("notes", "")
    return data
