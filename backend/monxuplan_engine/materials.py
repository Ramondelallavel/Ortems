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
    """Time-ordered events of one material, kept in chunks of ~``CHUNK`` events.

    Each chunk stores its total delta and the minimum of its local running level, so the level at an
    instant and the minimum level after it are answered in O(sqrt n) and an insertion costs
    O(chunk size) — a material with 100 000 movements stays fast (a flat prefix array would be
    recomputed on every allocation: quadratic)."""

    __slots__ = ("material", "_chunks", "_keys", "_sum", "_min", "_flat")

    CHUNK = 64

    def __init__(self, material: int) -> None:
        self.material = material
        self._chunks: list[list[LedgerEvent]] = []
        self._keys: list[tuple[int, bool]] = []  # first event key of each chunk
        self._sum: list[float] = []
        self._min: list[float] = []  # min running level inside the chunk, relative to its start
        self._flat: list[LedgerEvent] | None = []

    # ---------------------------------------------------------------- maintenance
    @staticmethod
    def _key(e: LedgerEvent) -> tuple[int, bool]:
        return (e.time, e.delta < 0)

    def _stats(self, c: int) -> None:
        lvl = 0.0
        m = float("inf")
        for e in self._chunks[c]:
            lvl += e.delta
            if lvl < m:
                m = lvl
        self._sum[c] = lvl
        self._min[c] = m
        self._keys[c] = self._key(self._chunks[c][0])

    def add(self, ev: LedgerEvent) -> None:
        self._flat = None
        if not self._chunks:
            self._chunks.append([ev])
            self._keys.append(self._key(ev))
            self._sum.append(0.0)
            self._min.append(0.0)
            self._stats(0)
            return
        c = max(bisect_right(self._keys, self._key(ev)) - 1, 0)
        insort(self._chunks[c], ev)
        if len(self._chunks[c]) > 2 * self.CHUNK:
            ch = self._chunks[c]
            half = len(ch) // 2
            self._chunks[c : c + 1] = [ch[:half], ch[half:]]
            self._keys[c : c + 1] = [(0, False), (0, False)]
            self._sum[c : c + 1] = [0.0, 0.0]
            self._min[c : c + 1] = [0.0, 0.0]
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
            self._sum.append(0.0)
            self._min.append(0.0)
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

    # ---------------------------------------------------------------- queries
    def _position(self, t: int) -> tuple[int, int, float]:
        """(chunk, index in chunk, level) of the last event at or before t; chunk -1 if none."""
        keys = self._keys
        c = bisect_right(keys, (t, True)) - 1
        if c < 0:
            return -1, -1, 0.0
        base = 0.0
        for k in range(c):
            base += self._sum[k]
        ch = self._chunks[c]
        lvl = base
        idx = -1
        for i, e in enumerate(ch):
            if e.time > t:
                break
            lvl += e.delta
            idx = i
        return c, idx, lvl

    def level_at(self, t: int) -> float:
        return self._position(t)[2]

    def available_from(self, t: int) -> float:
        """min over t' >= t of the level — what can be consumed at t without hurting anyone."""
        if not self._chunks:
            return 0.0
        c, idx, cur = self._position(t)
        best = cur
        if c < 0:
            c, idx, lvl = 0, -1, 0.0
        else:
            lvl = cur
        ch = self._chunks[c]
        for e in ch[idx + 1 :]:
            lvl += e.delta
            if lvl < best:
                best = lvl
        for k in range(c + 1, len(self._chunks)):
            m = lvl + self._min[k]
            if m < best:
                best = m
            lvl += self._sum[k]
        return best

    def earliest(self, qty: float, t0: int) -> int | None:
        """Smallest t >= t0 with available_from(t) >= qty, or None if never within the ledger.

        available_from is non-decreasing in t, so the candidates (t0 and later event times) are
        searched with a binary search."""
        if self.available_from(t0) + EPS >= qty:
            return t0
        times = [e.time for e in self.events]
        lo = bisect_right(times, t0)
        if lo >= len(times) or self.available_from(times[-1]) + EPS < qty:
            return None
        hi = len(times) - 1
        while lo < hi:
            mid = (lo + hi) // 2
            if self.available_from(times[mid]) + EPS >= qty:
                hi = mid
            else:
                lo = mid + 1
        return times[lo]

    def max_available_after(self, t0: int) -> float:
        """Best achievable availability at or after t0 (for shortage reporting): available_from
        is non-decreasing, so it is its value at the last event (or at t0)."""
        evs = self.events
        if not evs:
            return 0.0
        return max(self.available_from(t0), self.available_from(max(evs[-1].time, t0)))

    def min_level(self) -> tuple[float, int | None]:
        lvl = 0.0
        best, at = float("inf"), None
        for e in self.events:
            lvl += e.delta
            if lvl < best:
                best, at = lvl, e.time
        if at is None:
            return 0.0, None
        return best, at


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
