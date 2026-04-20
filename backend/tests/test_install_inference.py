"""Unit tests for the install-command inference helper."""

from __future__ import annotations

from pathlib import Path

from app.orchestrator import _infer_install_command


def test_pytest_auto_installed_when_missing(tmp_path: Path) -> None:
    # Planner lost install_command, test_command asks for pytest.
    cmd = _infer_install_command("", "pytest -q", tmp_path)
    assert "pip install" in cmd
    assert "pytest" in cmd


def test_pytest_not_installed_twice(tmp_path: Path) -> None:
    # User's install_command already installs pytest.
    cmd = _infer_install_command("pip install pytest", "pytest -q", tmp_path)
    assert cmd.count("pip install") == 1


def test_requirements_used_when_non_empty(tmp_path: Path) -> None:
    (tmp_path / "requirements.txt").write_text("click==8.1.7\n", encoding="utf-8")
    cmd = _infer_install_command("", "pytest -q", tmp_path)
    assert "requirements.txt" in cmd
    # pytest also needed, should be added too
    assert "pytest" in cmd


def test_requirements_ignored_when_only_comments(tmp_path: Path) -> None:
    (tmp_path / "requirements.txt").write_text(
        "# no runtime deps\n# just stdlib\n", encoding="utf-8"
    )
    cmd = _infer_install_command("", "pytest -q", tmp_path)
    assert "requirements.txt" not in cmd
    assert "pytest" in cmd


def test_no_tests_no_install(tmp_path: Path) -> None:
    cmd = _infer_install_command("", "python -m foo", tmp_path)
    assert cmd == ""


def test_preserves_user_install_command(tmp_path: Path) -> None:
    cmd = _infer_install_command("pip install click rich", "pytest -q", tmp_path)
    assert "pip install click rich" in cmd
    assert "pytest" in cmd
