"""Agent prompts and helpers.

Each agent is just a prompt + parsing strategy around the Ollama router.
Agents are deliberately thin so the orchestrator controls state.
"""

from .architect import run_architect
from .coder import run_coder, run_fix, run_static_fix
from .planner import run_planner
from .reviewer import run_reviewer
from .tester import run_tester_analyst

__all__ = [
    "run_architect",
    "run_coder",
    "run_fix",
    "run_planner",
    "run_reviewer",
    "run_static_fix",
    "run_tester_analyst",
]
