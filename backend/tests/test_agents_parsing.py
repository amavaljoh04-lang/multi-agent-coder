"""Tests for the lightweight parsing helpers in agents/base.py.

These are pure-function tests, no network, no GPUs.
"""

from __future__ import annotations

import pytest

from app.agents.base import extract_code_blocks, extract_json, strip_reasoning


def test_strip_reasoning_removes_think_block():
    text = "<think>lots of reasoning</think>\n```json\n{\"a\": 1}\n```"
    assert "<think>" not in strip_reasoning(text)


def test_extract_json_from_fenced_block():
    text = "bla bla\n```json\n{\"ok\": true, \"n\": 3}\n```\ntrailing"
    assert extract_json(text) == {"ok": True, "n": 3}


def test_extract_json_from_raw_object():
    text = "prefix {\"a\": [1, 2, 3], \"b\": \"x\"} suffix"
    assert extract_json(text) == {"a": [1, 2, 3], "b": "x"}


def test_extract_json_lenient_trailing_comma():
    text = '{"a": 1, "b": 2,}'
    assert extract_json(text) == {"a": 1, "b": 2}


def test_extract_json_raises_when_empty():
    with pytest.raises(ValueError):
        extract_json("there is no json here at all")


def test_extract_code_blocks_with_path_header():
    text = """
some prose
```path=src/app/main.py
print("hi")
```
more prose
```path=README.md
# hello
```
"""
    blocks = extract_code_blocks(text)
    assert set(blocks) == {"src/app/main.py", "README.md"}
    assert 'print("hi")' in blocks["src/app/main.py"]
    assert blocks["README.md"].startswith("# hello")


def test_extract_code_blocks_ignores_bare_language_blocks():
    # A plain ``` python fenced block without a path should not be picked up.
    text = "```python\nprint(1)\n```"
    assert extract_code_blocks(text) == {}
