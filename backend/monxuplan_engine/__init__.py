"""MonxuPlan Planning Engine.

Pure, deterministic planning & scheduling engine: ``solve(Problem) -> Solution``.
No database, no HTTP — see docs/PLANNING_ENGINE.md.
"""

from .compile import ENGINE_VERSION
from .contract import Problem, Solution
from .pipeline import solve

__all__ = ["ENGINE_VERSION", "Problem", "Solution", "solve"]
