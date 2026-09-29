"""Read models of a built schedule: capacity profiles, resource calendars, material projections.

Screens of a 100 000-order plan must not rebuild the engine state to draw a capacity heat-map, a
resource row or a stock projection. These functions turn a :class:`BuildResult` into plain JSON
documents that the platform stores (compressed) with the plan version; the same functions answer
on-demand requests from a replayed plan, so both paths return identical data.
"""

from __future__ import annotations

import zlib
from collections import defaultdict
from typing import Any

from .builder import BuildResult
from .capacity import load_profile, requirement_intervals

CAPACITY_SIZES = ("hour", "shift", "day", "week", "month")
CAPACITY_EXCLUDED_KINDS = ("SUBCONTRACTOR", "STORAGE", "TRANSPORT")


def capacity_view(result: BuildResult, size: str, start: int | None = None, end: int | None = None, req: dict | None = None) -> dict[str, Any]:
    """Load vs capacity per resource and bucket (minutes). Compact: one array per measure."""
    cp = result.cp
    bks, loads = load_profile(cp, result.placements, result.timing, size, start, end, req=req)
    rows = []
    for rl in loads:
        r = cp.resources[rl.resource]
        if r.kind in CAPACITY_EXCLUDED_KINDS:
            continue
        rows.append(
            {
                "id": r.id,
                "code": r.code,
                "name": r.name,
                "kind": r.kind,
                "area": r.area,
                "groups": list(r.groups),
                "capacity": [b["capacity"] for b in rl.buckets],
                "scheduled": [b["scheduled"] for b in rl.buckets],
                "requirement": [b["requirement"] for b in rl.buckets],
            }
        )
    return {"size": size, "buckets": [{"label": bk.label, "start": cp.dt(bk.start).isoformat(), "end": cp.dt(bk.end).isoformat()} for bk in bks], "rows": rows}


def capacity_views(result: BuildResult, sizes: tuple[str, ...] = CAPACITY_SIZES) -> dict[str, dict[str, Any]]:
    """Every bucket size over the plan horizon; the requirement windows are computed once."""
    req = requirement_intervals(result.cp, result.placements, result.timing)
    return {size: capacity_view(result, size, req=req) for size in sizes}


def resource_calendar(result: BuildResult, ri: int) -> dict[str, Any]:
    """Working blocks (minutes from the plan origin) and unavailability of one resource."""
    cp = result.cp
    r = cp.resources[ri]
    return {
        "id": r.id,
        "code": r.code,
        "name": r.name,
        "kind": r.kind,
        "area": r.area,
        "order": ri,
        "origin": cp.dt(0).isoformat(),
        "finite": bool(r.finite),
        "working": [[a, b] for a, b in r.cal.segments()],
        "unavailability": [{"id": uid, "start": a, "end": b, "kind": kind, "reason": reason} for a, b, kind, reason, uid, _loss in r.unavail],
    }


def resource_calendars(result: BuildResult) -> dict[str, dict[str, Any]]:
    return {r.id: resource_calendar(result, r.idx) for r in result.cp.resources}


def material_projection(result: BuildResult, mi: int) -> dict[str, Any]:
    """Projected stock of one material under the plan: supplies, consumptions, alerts."""
    cp = result.cp
    m = cp.materials[mi]
    acc = result.ledger.accounts[mi]
    level = 0.0
    points = []
    # same instant: supplies first, then by reference — the same order however the ledger was built
    events = sorted(acc.events, key=lambda e: (e.time, e.delta < 0, str(e.meta.get("ref") or e.ref)))
    for e in events:
        level += e.delta
        points.append(
            {
                "time": cp.dt(max(e.time, cp.as_of)).isoformat(),
                "delta": round(e.delta, 4),
                "level": round(level, 4),
                "kind": e.kind,
                "ref": e.meta.get("ref") or e.ref,
                "supply_kind": e.meta.get("kind"),
                "supplier": e.meta.get("supplier"),
            }
        )
    alerts = []
    min_units, t = acc.min_level_units()
    min_level = min_units / 1_000_000
    if min_units < 0:
        alerts.append({"type": "STOCKOUT", "at": cp.dt(t).isoformat() if t is not None else None, "level": min_level})
    elif m.safety_stock and min_level < m.safety_stock:
        alerts.append({"type": "SAFETY_STOCK_BREACH", "at": cp.dt(t).isoformat() if t is not None else None, "level": min_level, "safety_stock": m.safety_stock})
    total_in = sum(e.delta for e in events if e.delta > 0)
    total_out = -sum(e.delta for e in events if e.delta < 0)
    if total_out > 0 and level > 3 * total_out:
        alerts.append({"type": "EXCESS_INVENTORY", "level": level})
    return {"material_id": m.id, "code": m.code, "name": m.name, "uom": m.uom, "safety_stock": m.safety_stock, "points": points, "alerts": alerts, "supply_total": total_in, "demand_total": total_out}


def material_projections(result: BuildResult) -> dict[str, dict[str, Any]]:
    return {m.id: material_projection(result, m.idx) for m in result.cp.materials}


CHAIN_SHARDS = 256


def chain_shard(order_id: str) -> str:
    """Stable shard of an order's dependency chain (orders are stored 256 documents to a plan)."""
    return f"{zlib.crc32(order_id.encode()) % CHAIN_SHARDS:02x}"


def order_chain(result: BuildResult, oi: int, feeders: dict[int, set[int]] | None = None) -> dict[str, Any]:
    """Operations of one order and of the component orders feeding it, with their dependencies."""
    cp = result.cp
    ops = set(cp.orders[oi].ops)
    if feeders is None:
        feeders = _feeders(result)
    for po in feeders.get(oi, ()):
        ops.update(cp.orders[po].ops)
    deps = []
    for i in ops:
        for p, kind, lag, _f in cp.ops[i].preds:
            if p in ops:
                deps.append({"from": cp.ops[p].id, "to": cp.ops[i].id, "type": "FS" if kind == "OVL" else kind, "lag_minutes": lag, "overlap": kind == "OVL"})
    return {"order_id": cp.orders[oi].id, "operations": sorted(cp.ops[i].id for i in ops), "dependencies": deps}


def _feeders(result: BuildResult) -> dict[int, set[int]]:
    """Consumer order → component (make-item) orders that supply it."""
    cp = result.cp
    out: dict[int, set[int]] = defaultdict(set)
    for peg in cp.static_pegs:
        if peg.producer_order is not None:
            out[cp.ops[peg.consumer_op].order].add(peg.producer_order)
    return out


def order_chains(result: BuildResult) -> dict[str, dict[str, Any]]:
    """Every order's chain, grouped by shard."""
    feeders = _feeders(result)
    shards: dict[str, dict[str, Any]] = defaultdict(dict)
    for o in result.cp.orders:
        shards[chain_shard(o.id)][o.id] = order_chain(result, o.idx, feeders)
    return dict(shards)


def plan_views(result: BuildResult) -> dict[str, dict[str, Any]]:
    """All read models of a schedule, by kind then key (what the platform stores per plan)."""
    return {
        "CAPACITY": capacity_views(result),
        "CALENDAR": resource_calendars(result),
        "MATERIAL": material_projections(result),
        "CHAINS": order_chains(result),
    }
