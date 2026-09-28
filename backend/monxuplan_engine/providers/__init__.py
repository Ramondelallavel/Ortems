"""Optimisation providers registry."""

from __future__ import annotations

from .base import OptimizationProvider

_REGISTRY: dict[str, type[OptimizationProvider]] = {}


def register(name: str, cls: type[OptimizationProvider]) -> None:
    _REGISTRY[name] = cls


def get_provider(name: str) -> OptimizationProvider:
    _ensure_builtin()
    try:
        return _REGISTRY[name]()
    except KeyError as exc:
        raise ValueError(f"unknown optimisation provider {name!r}; available: {sorted(_REGISTRY)}") from exc


def available() -> list[str]:
    _ensure_builtin()
    return sorted(_REGISTRY)


def _ensure_builtin() -> None:
    if _REGISTRY:
        return
    from .heuristic import HeuristicProvider

    register("heuristic", HeuristicProvider)
    try:
        from .cpsat import CpSatProvider
        from .hybrid import HybridProvider

        register("cpsat", CpSatProvider)
        register("hybrid", HybridProvider)
    except ImportError:  # pragma: no cover - OR-Tools not installed
        pass
    try:
        from .mip import MipProvider

        register("mip", MipProvider)
    except ImportError:  # pragma: no cover
        pass
