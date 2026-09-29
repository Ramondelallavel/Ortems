"""Quantity policy of the engine (one place, used by the ledger, the validator and pegging).

Material quantities (stock, receipts, consumptions, production, scrap-inflated requirements) are
accounted in **integer micro-units**: ``units = round(quantity × 10⁶)``. Sums and comparisons of
integers are exact, so feasibility never depends on floating-point accumulation: 0.1 + 0.2 received
covers 0.3 required; a level of −10⁻¹² is not a shortage; 10⁶ movements of 0.001 add up exactly.
Six decimals are below any industrial unit of measure (a gram in tonnes, a millimetre in km).

Floats remain where no exact decision depends on them: durations are converted to whole minutes with
a documented tolerance (``durations.MINUTE_EPS``), and costs and KPI values are reporting figures.
"""

from __future__ import annotations

SCALE = 1_000_000
DECIMALS = 6


def to_units(quantity: float) -> int:
    """Quantity → integer micro-units (rounded half to even at the 7th decimal)."""
    return round(quantity * SCALE)


def from_units(units: int) -> float:
    return units / SCALE


def qty_round(quantity: float) -> float:
    """A quantity as the ledger sees it (for display next to ledger figures)."""
    return round(quantity, DECIMALS)
