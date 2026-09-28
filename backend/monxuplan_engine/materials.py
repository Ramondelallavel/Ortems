"""Material ledger: time-phased supply and demand per material.

The ledger never creates stock. A consumption of ``q`` at ``t`` is accepted only if the projected
level stays non-negative at every instant >= t, i.e. ``min_{t' >= t} level(t') >= q`` (earlier
allocations at later instants are protected). The earliest material availability for a requirement
is the smallest such ``t``.
"""

from __future__ import annotations

from bisect import bisect_right, insort
from dataclasses import dataclass, field

EPS = 1e-9


@dataclass(slots=True)
class LedgerEvent:
    time: int
    delta: float
    kind: str  # SUPPLY | CONSUMPTION
    ref: str  # supply id or op id
    meta: dict = field(default_factory=dict)

    def __lt__(self, other: LedgerEvent) -> bool:  # supplies first at equal time
        return (self.time, self.delta < 0) < (other.time, other.delta < 0)


class MaterialAccount:
    __slots__ = ("material", "events", "_levels", "_suffix_min", "_times", "_dirty")

    def __init__(self, material: int) -> None:
        self.material = material
        self.events: list[LedgerEvent] = []
        self._dirty = True
        self._levels: list[float] = []
        self._suffix_min: list[float] = []
        self._times: list[int] = []

    def add(self, ev: LedgerEvent) -> None:
        insort(self.events, ev)
        self._dirty = True

    def remove_ref(self, ref: str) -> None:
        before = len(self.events)
        self.events = [e for e in self.events if e.ref != ref]
        if len(self.events) != before:
            self._dirty = True

    def _refresh(self) -> None:
        if not self._dirty:
            return
        levels = []
        lvl = 0.0
        for e in self.events:
            lvl += e.delta
            levels.append(lvl)
        suffix = [0.0] * len(levels)
        m = float("inf")
        for i in range(len(levels) - 1, -1, -1):
            m = min(m, levels[i])
            suffix[i] = m
        self._levels = levels
        self._suffix_min = suffix
        self._times = [e.time for e in self.events]
        self._dirty = False

    def level_at(self, t: int) -> float:
        self._refresh()
        i = bisect_right(self._times, t) - 1
        return self._levels[i] if i >= 0 else 0.0

    def available_from(self, t: int) -> float:
        """min over t' >= t of the level — what can be consumed at t without hurting anyone."""
        self._refresh()
        if not self.events:
            return 0.0
        i = bisect_right(self._times, t) - 1
        cur = self._levels[i] if i >= 0 else 0.0
        nxt = self._suffix_min[i + 1] if i + 1 < len(self._suffix_min) else float("inf")
        return min(cur, nxt)

    def earliest(self, qty: float, t0: int) -> int | None:
        """Smallest t >= t0 with available_from(t) >= qty, or None if never within the ledger."""
        self._refresh()
        if self.available_from(t0) + EPS >= qty:
            return t0
        i = bisect_right(self._times, t0)
        n = len(self._times)
        while i < n:
            t = self._times[i]
            # advance to last event at the same instant
            while i + 1 < n and self._times[i + 1] == t:
                i += 1
            if self.available_from(t) + EPS >= qty:
                return t
            i += 1
        return None

    def max_available_after(self, t0: int) -> float:
        """Best achievable availability at or after t0 (for shortage reporting)."""
        self._refresh()
        best = self.available_from(t0)
        i = bisect_right(self._times, t0)
        while i < len(self._times):
            best = max(best, self.available_from(self._times[i]))
            i += 1
        return best

    def min_level(self) -> tuple[float, int | None]:
        self._refresh()
        if not self._levels:
            return 0.0, None
        k = min(range(len(self._levels)), key=lambda i: self._levels[i])
        return self._levels[k], self._times[k]


class MaterialLedger:
    def __init__(self, n_materials: int) -> None:
        self.accounts = [MaterialAccount(i) for i in range(n_materials)]

    def supply(self, mat: int, t: int, qty: float, key: str, **meta) -> None:
        self.accounts[mat].add(LedgerEvent(t, qty, "SUPPLY", key, meta))

    def consume(self, mat: int, t: int, qty: float, key: str, **meta) -> None:
        self.accounts[mat].add(LedgerEvent(t, -qty, "CONSUMPTION", key, meta))

    def earliest(self, mat: int, qty: float, t0: int) -> int | None:
        return self.accounts[mat].earliest(qty, t0)

    def remove_ref(self, mat: int, ref: str) -> None:
        self.accounts[mat].remove_ref(ref)

    def copy(self) -> MaterialLedger:
        new = MaterialLedger(0)
        new.accounts = []
        for a in self.accounts:
            b = MaterialAccount(a.material)
            b.events = list(a.events)
            new.accounts.append(b)
        return new


def fifo_pegging(account: MaterialAccount) -> list[tuple[LedgerEvent, LedgerEvent, float]]:
    """Match consumptions to supplies FIFO by time → (supply, consumption, qty).

    Consistent with the ledger's feasibility rule: if cumulative supply covers cumulative demand at
    every instant, FIFO matching always succeeds; any remainder is an uncovered shortage.
    """
    supplies = [[e, e.delta] for e in account.events if e.delta > 0]
    out: list[tuple[LedgerEvent, LedgerEvent, float]] = []
    si = 0
    for c in (e for e in account.events if e.delta < 0):
        need = -c.delta
        while need > EPS and si < len(supplies):
            s, left = supplies[si]
            take = min(left, need)
            if take > EPS:
                out.append((s, c, take))
                need -= take
                supplies[si][1] -= take
            if supplies[si][1] <= EPS:
                si += 1
    return out
