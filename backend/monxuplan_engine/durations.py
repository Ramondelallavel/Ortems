"""Operation duration model.

``run(q)`` combines the per-unit rate (optionally tiered by lot size — the rate of the highest tier
whose ``min_quantity`` is <= q applies to the whole quantity), a fixed time and a batch time
(``ceil(q / batch_size) × minutes_per_batch``). The result is divided by the resource efficiency and
the mode speed factor and rounded **up** to whole minutes (never optimistic).
"""

from __future__ import annotations

import math

from .contract import DurationSpec

EPS = 1e-9


def rate_for_quantity(d: DurationSpec, qty: float, override: float | None = None) -> float:
    if override is not None:
        return override
    rate = d.run_minutes_per_unit
    best = -1.0
    for tier in d.run_tiers:
        if tier.min_quantity <= qty + EPS and tier.min_quantity > best:
            best = tier.min_quantity
            rate = tier.minutes_per_unit
    return rate


def run_minutes(
    d: DurationSpec,
    qty: float,
    efficiency: float = 1.0,
    speed_factor: float = 1.0,
    rate_override: float | None = None,
) -> int:
    rate = rate_for_quantity(d, qty, rate_override)
    raw = qty * rate + d.fixed_minutes
    if d.batch_size:
        raw += math.ceil(qty / d.batch_size - EPS) * d.minutes_per_batch
    eff = max(efficiency * speed_factor, EPS)
    return max(0, math.ceil(raw / eff - EPS))


def whole(minutes: float) -> int:
    return max(0, math.ceil(minutes - EPS))
