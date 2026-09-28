"""Optimisation provider abstraction.

A provider receives the compiled problem and returns *decisions* materialised by the exact schedule
builder, so every provider's output satisfies the HARD constraints by construction and is evaluated
with the same objective function.
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from ..builder import BuildConfig, BuildResult, build
from ..compile import CompiledProblem
from ..objectives import better, components, scales, vector
from ..timing import Timing

ProgressFn = Callable[[str, float | None, str | None], None]


@dataclass
class SolveContext:
    time_limit_s: float
    seed: int = 42
    progress: ProgressFn | None = None
    cancelled: Callable[[], bool] | None = None
    started: float = field(default_factory=time.monotonic)
    messages: list[str] = field(default_factory=list)

    @property
    def deadline(self) -> float:
        return self.started + self.time_limit_s

    def remaining(self) -> float:
        return max(0.0, self.deadline - time.monotonic())

    def expired(self) -> bool:
        return time.monotonic() >= self.deadline or bool(self.cancelled and self.cancelled())

    def report(self, step: str, fraction: float | None = None, detail: str | None = None) -> None:
        if self.progress:
            self.progress(step, fraction, detail)


@dataclass
class ProviderResult:
    result: BuildResult
    status: str  # OPTIMAL | FEASIBLE | HEURISTIC | NO_SOLUTION
    components: dict[str, float]
    vector: list[float]
    best_bound: float | None = None
    gap: float | None = None
    proven_optimal: bool = False
    iterations: int = 0
    details: dict[str, Any] = field(default_factory=dict)


@dataclass
class ProviderCapabilities:
    detailed_scheduling: bool = True
    aggregate_planning: bool = False
    proves_optimality: bool = False
    max_recommended_ops: int | None = None


class Evaluator:
    """Shared objective evaluation (weighted or lexicographic) with fixed normalisation scales."""

    def __init__(self, cp: CompiledProblem, reference: dict[str, float] | None = None) -> None:
        self.cp = cp
        self.spec = cp.objectives
        self.scale = scales(reference or {})
        self.tol = cp.objectives.tolerance

    def set_reference(self, comp: dict[str, float]) -> None:
        self.scale = scales(comp)

    def evaluate(self, res: BuildResult) -> tuple[dict[str, float], list[float]]:
        comp = components(self.cp, res.placements, res.unscheduled)
        return comp, vector(self.spec, comp, self.scale)

    def better(self, a: list[float], b: list[float]) -> bool:
        return better(a, b, self.tol)


class OptimizationProvider(ABC):
    name: str = "abstract"

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities()

    @abstractmethod
    def solve(self, cp: CompiledProblem, timing: Timing, ctx: SolveContext) -> ProviderResult: ...


def decode(cp: CompiledProblem, timing: Timing, *, rules=None, priority=None, forced=None, chains=None, explain=False, mode_selection=None, targets=None) -> BuildResult:
    cfg = BuildConfig(
        rules=tuple(rules or cp.solver.dispatch_rules),
        priority=priority,
        forced_modes=forced,
        resource_chains=chains,
        explain=explain,
        mode_selection=mode_selection or cp.solver.mode_selection,
        direction=cp.solver.direction,
        targets=targets,
    )
    return build(cp, cfg, timing)


def decisions_of(res: BuildResult) -> tuple[dict[int, float], dict[int, int]]:
    """Priority (by setup start) and modes of a built schedule — used to re-decode it."""
    prio: dict[int, float] = {}
    modes: dict[int, int] = {}
    for p in res.placements:
        if p is None:
            continue
        prio[p.op] = p.setup_start + p.op * 1e-6
        modes[p.op] = p.mode
    return prio, modes
