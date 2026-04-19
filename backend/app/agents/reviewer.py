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

Approve only if:
- All requested files are present.
- No placeholders, TODOs, or "..." left in the code.
- Imports / syntax look valid.
- Interfaces match the plan.

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
