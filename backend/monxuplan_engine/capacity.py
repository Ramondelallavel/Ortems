"""Capacity analysis: load vs capacity per resource and time bucket, heatmap states, bottlenecks.

Two loads are computed:

* **scheduled load** — working minutes of the finite-capacity schedule falling in each bucket; it can
  never exceed capacity (the builder guarantees it);
* **requirement load** — where the work *should* be to meet due dates at infinite capacity (latest
  start from the backward pass, work spread on the resource calendar). Requirement > capacity is an
  **overload**: the finite schedule resolves it by moving work later (lateness) or to alternatives.
"""

from __future__ import annotations

from bisect import bisect_right
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from .compile import CompiledProblem

POS = 1 << 50

HEAT_THRESHOLDS = {"underloaded": 0.5, "balanced": 0.85, "high": 1.0}


@dataclass
class Bucket:
    start: int
    end: int
    label: str


@dataclass
class ResourceLoad:
    resource: int
    buckets: list[dict] = field(default_factory=list)
    capacity: int = 0
    scheduled: int = 0
    requirement: int = 0


def buckets(cp: CompiledProblem, size: str, start: int | None = None, end: int | None = None) -> list[Bucket]:
    """Calendar buckets in plant local time: hour, shift (8h from 06:00), day, week (ISO), month."""
    tz = ZoneInfo(cp.problem.horizon.timezone)
    a = cp.as_of if start is None else start
    b = cp.h_end if end is None else end
    local = cp.axis.to_dt(a).astimezone(tz)
    if size == "hour":
        cur = local.replace(minute=0, second=0, microsecond=0)
    elif size == "shift":
        cur = local.replace(minute=0, second=0, microsecond=0)
        cur = cur.replace(hour=6 + 8 * ((cur.hour - 6) // 8)) if cur.hour >= 6 else (cur - timedelta(days=1)).replace(hour=22)
    elif size == "week":
        cur = datetime(local.year, local.month, local.day, tzinfo=tz) - timedelta(days=local.weekday())
    elif size == "month":
        cur = datetime(local.year, local.month, 1, tzinfo=tz)
    else:
        cur = datetime(local.year, local.month, local.day, tzinfo=tz)
    out: list[Bucket] = []
    guard = 0
    while guard < 5000:
        guard += 1
        if size == "hour":
            nxt = cur + timedelta(hours=1)
            label = cur.strftime("%Y-%m-%d %H:00")
        elif size == "shift":
            nxt = cur + timedelta(hours=8)
            label = cur.strftime("%Y-%m-%d %H:00")
        elif size == "week":
            nxt = cur + timedelta(days=7)
            iso = cur.isocalendar()
            label = f"{iso[0]}-W{iso[1]:02d}"
        elif size == "month":
            nxt = datetime(cur.year + (cur.month // 12), cur.month % 12 + 1, 1, tzinfo=tz)
            label = cur.strftime("%Y-%m")
        else:
            nxt = datetime.combine(cur.date() + timedelta(days=1), datetime.min.time(), tzinfo=tz)
            label = cur.strftime("%Y-%m-%d")
        s, e = cp.axis.to_min(cur), cp.axis.to_min(nxt)
        if e > a:
            out.append(Bucket(max(s, a) if not out else s, e, label))
        if e >= b:
            break
        cur = nxt
    return out


def _spread(cal, a: int, b: int, bks: list[Bucket], acc: dict[int, int], ends: list[int] | None = None) -> None:
    """Add the working minutes of [a, b) on `cal` to the buckets (``ends``: bucket ends, to jump
    straight to the first overlapping bucket)."""
    k = bisect_right(ends, a) if ends is not None else 0
    n = len(bks)
    while k < n:
        bk = bks[k]
        if bk.start >= b:
            break
        if bk.end > a:
            w = cal.working_between(max(a, bk.start), min(b, bk.end))
            if w:
                acc[k] = acc.get(k, 0) + w
        k += 1


def requirement_intervals(cp: CompiledProblem, placements, timing) -> dict[int, list[tuple[int, int, int]]]:
    """Per resource: (op, start, end) of each operation's infinite-capacity requirement window."""
    out: dict[int, list[tuple[int, int, int]]] = defaultdict(list)
    for op in cp.ops:
        if not op.modes:
            continue
        p = placements[op.idx]
        # frozen / locked / in-progress work is required where it is; positions merely kept by the
        # replay of a stored plan ("KEPT") are not requirements: their requirement is the backward pass
        if p is not None and p.fixed and p.fixed_reason != "KEPT":
            out[p.res].append((op.idx, p.setup_start, p.end))
            continue
        if p is not None:
            m = op.modes[p.mode]
        else:
            m = min(op.modes, key=lambda m: (m.pref, m.nominal))
        if m.sub:
            continue
        lst = timing.lst[op.idx]
        s = max(cp.as_of, lst if lst < POS else cp.as_of)
        s = m.cal.next_work(s)
        if s is None:
            continue
        e = m.cal.add_work(s, m.nominal) or cp.hi
        out[m.res].append((op.idx, s, e))
    return out


def load_profile(
    cp: CompiledProblem,
    placements,
    timing,
    size: str = "day",
    start: int | None = None,
    end: int | None = None,
    resources: list[int] | None = None,
    req: dict[int, list[tuple[int, int, int]]] | None = None,
) -> tuple[list[Bucket], list[ResourceLoad]]:
    bks = buckets(cp, size, start, end)
    ends = [bk.end for bk in bks]
    if req is None:
        req = requirement_intervals(cp, placements, timing)
    by_res: dict[int, list] = defaultdict(list)
    for p in placements:
        if p is not None:
            by_res[p.res].append(p)
            for ri, _u in cp.ops[p.op].modes[p.mode].sec:
                by_res[ri].append(p)
    out: list[ResourceLoad] = []
    rset = resources if resources is not None else [r.idx for r in cp.resources if r.finite]
    for ri in rset:
        r = cp.resources[ri]
        cap_acc: dict[int, int] = {}
        for k, bk in enumerate(bks):
            if r.unary:
                cap_acc[k] = r.cal.working_between(bk.start, bk.end)
            else:
                cap_acc[k] = _profile_minutes(r.profile, bk.start, bk.end)
        sched: dict[int, int] = {}
        for p in by_res.get(ri, ()):
            m = cp.ops[p.op].modes[p.mode]
            units = 1 if p.res == ri else next((u for rr, u in m.sec if rr == ri), 1)
            if units == 1:
                _spread(m.cal, p.setup_start, p.end, bks, sched, ends)
                continue
            tmp: dict[int, int] = {}
            _spread(m.cal, p.setup_start, p.end, bks, tmp, ends)
            for k, v in tmp.items():
                sched[k] = sched.get(k, 0) + v * units
        rq: dict[int, int] = {}
        for i, s, e in req.get(ri, ()):
            m = next((m for m in cp.ops[i].modes if m.res == ri), None)
            cal = m.cal if m is not None else r.cal
            # requirement before the first bucket is backlog → first bucket
            if s < bks[0].start if bks else False:
                s = bks[0].start
            _spread(cal, s, e, bks, rq, ends)
        rl = ResourceLoad(ri)
        for k, bk in enumerate(bks):
            cap = cap_acc.get(k, 0)
            sc = sched.get(k, 0)
            rqv = rq.get(k, 0)
            rl.buckets.append(
                {
                    "bucket": k,
                    "capacity": cap,
                    "scheduled": sc,
                    "requirement": rqv,
                    "overload": max(0, rqv - cap),
                    "available": max(0, cap - sc),
                    "utilization": (sc / cap) if cap else None,
                    "requirement_utilization": (rqv / cap) if cap else None,
                    "state": heat_state(cap, sc, rqv),
                }
            )
            rl.capacity += cap
            rl.scheduled += sc
            rl.requirement += rqv
        out.append(rl)
    return bks, out


def _profile_minutes(profile: list[tuple[int, int]], a: int, b: int) -> int:
    total = 0
    for k, (t, c) in enumerate(profile):
        e = profile[k + 1][0] if k + 1 < len(profile) else POS
        lo, hi = max(t, a), min(e, b)
        if hi > lo and c > 0:
            total += (hi - lo) * c
    return total


def heat_state(cap: int, scheduled: int, requirement: int) -> str:
    if cap <= 0:
        return "UNAVAILABLE" if (scheduled == 0 and requirement == 0) else "OVERLOADED"
    u = max(scheduled, requirement) / cap
    if u > HEAT_THRESHOLDS["high"] + 1e-9:
        return "OVERLOADED"
    if u >= HEAT_THRESHOLDS["balanced"]:
        return "HIGH_LOAD"
    if u >= HEAT_THRESHOLDS["underloaded"]:
        return "BALANCED"
    return "UNDERLOADED"


# =============================================================================================
# Bottlenecks
# =============================================================================================


def bottlenecks(cp: CompiledProblem, placements, timing, unscheduled: dict, order_late: dict[int, int]) -> list[dict]:
    """Rank limiting factors by measurable quantities only: overload minutes, induced waiting
    minutes and utilisation (no synthetic score)."""
    a, b = cp.as_of, cp.h_end
    req = requirement_intervals(cp, placements, timing)
    out: list[dict] = []
    waits_by_ref: dict[tuple[str, str], int] = defaultdict(int)
    late_orders_by_ref: dict[tuple[str, str], set[int]] = defaultdict(set)
    for p in placements:
        if p is None or p.binding is None:
            continue
        bt = p.binding
        ref = bt.ref
        if bt.type == "RESOURCE":
            # ref is the blocking operation (or the resource itself for cumulative resources)
            if ref in cp.op_index:
                bp = placements[cp.op_index[ref]]
                ref = cp.resources[bp.res].id if bp is not None else ref
            key = ("RESOURCE", ref or "")
        elif bt.type in ("LABOR", "TOOL", "MATERIAL", "CALENDAR", "SETUP"):
            key = (bt.type, ref or cp.resources[p.res].id)
            if bt.type in ("CALENDAR", "SETUP"):
                key = (bt.type, cp.resources[p.res].id)
        else:
            continue
        waits_by_ref[key] += bt.wait
        o = cp.ops[p.op].order
        if order_late.get(o, 0) > 0:
            late_orders_by_ref[key].add(o)
    if cp.constraints.materials != "IGNORE":
        for i, u in unscheduled.items():
            if u.reason == "MATERIAL_SHORTAGE":
                for d in u.details.get("materials", []):
                    key = ("MATERIAL", d["material_id"])
                    late_orders_by_ref[key].add(cp.ops[i].order)

    by_res: dict[int, list] = defaultdict(list)
    for p in placements:  # group once: the loop below is per resource
        if p is not None:
            by_res[p.res].append(p)
    for r in cp.resources:
        if not r.finite or r.kind in ("LABOR_POOL", "TOOL"):
            continue
        cap = r.cal.working_between(a, b) * (1 if r.unary else max(r.capacity, 1))
        if cap <= 0 and not req.get(r.idx):
            continue
        sched = 0
        setup_min = 0
        fam_minutes: dict[str, int] = defaultdict(int)
        for p in by_res.get(r.idx, ()):
            m = cp.ops[p.op].modes[p.mode]
            w = m.cal.working_between(max(p.setup_start, a), min(p.end, b))
            sched += w
            setup_min += min(p.setup, w)
        requirement = 0
        for i, s, e in req.get(r.idx, ()):
            if s >= b:
                continue
            m = next((m for m in cp.ops[i].modes if m.res == r.idx), None)
            w = m.nominal if m is not None else (e - s)
            requirement += w
            fam_minutes[cp.ops[i].family or cp.orders[cp.ops[i].order].item_code or "—"] += max(w - (m.setup_base if m else 0), 0)
        maint = 0
        for ua, ub, kind, *_ in r.unavail:
            if kind.startswith("MAINTENANCE") or kind in ("BREAKDOWN", "DOWNTIME"):
                maint += max(0, min(ub, b) - max(ua, a))
        overload = max(0, requirement - cap)
        util = sched / cap if cap else 0.0
        key = ("RESOURCE", r.id)
        wait = waits_by_ref.get(key, 0)
        if overload <= 0 and util < 0.85 and wait <= 0:
            continue
        # QUEUE: operations wait for this resource although it is not highly utilised over the
        # horizon (load arrives in bursts) — reported separately so utilisation is never overstated
        kind = "OVERLOADED" if overload > 0 else "HIGH_UTILIZATION" if util >= 0.85 else "QUEUE"
        causes = []
        total = sum(fam_minutes.values()) + setup_min + maint
        if total > 0:
            for fam, mins in sorted(fam_minutes.items(), key=lambda kv: -kv[1])[:5]:
                causes.append({"kind": "FAMILY", "ref": fam, "label": f"Orders {fam}", "minutes": mins, "share": mins / total})
            if maint:
                causes.append({"kind": "MAINTENANCE", "ref": None, "label": "Maintenance / downtime", "minutes": maint, "share": maint / total})
            if setup_min:
                causes.append({"kind": "SETUP", "ref": None, "label": "Setup", "minutes": setup_min, "share": setup_min / total})
        out.append(
            {
                "resource_id": r.id,
                "kind": kind,
                "capacity_minutes": cap,
                "scheduled_minutes": sched,
                "requirement_minutes": requirement,
                "overload_minutes": overload,
                "utilization": round(util, 4),
                "induced_wait_minutes": wait,
                "orders_affected": len(late_orders_by_ref.get(key, ())),
                "causes": causes,
                "ref": r.code,
            }
        )
    for (kind, ref), wait in waits_by_ref.items():
        if kind == "RESOURCE" or wait <= 0:
            continue
        if kind in ("CALENDAR", "SETUP"):
            kind_out = "CALENDAR" if kind == "CALENDAR" else "SEQUENCE"
        else:
            kind_out = kind
        ri = cp.res_index.get(ref)
        label = cp.resources[ri].code if ri is not None else (cp.materials[cp.mat_index[ref]].code if ref in cp.mat_index else ref)
        out.append(
            {
                "resource_id": ref if ri is not None else None,
                "kind": kind_out,
                "induced_wait_minutes": wait,
                "orders_affected": len(late_orders_by_ref.get((kind, ref), ())),
                "causes": [],
                "ref": label,
            }
        )
    for (kind, ref), orders in late_orders_by_ref.items():
        if kind == "MATERIAL" and (kind, ref) not in waits_by_ref:
            mi = cp.mat_index.get(ref)
            out.append({"resource_id": None, "kind": "MATERIAL", "induced_wait_minutes": 0, "orders_affected": len(orders), "causes": [], "ref": cp.materials[mi].code if mi is not None else ref})
    out.sort(key=lambda x: (-x.get("overload_minutes", 0), -x.get("induced_wait_minutes", 0), -x.get("orders_affected", 0), -x.get("utilization", 0.0)))
    for k, x in enumerate(out, 1):
        x["rank"] = k
    return out
