"""Material ledger: time-phased supply and demand per material.

The ledger never creates stock. A consumption of ``q`` at ``t`` is accepted only if the projected
level stays non-negative at every instant >= t, i.e. ``min_{t' >= t} level(t') >= q`` (earlier
allocations at later instants are protected). The earliest material availability for a requirement
is the smallest such ``t``.

Quantities are kept in exact integer micro-units (``quantities.py``): every decision (available or
not, shortage or not) is an integer comparison, never a floating-point one with a tolerance.
"""

from __future__ import annotations

from bisect import bisect_right, insort
from dataclasses import dataclass, field

from .quantities import from_units, to_units

EPS = 1e-9  # kept for callers that compare reported float quantities; the ledger itself is exact


@dataclass(slots=True)
class LedgerEvent:
    time: int
    units: int  # signed quantity in micro-units: > 0 supply, < 0 consumption
    kind: str  # SUPPLY | CONSUMPTION
    ref: str  # supply id or op id
    meta: dict = field(default_factory=dict)

    @property
    def delta(self) -> float:
        return from_units(self.units)

    def __lt__(self, other: LedgerEvent) -> bool:  # supplies first at equal time
        return (self.time, self.units < 0) < (other.time, other.units < 0)


class MaterialAccount:
    """Time-ordered events of one material, kept in chunks of ~``CHUNK`` events.

    Each chunk stores its total and the minimum of its local running level, so the level at an instant
    and the minimum level after it are answered in O(sqrt n) and an insertion costs O(chunk size) — a
    material with 100 000 movements stays fast (a flat prefix array would be recomputed on every
    allocation: quadratic). All sums are integers (micro-units)."""

    __slots__ = ("material", "_chunks", "_keys", "_sum", "_min", "_flat")

    CHUNK = 64

    def __init__(self, material: int) -> None:
        self.material = material
        self._chunks: list[list[LedgerEvent]] = []
        self._keys: list[tuple[int, bool]] = []  # first event key of each chunk
        self._sum: list[int] = []
        self._min: list[int] = []  # min running level inside the chunk, relative to its start
        self._flat: list[LedgerEvent] | None = []

    # ---------------------------------------------------------------- maintenance
    @staticmethod
    def _key(e: LedgerEvent) -> tuple[int, bool]:
        return (e.time, e.units < 0)

    def _stats(self, c: int) -> None:
        lvl = 0
        m = None
        for e in self._chunks[c]:
            lvl += e.units
            if m is None or lvl < m:
                m = lvl
        self._sum[c] = lvl
        self._min[c] = 0 if m is None else m
        self._keys[c] = self._key(self._chunks[c][0])

    def add(self, ev: LedgerEvent) -> None:
        self._flat = None
        if not self._chunks:
            self._chunks.append([ev])
            self._keys.append(self._key(ev))
            self._sum.append(0)
            self._min.append(0)
            self._stats(0)
            return
        c = max(bisect_right(self._keys, self._key(ev)) - 1, 0)
        insort(self._chunks[c], ev)
        if len(self._chunks[c]) > 2 * self.CHUNK:
            ch = self._chunks[c]
            half = len(ch) // 2
            self._chunks[c : c + 1] = [ch[:half], ch[half:]]
            self._keys[c : c + 1] = [(0, False), (0, False)]
            self._sum[c : c + 1] = [0, 0]
            self._min[c : c + 1] = [0, 0]
            self._stats(c)
            self._stats(c + 1)
        else:
            self._stats(c)

    def remove_ref(self, ref: str) -> None:
        evs = [e for e in self.events if e.ref != ref]
        if len(evs) != len(self.events):
            self._rebuild(evs)

    def _rebuild(self, evs: list[LedgerEvent]) -> None:
        self._chunks, self._keys, self._sum, self._min = [], [], [], []
        for i in range(0, len(evs), self.CHUNK):
            self._chunks.append(evs[i : i + self.CHUNK])
            self._keys.append((0, False))
            self._sum.append(0)
            self._min.append(0)
            self._stats(len(self._chunks) - 1)
        self._flat = list(evs)

    @property
    def events(self) -> list[LedgerEvent]:
        if self._flat is None:
            self._flat = [e for ch in self._chunks for e in ch]
        return self._flat

    @events.setter
    def events(self, evs: list[LedgerEvent]) -> None:
        self._rebuild(sorted(evs))

    # ---------------------------------------------------------------- queries (micro-units)
    def _position(self, t: int) -> tuple[int, int, int]:
        """(chunk, index in chunk, level) of the last event at or before t; chunk -1 if none."""
        c = bisect_right(self._keys, (t, True)) - 1
        if c < 0:
            return -1, -1, 0
        lvl = sum(self._sum[:c])
        idx = -1
        for i, e in enumerate(self._chunks[c]):
            if e.time > t:
                break
            lvl += e.units
            idx = i
        return c, idx, lvl

    def available_units(self, t: int) -> int:
        """min over t' >= t of the level — what can be consumed at t without hurting anyone."""
        if not self._chunks:
            return 0
        c, idx, cur = self._position(t)
        best = cur
        if c < 0:
            c, idx, lvl = 0, -1, 0
        else:
            lvl = cur
        for e in self._chunks[c][idx + 1 :]:
            lvl += e.units
            if lvl < best:
                best = lvl
        for k in range(c + 1, len(self._chunks)):
            m = lvl + self._min[k]
            if m < best:
                best = m
            lvl += self._sum[k]
        return best

    # ---------------------------------------------------------------- queries (quantities)
    def level_at(self, t: int) -> float:
        return from_units(self._position(t)[2])

    def available_from(self, t: int) -> float:
        return from_units(self.available_units(t))

    def earliest(self, qty: float, t0: int) -> int | None:
        """Smallest t >= t0 with available(t) >= qty, or None if never within the ledger.

        Availability is non-decreasing in t, so the candidates (t0 and later event times) are
        searched with a binary search. Exact integer comparison."""
        need = to_units(qty)
        if self.available_units(t0) >= need:
            return t0
        times = [e.time for e in self.events]
        lo = bisect_right(times, t0)
        if lo >= len(times) or self.available_units(times[-1]) < need:
            return None
        hi = len(times) - 1
        while lo < hi:
            mid = (lo + hi) // 2
            if self.available_units(times[mid]) >= need:
                hi = mid
            else:
                lo = mid + 1
        return times[lo]

    def max_available_after(self, t0: int) -> float:
        """Best achievable availability at or after t0 (for shortage reporting): availability is
        non-decreasing, so it is its value at the last event (or at t0)."""
        evs = self.events
        if not evs:
            return 0.0
        return from_units(max(self.available_units(t0), self.available_units(max(evs[-1].time, t0))))

    def min_level_units(self) -> tuple[int, int | None]:
        lvl = 0
        best, at = None, None
        for e in self.events:
            lvl += e.units
            if best is None or lvl < best:
                best, at = lvl, e.time
        return (0, None) if best is None else (best, at)

    def min_level(self) -> tuple[float, int | None]:
        best, at = self.min_level_units()
        return from_units(best), at


class MaterialLedger:
    def __init__(self, n_materials: int) -> None:
        self.accounts = [MaterialAccount(i) for i in range(n_materials)]
        self._pending: dict[int, list[LedgerEvent]] | None = None

    def _add(self, mat: int, ev: LedgerEvent) -> None:
        if self._pending is not None:
            self._pending.setdefault(mat, []).append(ev)
        else:
            self.accounts[mat].add(ev)

    def supply(self, mat: int, t: int, qty: float, key: str, **meta) -> None:
        self._add(mat, LedgerEvent(t, to_units(qty), "SUPPLY", key, meta))

    def consume(self, mat: int, t: int, qty: float, key: str, **meta) -> None:
        self._add(mat, LedgerEvent(t, -to_units(qty), "CONSUMPTION", key, meta))

    def begin_bulk(self) -> None:
        """Collect movements without querying the ledger (placing 200 000 fixed operations): they
        are sorted into the accounts once by :meth:`end_bulk`. A stable sort of the existing events
        followed by the new ones in arrival order gives the order one-by-one insertion gives."""
        self._pending = {}

    def end_bulk(self) -> None:
        pending, self._pending = self._pending, None
        for mat, evs in (pending or {}).items():
            acc = self.accounts[mat]
            acc.events = acc.events + evs

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
    every instant, FIFO matching always succeeds; any remainder is an uncovered shortage. Exact
    (micro-units); quantities are returned as floats for reporting."""
    supplies = [[e, e.units] for e in account.events if e.units > 0]
    out: list[tuple[LedgerEvent, LedgerEvent, float]] = []
    si = 0
    for c in (e for e in account.events if e.units < 0):
        need = -c.units
        while need > 0 and si < len(supplies):
            s, left = supplies[si]
            take = min(left, need)
            if take > 0:
                out.append((s, c, from_units(take)))
                need -= take
                supplies[si][1] -= take
            if supplies[si][1] <= 0:
                si += 1
    return out
