"""Capacity timelines used by the schedule builder and the validator.

* :class:`UnaryTimeline` — disjunctive resource (a machine): ordered, non-overlapping blocks
  ``[setup_start, end)``; the block before an instant defines the setup state.
* :class:`CumulativeTimeline` — resource with a (time-varying) number of units: labour pools, tools,
  multi-capacity machines, ovens. Stored as a step function of *free* units.
"""

from __future__ import annotations

from bisect import bisect_right
from typing import Any

NEG_INF = -(1 << 60)
POS_INF = 1 << 60


class Block:
    __slots__ = ("op", "setup_start", "start", "end", "setup", "setup_base", "state_key", "cal", "fixed")

    def __init__(
        self,
        op: int,
        setup_start: int,
        start: int,
        end: int,
        setup: int,
        setup_base: int,
        state_key: Any,
        cal: Any,
        fixed: bool = False,
    ) -> None:
        self.op = op
        self.setup_start = setup_start
        self.start = start
        self.end = end
        self.setup = setup
        self.setup_base = setup_base
        self.state_key = state_key
        self.cal = cal
        self.fixed = fixed

    def __repr__(self) -> str:  # pragma: no cover
        return f"Block(op={self.op}, {self.setup_start}->{self.end})"


class UnaryTimeline:
    __slots__ = ("res", "starts", "blocks")

    def __init__(self, res: int) -> None:
        self.res = res
        self.starts: list[int] = []
        self.blocks: list[Block] = []

    def prev_index(self, t: int) -> int:
        """Index of the last block whose setup starts at or before t (-1 if none)."""
        return bisect_right(self.starts, t) - 1

    def insert(self, block: Block) -> int:
        i = bisect_right(self.starts, block.setup_start)
        self.starts.insert(i, block.setup_start)
        self.blocks.insert(i, block)
        return i

    def remove_op(self, op: int) -> Block | None:
        for i, b in enumerate(self.blocks):
            if b.op == op:
                del self.starts[i]
                del self.blocks[i]
                return b
        return None

    def index_of(self, op: int) -> int:
        for i, b in enumerate(self.blocks):
            if b.op == op:
                return i
        return -1

    def __len__(self) -> int:
        return len(self.blocks)


class CumulativeTimeline:
    """Step function of free capacity. ``times[i]`` .. ``times[i+1]`` has ``free[i]`` units."""

    __slots__ = ("res", "times", "free")

    def __init__(self, res: int, profile: list[tuple[int, int]]) -> None:
        """``profile`` is a list of (time, capacity) breakpoints; capacity before the first is 0."""
        self.res = res
        times = [NEG_INF]
        free = [0]
        for t, c in sorted(profile):
            if t == times[-1]:
                free[-1] = c
            elif free[-1] != c:
                times.append(t)
                free.append(c)
        self.times = times
        self.free = free

    def free_at(self, t: int) -> int:
        return self.free[bisect_right(self.times, t) - 1]

    def next_fit(self, a: int, b: int, units: int) -> int | None:
        """None if ``units`` are free during [a, b); otherwise the earliest t > a from which the
        shortage that blocks [a, b) is over (a candidate start for the next attempt)."""
        if b <= a:
            return None
        times, free = self.times, self.free
        i = bisect_right(times, a) - 1
        n = len(times)
        while i < n and times[i] < b:
            if free[i] < units:
                # skip forward to the first segment with enough free units
                j = i + 1
                while j < n and free[j] < units:
                    j += 1
                if j >= n:
                    return POS_INF
                return times[j]
            i += 1
        return None

    def min_free(self, a: int, b: int) -> int:
        times, free = self.times, self.free
        i = bisect_right(times, a) - 1
        m = POS_INF
        n = len(times)
        while i < n and times[i] < b:
            m = min(m, free[i])
            i += 1
        return m

    def _split(self, t: int) -> int:
        i = bisect_right(self.times, t) - 1
        if self.times[i] == t:
            return i
        self.times.insert(i + 1, t)
        self.free.insert(i + 1, self.free[i])
        return i + 1

    def reserve(self, a: int, b: int, units: int) -> None:
        if b <= a or units == 0:
            return
        i = self._split(a)
        j = self._split(b)
        for k in range(i, j):
            self.free[k] -= units

    def release(self, a: int, b: int, units: int) -> None:
        self.reserve(a, b, -units)

    def shortfalls(self) -> list[tuple[int, int, int]]:
        """Segments where free < 0: (start, end, missing units)."""
        out = []
        for k, f in enumerate(self.free):
            if f < 0:
                end = self.times[k + 1] if k + 1 < len(self.times) else POS_INF
                out.append((self.times[k], end, -f))
        return out
