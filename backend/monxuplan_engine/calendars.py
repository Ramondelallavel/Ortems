"""Working-time calendars.

A :class:`WorkCalendar` is a sorted list of disjoint working windows ``[start, end)`` expressed in
integer minutes since the horizon origin (UTC). Each window is ``REGULAR`` or ``OVERTIME``.

Shift patterns are defined in plant local time and expanded with :mod:`zoneinfo`, so daylight-saving
transitions are handled exactly: a 22:00–06:00 shift on the night the clocks go back lasts nine real
hours, on the night they go forward seven.
"""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from collections.abc import Iterable, Sequence
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from .contract import CalendarSpec

INF = 1 << 60


class WorkCalendar:
    """Immutable set of working windows with working-time arithmetic.

    ``starts``/``ends`` are strictly increasing, windows are disjoint and never touch when they have
    the same kind (touching windows of different kinds are allowed, e.g. regular shift followed by
    an overtime extension).
    """

    __slots__ = ("starts", "ends", "overtime", "cum", "key", "_total")

    def __init__(self, windows: Iterable[tuple[int, int, bool]] = (), key: str = "") -> None:
        ws = _normalize(windows)
        self.starts: list[int] = [w[0] for w in ws]
        self.ends: list[int] = [w[1] for w in ws]
        self.overtime: list[bool] = [w[2] for w in ws]
        cum = [0]
        for s, e in zip(self.starts, self.ends, strict=True):
            cum.append(cum[-1] + (e - s))
        self.cum: list[int] = cum
        self._total = cum[-1]
        self.key = key

    # ------------------------------------------------------------------ construction helpers
    @classmethod
    def always(cls, lo: int, hi: int, key: str = "24/7") -> WorkCalendar:
        return cls([(lo, hi, False)], key=key)

    def windows(self) -> list[tuple[int, int, bool]]:
        return list(zip(self.starts, self.ends, self.overtime, strict=True))

    def __len__(self) -> int:
        return len(self.starts)

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"WorkCalendar({self.key!r}, windows={len(self.starts)}, minutes={self._total})"

    @property
    def total_minutes(self) -> int:
        return self._total

    @property
    def lo(self) -> int:
        return self.starts[0] if self.starts else 0

    @property
    def hi(self) -> int:
        return self.ends[-1] if self.ends else 0

    # ------------------------------------------------------------------ queries
    def is_working(self, t: int) -> bool:
        i = bisect_right(self.starts, t) - 1
        return i >= 0 and t < self.ends[i]

    def window_index(self, t: int) -> int:
        """Index of the window containing t, or -1."""
        i = bisect_right(self.starts, t) - 1
        if i >= 0 and t < self.ends[i]:
            return i
        return -1

    def next_work(self, t: int) -> int | None:
        """Earliest working instant >= t (None if the calendar has no more working time)."""
        i = bisect_right(self.starts, t) - 1
        if i >= 0 and t < self.ends[i]:
            return t
        j = i + 1
        if j < len(self.starts):
            return self.starts[j]
        return None

    def cumulative(self, t: int) -> int:
        """W(t): working minutes in (-inf, t)."""
        i = bisect_right(self.starts, t) - 1
        if i < 0:
            return 0
        return self.cum[i] + min(t, self.ends[i]) - self.starts[i]

    def working_between(self, a: int, b: int) -> int:
        if b <= a:
            return 0
        return self.cumulative(b) - self.cumulative(a)

    def pieces(self, a: int, b: int) -> list[tuple[int, int]]:
        """Working sub-intervals of [a, b) (an operation consumes secondary resources only there)."""
        if b <= a:
            return []
        out = []
        i = max(bisect_right(self.starts, a) - 1, 0)
        n = len(self.starts)
        while i < n and self.starts[i] < b:
            lo, hi = max(a, self.starts[i]), min(b, self.ends[i])
            if hi > lo:
                if out and out[-1][1] == lo:
                    out[-1] = (out[-1][0], hi)
                else:
                    out.append((lo, hi))
            i += 1
        return out

    def overtime_between(self, a: int, b: int) -> int:
        if b <= a or not any(self.overtime):
            return 0
        total = 0
        i = max(bisect_right(self.starts, a) - 1, 0)
        n = len(self.starts)
        while i < n and self.starts[i] < b:
            if self.overtime[i]:
                lo = max(a, self.starts[i])
                hi = min(b, self.ends[i])
                if hi > lo:
                    total += hi - lo
            i += 1
        return total

    def add_work(self, s: int, d: int) -> int | None:
        """End of ``d`` working minutes starting at the first working instant >= s.

        Work pauses over non-working time (interruptible operation). ``d == 0`` returns the start.
        Returns None when the calendar ends before the work is complete.
        """
        t = self.next_work(s)
        if t is None:
            return None
        if d <= 0:
            return t
        target = self.cumulative(t) + d
        if target > self._total:
            return None
        # smallest window j with cum[j+1] >= target
        j = bisect_left(self.cum, target, lo=1) - 1
        return self.starts[j] + (target - self.cum[j])

    def sub_work(self, e: int, d: int) -> int | None:
        """Latest start s such that working minutes in [s, e) == d (backward scheduling)."""
        if d <= 0:
            return e
        target = self.cumulative(e) - d
        if target < 0:
            return None
        # largest window j with cum[j] <= target, and the instant inside it
        j = bisect_right(self.cum, target) - 1
        if j >= len(self.starts):
            j = len(self.starts) - 1
        s = self.starts[j] + (target - self.cum[j])
        if s >= self.ends[j]:  # exactly at a window end → belongs to next window start
            if j + 1 < len(self.starts):
                return self.starts[j + 1]
            return None
        return s

    def fits_uninterrupted(self, s: int, d: int) -> bool:
        i = self.window_index(s)
        return i >= 0 and s + d <= self._merged_end(i)

    def next_uninterrupted_start(self, s: int, d: int) -> int | None:
        """Earliest t >= s such that [t, t+d) lies inside continuous working time."""
        t = self.next_work(s)
        while t is not None:
            i = self.window_index(t)
            if t + d <= self._merged_end(i):
                return t
            # jump to the start of the next non-contiguous block
            k = i
            n = len(self.starts)
            while k + 1 < n and self.starts[k + 1] == self.ends[k]:
                k += 1
            if k + 1 >= n:
                return None
            t = self.starts[k + 1]
        return None

    def _merged_end(self, i: int) -> int:
        """End of the continuous block that contains window i (touching windows are contiguous)."""
        n = len(self.starts)
        e = self.ends[i]
        k = i
        while k + 1 < n and self.starts[k + 1] == e:
            k += 1
            e = self.ends[k]
        return e

    # ------------------------------------------------------------------ set operations
    def intersect(self, other: WorkCalendar, key: str | None = None) -> WorkCalendar:
        out: list[tuple[int, int, bool]] = []
        i = j = 0
        a_s, a_e, a_o = self.starts, self.ends, self.overtime
        b_s, b_e, b_o = other.starts, other.ends, other.overtime
        while i < len(a_s) and j < len(b_s):
            lo = max(a_s[i], b_s[j])
            hi = min(a_e[i], b_e[j])
            if lo < hi:
                out.append((lo, hi, a_o[i] or b_o[j]))
            if a_e[i] < b_e[j]:
                i += 1
            else:
                j += 1
        return WorkCalendar(out, key=key or f"{self.key}&{other.key}")

    def subtract(self, blocks: Sequence[tuple[int, int]], key: str | None = None) -> WorkCalendar:
        if not blocks:
            return self
        bl = _merge([(a, b) for a, b in blocks if b > a])
        bl_ends = [b for _, b in bl]
        out: list[tuple[int, int, bool]] = []
        for s, e, ot in self.windows():
            cur = s
            k = bisect_right(bl_ends, cur)
            while k < len(bl) and bl[k][0] < e:
                a, b = bl[k]
                if a > cur:
                    out.append((cur, min(a, e), ot))
                cur = max(cur, b)
                if cur >= e:
                    break
                k += 1
            if cur < e:
                out.append((cur, e, ot))
        return WorkCalendar(out, key=key or f"{self.key}-blocks")

    def without_overtime(self) -> WorkCalendar:
        if not any(self.overtime):
            return self
        return WorkCalendar([w for w in self.windows() if not w[2]], key=f"{self.key}-no-ot")

    def clip(self, lo: int, hi: int) -> WorkCalendar:
        out = []
        for s, e, ot in self.windows():
            s2, e2 = max(s, lo), min(e, hi)
            if s2 < e2:
                out.append((s2, e2, ot))
        return WorkCalendar(out, key=self.key)

    def segments(self) -> list[tuple[int, int]]:
        """Continuous blocks (touching windows merged) — used by the CP-SAT calendar model."""
        out: list[tuple[int, int]] = []
        for s, e in zip(self.starts, self.ends, strict=True):
            if out and out[-1][1] == s:
                out[-1] = (out[-1][0], e)
            else:
                out.append((s, e))
        return out


def _normalize(windows: Iterable[tuple[int, int, bool]]) -> list[tuple[int, int, bool]]:
    """Sort, drop empties, merge same-kind overlaps; regular time wins over overtime."""
    ws = sorted((int(s), int(e), bool(o)) for s, e, o in windows if e > s)
    if not ws:
        return []
    regular = _merge([(s, e) for s, e, o in ws if not o])
    overtime = _merge([(s, e) for s, e, o in ws if o])
    # remove regular time from overtime windows
    ot_clean: list[tuple[int, int]] = []
    for s, e in overtime:
        cur = s
        for rs, re_ in regular:
            if re_ <= cur or rs >= e:
                continue
            if rs > cur:
                ot_clean.append((cur, rs))
            cur = max(cur, re_)
            if cur >= e:
                break
        if cur < e:
            ot_clean.append((cur, e))
    out = [(s, e, False) for s, e in regular] + [(s, e, True) for s, e in ot_clean]
    out.sort()
    return out


def _merge(iv: list[tuple[int, int]]) -> list[tuple[int, int]]:
    out: list[tuple[int, int]] = []
    for s, e in sorted(iv):
        if out and s <= out[-1][1]:
            if e > out[-1][1]:
                out[-1] = (out[-1][0], e)
        else:
            out.append((s, e))
    return out


# ---------------------------------------------------------------------------------------------
# Expansion of calendar specifications
# ---------------------------------------------------------------------------------------------


class TimeAxis:
    """Conversion between aware datetimes and integer minutes since the horizon origin."""

    __slots__ = ("origin", "default_tz")

    def __init__(self, origin: datetime, default_tz: str = "UTC") -> None:
        if origin.tzinfo is None:
            origin = origin.replace(tzinfo=ZoneInfo(default_tz))
        self.origin = origin.astimezone(UTC)
        self.default_tz = default_tz

    def to_min(self, dt: datetime, tz: str | None = None) -> int:
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=ZoneInfo(tz or self.default_tz))
        delta = dt.astimezone(UTC) - self.origin
        return int(delta.total_seconds() // 60)

    def to_min_ceil(self, dt: datetime, tz: str | None = None) -> int:
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=ZoneInfo(tz or self.default_tz))
        secs = (dt.astimezone(UTC) - self.origin).total_seconds()
        m = int(secs // 60)
        return m if m * 60 == secs else m + 1

    def to_dt(self, minutes: int) -> datetime:
        return self.origin + timedelta(minutes=minutes)


def _local_to_min(axis: TimeAxis, d: date, t: time, tz: ZoneInfo) -> int:
    local = datetime.combine(d, t).replace(tzinfo=tz)
    return axis.to_min(local)


def expand_calendar(
    spec: CalendarSpec,
    axis: TimeAxis,
    lo: int,
    hi: int,
    registry: dict[str, CalendarSpec] | None = None,
) -> WorkCalendar:
    """Expand a calendar specification into working windows within [lo, hi)."""
    if spec.always_available:
        base = WorkCalendar.always(lo, hi, key=spec.id)
        return _apply_exceptions(base, _collect_exceptions(spec, registry), axis, spec.timezone, lo, hi, spec.id)

    shifts = _resolve_shifts(spec, registry)
    tz = ZoneInfo(spec.timezone)
    first_day = axis.to_dt(lo).astimezone(tz).date() - timedelta(days=1)
    last_day = axis.to_dt(hi).astimezone(tz).date() + timedelta(days=1)
    windows: list[tuple[int, int, bool]] = []
    breaks: list[tuple[int, int]] = []
    day = first_day
    by_weekday: dict[int, list] = {}
    for sh in shifts:
        by_weekday.setdefault(sh.weekday, []).append(sh)
    while day <= last_day:
        for sh in by_weekday.get(day.weekday(), ()):
            s = _local_to_min(axis, day, sh.start, tz)
            end_day = day if sh.end > sh.start else day + timedelta(days=1)
            e = _local_to_min(axis, end_day, sh.end, tz)
            if e > s:
                windows.append((s, e, sh.kind == "OVERTIME"))
            for br in sh.breaks:
                b_day = day if br.start >= sh.start else day + timedelta(days=1)
                b_end_day = b_day if br.end > br.start else b_day + timedelta(days=1)
                bs = _local_to_min(axis, b_day, br.start, tz)
                be = _local_to_min(axis, b_end_day, br.end, tz)
                if be > bs:
                    breaks.append((bs, be))
        day += timedelta(days=1)
    cal = WorkCalendar(windows, key=spec.id).subtract(breaks, key=spec.id)
    cal = _apply_exceptions(cal, _collect_exceptions(spec, registry), axis, spec.timezone, lo, hi, spec.id)
    return cal.clip(lo, hi)


def _resolve_shifts(spec: CalendarSpec, registry: dict[str, CalendarSpec] | None, depth: int = 0):
    if spec.shifts or not spec.parent_id or registry is None or depth > 10:
        return spec.shifts
    parent = registry.get(spec.parent_id)
    if parent is None:
        return spec.shifts
    return _resolve_shifts(parent, registry, depth + 1)


def _collect_exceptions(spec: CalendarSpec, registry: dict[str, CalendarSpec] | None, depth: int = 0):
    exc = list(spec.exceptions)
    if spec.parent_id and registry is not None and depth < 10:
        parent = registry.get(spec.parent_id)
        if parent is not None:
            exc = _collect_exceptions(parent, registry, depth + 1) + exc
    return exc


def _apply_exceptions(cal: WorkCalendar, exceptions, axis: TimeAxis, tz: str, lo: int, hi: int, key: str) -> WorkCalendar:
    """Closures are removed from the regular pattern first; extra working / overtime periods are then
    added on top, so an explicit extra shift on a holiday is honoured."""
    if not exceptions:
        return cal
    closed: list[tuple[int, int]] = []
    extra: list[tuple[int, int, bool]] = []
    for ex in exceptions:
        a = axis.to_min(ex.start, tz)
        b = axis.to_min(ex.end, tz)
        if b <= a:
            continue
        if ex.kind == "CLOSED":
            closed.append((a, b))
        elif min(b, hi) > max(a, lo):
            extra.append((max(a, lo), min(b, hi), ex.kind == "OVERTIME"))
    out = cal.subtract(closed, key=key)
    if extra:
        out = WorkCalendar(out.windows() + extra, key=key)
    return out
