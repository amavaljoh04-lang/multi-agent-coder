"""Tests for config loading."""

from __future__ import annotations

from app.config import get_config


def test_config_has_three_servers():
    cfg = get_config()
    assert set(cfg.servers) == {"gpu5070", "gpu4060", "gpu3070"}
    for name, srv in cfg.servers.items():
        assert srv.url.startswith("http://"), name


def test_every_role_references_a_valid_server():
    cfg = get_config()
    for role, role_cfg in cfg.roles.items():
        for pick in role_cfg.candidates():
            assert pick.server in cfg.servers, f"{role} -> unknown server {pick.server}"
            assert pick.model, f"{role} has empty model"


def test_core_roles_present():
    cfg = get_config()
    for role in ("planner", "architect", "coder", "reviewer", "tester_analyst"):
        assert role in cfg.roles
