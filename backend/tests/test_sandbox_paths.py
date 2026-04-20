"""Smoke tests for sandbox path safety."""

from __future__ import annotations

from app.orchestrator import _safe_relpath


def test_rejects_absolute():
    assert _safe_relpath("/etc/passwd") == "etc/passwd"  # leading slash stripped, still safe relative


def test_rejects_traversal():
    assert _safe_relpath("../../etc/passwd") is None


def test_accepts_normal_path():
    assert _safe_relpath("src/app/main.py") == "src/app/main.py"


def test_rejects_empty():
    assert _safe_relpath("   ") is None
