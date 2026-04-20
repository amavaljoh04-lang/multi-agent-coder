"""Configuration loader.

Reads ``config.yaml`` at startup and exposes the server / role matrix as
typed Pydantic models so the rest of the code doesn't deal with raw dicts.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import yaml
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class ServerConfig(BaseModel):
    url: str
    label: str = ""
    concurrency: int = 2


class ModelPick(BaseModel):
    server: str
    model: str


class RoleConfig(BaseModel):
    primary: ModelPick
    fallbacks: list[ModelPick] = Field(default_factory=list)
    temperature: float = 0.2
    num_ctx: int = 8192
    # Maximum tokens the model may emit in a single response. Ollama's
    # default is only 128 which truncates anything longer than a few lines
    # (e.g. a full README or a multi-file Coder response). 4096 is a safe
    # upper bound for single-file generation; the orchestrator clamps
    # total usage via num_ctx and per-role timeouts.
    num_predict: int = 4096

    def candidates(self) -> list[ModelPick]:
        return [self.primary, *self.fallbacks]


class OrchestratorConfig(BaseModel):
    max_iterations: int = -1
    max_review_retries: int = 5
    heartbeat_interval: int = 30
    sandbox: str = "docker"
    sandbox_image: str = "python:3.12-slim"
    sandbox_timeout: int = 600
    # Number of coder tasks that may run in parallel. We have 3 GPUs, so 2-3
    # concurrent coders is the sweet spot: independent tasks (e.g. models vs
    # endpoints) get coded simultaneously, dividing wall-clock time roughly
    # by this factor.
    parallel_coders: int = 2
    # Absolute wall-clock budget for a single project, in seconds. -1 = unlimited.
    # Lets the orchestrator keep iterating for days on hard projects without a
    # hardcoded max_iterations cap. The project is failed with a clear message
    # when the budget is exceeded.
    max_wall_seconds: int = -1


class AppConfig(BaseModel):
    servers: dict[str, ServerConfig]
    roles: dict[str, RoleConfig]
    orchestrator: OrchestratorConfig = Field(default_factory=OrchestratorConfig)

    def server_url(self, name: str) -> str:
        if name not in self.servers:
            raise KeyError(f"Unknown server '{name}' in config")
        return self.servers[name].url


class Settings(BaseSettings):
    """Runtime env overrides."""

    model_config = SettingsConfigDict(env_file=".env", env_prefix="MAC_", extra="ignore")

    config_path: str = "config.yaml"
    data_dir: str = "data"
    workspaces_dir: str = "workspaces"
    zips_dir: str = "zips"
    host: str = "0.0.0.0"
    port: int = 5555
    db_url: str = "sqlite+aiosqlite:///data/mac.db"


def _find_config_file(path: str) -> Path:
    """Locate config.yaml relative to CWD or repo root."""
    p = Path(path)
    if p.exists():
        return p
    here = Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent / path
        if candidate.exists():
            return candidate
    raise FileNotFoundError(f"Could not find config file '{path}'")


@lru_cache
def get_settings() -> Settings:
    return Settings()


@lru_cache
def get_config() -> AppConfig:
    settings = get_settings()
    path = _find_config_file(settings.config_path)
    with path.open("r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh)
    return AppConfig.model_validate(raw)
