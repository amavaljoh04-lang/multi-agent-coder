"""Architect agent: refines the plan with detailed interfaces per file."""

from __future__ import annotations

import json
from typing import Any

from ..ollama_client import OllamaRouter
from .base import PLAN_SYSTEM, extract_json

ARCH_PROMPT = """\
Project plan (JSON):
---
{plan}
---

For each file in "files", produce a detailed spec describing its public
interface, key functions/classes, and how it integrates with the other files.
Return JSON:

{{
  "files": [
    {{"path": "...", "spec": "detailed technical spec in markdown"}}
  ]
}}

Keep specs concrete: function signatures, return types, error cases. No prose
outside the JSON. Return ONLY the JSON object.
"""


async def run_architect(router: OllamaRouter, plan: dict[str, Any], stream_callback: Any | None = None) -> dict[str, Any]:
    raw = await router.generate(
        "architect",
        ARCH_PROMPT.format(plan=json.dumps(plan, indent=2, ensure_ascii=False)),
        system=PLAN_SYSTEM,
        json_mode=True,
        stream_callback=stream_callback,
    )
    data = extract_json(raw)
    if not isinstance(data, dict):
        raise ValueError("Architect returned non-object JSON")
    return data
