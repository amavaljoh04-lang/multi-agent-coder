"""Shared helpers for agents: robust JSON extraction, prompt blocks."""

from __future__ import annotations

import json
import re
from typing import Any

_JSON_BLOCK = re.compile(r"```(?:json)?\s*(\{.*?\}|\[.*?\])\s*```", re.DOTALL)
_FIRST_BRACE = re.compile(r"(\{.*\}|\[.*\])", re.DOTALL)
_THINK_BLOCK = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)


def strip_reasoning(text: str) -> str:
    """Remove <think>...</think> blocks emitted by deepseek-r1 style models."""
    return _THINK_BLOCK.sub("", text).strip()


def extract_json(text: str) -> Any:
    """Find the JSON payload inside a free-form model response.

    We try ``json`` fenced blocks first, then the largest brace/bracket run.
    Raises ``ValueError`` if nothing parseable is found.
    """
    cleaned = strip_reasoning(text)
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
            # Try to be lenient: strip trailing commas.
            fixed = re.sub(r",(\s*[}\]])", r"\1", snippet)
            return json.loads(fixed)
    raise ValueError("No JSON payload found in model response")


def extract_code_blocks(text: str) -> dict[str, str]:
    """Extract ```path\\ncontent``` fenced blocks into {path: content}.

    The model is asked to emit blocks of the shape::

        ```path=src/foo.py
        ...file content...
        ```

    We also accept ``path: src/foo.py`` and plain language tags when the
    filename is obvious from the surrounding prompt (handled by the caller).
    """
    blocks: dict[str, str] = {}
    # Require ``path=...`` or ``path:...`` headers so that plain ``` python
    # fenced blocks from reasoning models don't get picked up as fake files.
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


PLAN_SYSTEM = (
    "You are a senior software architect. "
    "You decompose user project requests into a concrete set of files and "
    "atomic implementation tasks that together deliver EVERY requirement the "
    "user listed — not a minimal MVP, not a subset. "
    "If the user enumerates features, flags, fields, or subcommands, each one "
    "MUST be covered by at least one task. Under-specifying the plan forces "
    "the pipeline to ship a half-finished project. "
    "Output ONLY valid JSON, no prose, no markdown, no <think> blocks leaking."
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
