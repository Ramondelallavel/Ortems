"""Scenario changes as pure transformations of a :class:`Problem`.

Scenarios are copy-on-write: a scenario is its base data plus an ordered list of changes. Applying
the changes to the base problem yields the scenario problem — deterministic, auditable and cheap to
clone (only the change list is copied).

Supported change types (payload keys in brackets):

``ADD_DOWNTIME``        machine breakdown / unexpected downtime / maintenance [resource_id, start, end, kind?, reason?]
``REMOVE_RESOURCE``     resource no longer available [resource_id, from?]
``ADD_RESOURCE``        new machine cloned from an existing one [clone_of, id, code, name?, available_from?, calendar_id?, efficiency?]
``ADD_SHIFT``           extra shift/overtime on resources [resource_ids, shifts: [{weekday,start,end,kind}], from?, to?]
``SET_CALENDAR``        [resource_ids, calendar_id]
``CHANGE_EFFICIENCY``   [resource_id, efficiency]
``CHANGE_CAPACITY``     labour pool headcount, tool copies [resource_id, capacity]
``ABSENCE``             operators absent from a labour pool [resource_id, start, end, units]
``ADD_ORDER``           rush order [order, operations, precedences?]
``CANCEL_ORDER``        [order_id]
``CHANGE_PRIORITY``     [order_id, priority?, expedite?, customer_priority?]
``CHANGE_DUE_DATE``     [order_id, due]
``CHANGE_QUANTITY``     [order_id, quantity] (operation quantities and material uses scaled)
``MATERIAL_DELAY``      supplier delay [material_id?, supply_id?, supplier_id?, delay_minutes? | new_time?]
``MATERIAL_ADJUST``     stock correction [material_id, quantity]
``SET_CONSTRAINTS``     [constraints: {...}]
``SET_OBJECTIVES``      [objectives: {...}]
``LOCK_SEQUENCE``       planner-fixed sequence on a resource [resource_id, op_ids]
``PIN_RESOURCE``        operation must run on a resource [op_id, resource_id]
"""

from __future__ import annotations

import copy
from datetime import datetime, timedelta
from typing import Any

from .contract import Problem

CHANGE_TYPES = {
    "ADD_DOWNTIME",
    "REMOVE_RESOURCE",
    "ADD_RESOURCE",
    "ADD_SHIFT",
    "SET_CALENDAR",
    "CHANGE_EFFICIENCY",
    "CHANGE_CAPACITY",
    "ABSENCE",
    "ADD_ORDER",
    "CANCEL_ORDER",
    "CHANGE_PRIORITY",
    "CHANGE_DUE_DATE",
    "CHANGE_QUANTITY",
    "MATERIAL_DELAY",
    "MATERIAL_ADJUST",
    "SET_CONSTRAINTS",
    "SET_OBJECTIVES",
    "LOCK_SEQUENCE",
    "PIN_RESOURCE",
}


class ChangeError(ValueError):
    pass


def apply_changes(problem: Problem, changes: list[dict[str, Any]]) -> tuple[Problem, list[str]]:
    """Return a new problem with the changes applied and a human-readable log."""
    data = problem.model_dump(mode="json", by_alias=True)
    log: list[str] = []
    for ch in changes:
        t = ch.get("type")
        payload = ch.get("payload", ch)
        fn = _HANDLERS.get(t or "")
        if fn is None:
            raise ChangeError(f"unknown scenario change type {t!r}")
        log.append(fn(data, payload))
    return Problem.model_validate(data), log


def _res(data, rid):
    for r in data["resources"]:
        if r["id"] == rid or r["code"] == rid:
            return r
    raise ChangeError(f"unknown resource {rid}")


def _order(data, oid):
    for o in data["orders"]:
        if o["id"] == oid or o["number"] == oid:
            return o
    raise ChangeError(f"unknown order {oid}")


def _add_downtime(data, p) -> str:
    r = _res(data, p["resource_id"])
    r.setdefault("unavailability", []).append(
        {
            "id": p.get("id"),
            "start": p["start"],
            "end": p["end"],
            "kind": p.get("kind", "BREAKDOWN"),
            "reason": p.get("reason"),
            "capacity_loss": p.get("capacity_loss"),
        }
    )
    return f"{r['code']}: {p.get('kind', 'BREAKDOWN').lower()} {p['start']} → {p['end']}"


def _remove_resource(data, p) -> str:
    r = _res(data, p["resource_id"])
    r["available_until"] = p.get("from") or data["horizon"]["start"]
    return f"{r['code']} removed from {r['available_until']}"


def _add_resource(data, p) -> str:
    src = _res(data, p["clone_of"])
    new = copy.deepcopy(src)
    new["id"] = p["id"]
    new["code"] = p.get("code", p["id"])
    new["name"] = p.get("name", new["code"])
    new["unavailability"] = []
    new["initial_state"] = None
    if p.get("available_from"):
        new["available_from"] = p["available_from"]
    if p.get("calendar_id"):
        new["calendar_id"] = p["calendar_id"]
    if p.get("efficiency"):
        new["efficiency"] = p["efficiency"]
    if p.get("cost_per_hour") is not None:
        new["cost_per_hour"] = p["cost_per_hour"]
    data["resources"].append(new)
    n = 0
    for op in data["operations"]:
        extra = []
        for m in op.get("modes", []):
            if m["resource_id"] == src["id"]:
                m2 = copy.deepcopy(m)
                m2["resource_id"] = new["id"]
                extra.append(m2)
        if extra:
            op["modes"].extend(extra)
            n += 1
    return f"{new['code']} added (clone of {src['code']}), qualified for {n} operations"


def _add_shift(data, p) -> str:
    ids = p["resource_ids"]
    shifts = p["shifts"]
    created = []
    for rid in ids:
        r = _res(data, rid)
        base_id = r.get("calendar_id")
        base = next((c for c in data["calendars"] if c["id"] == base_id), None)
        new_id = f"{base_id or '24x7'}+{p.get('label', 'extra')}+{r['id']}"
        if base is None:
            continue
        cal = copy.deepcopy(base)
        cal["id"] = new_id
        cal["name"] = f"{base.get('name') or base_id} + {p.get('label', 'extra shift')}"
        if not cal.get("shifts") and cal.get("parent_id"):
            parent = next((c for c in data["calendars"] if c["id"] == cal["parent_id"]), None)
            if parent:
                cal["shifts"] = copy.deepcopy(parent["shifts"])
        if p.get("from") or p.get("to"):
            # dated extra shifts → working exceptions for each matching weekday
            start = datetime.fromisoformat(p.get("from") or data["horizon"]["start"])
            end = datetime.fromisoformat(p.get("to") or data["horizon"]["end"])
            d = start.date()
            while d <= end.date():
                for sh in shifts:
                    if sh["weekday"] == d.weekday():
                        s_dt = datetime.fromisoformat(f"{d.isoformat()}T{sh['start']}")
                        e_day = d if sh["end"] > sh["start"] else d + timedelta(days=1)
                        e_dt = datetime.fromisoformat(f"{e_day.isoformat()}T{sh['end']}")
                        cal.setdefault("exceptions", []).append(
                            {"start": s_dt.isoformat(), "end": e_dt.isoformat(), "kind": "OVERTIME" if sh.get("kind") == "OVERTIME" else "WORKING", "reason": p.get("label")}
                        )
                d += timedelta(days=1)
        else:
            cal.setdefault("shifts", []).extend(copy.deepcopy(shifts))
        data["calendars"].append(cal)
        r["calendar_id"] = new_id
        created.append(r["code"])
    return f"extra shift {p.get('label', '')} on {', '.join(created)}"


def _set_calendar(data, p) -> str:
    for rid in p["resource_ids"]:
        _res(data, rid)["calendar_id"] = p["calendar_id"]
    return f"calendar {p['calendar_id']} on {len(p['resource_ids'])} resources"


def _change_efficiency(data, p) -> str:
    r = _res(data, p["resource_id"])
    r["efficiency"] = p["efficiency"]
    return f"{r['code']} efficiency {p['efficiency']:.0%}"


def _change_capacity(data, p) -> str:
    r = _res(data, p["resource_id"])
    r["capacity"] = int(p["capacity"])
    return f"{r['code']} capacity {p['capacity']}"


def _absence(data, p) -> str:
    r = _res(data, p["resource_id"])
    r.setdefault("unavailability", []).append(
        {"start": p["start"], "end": p["end"], "kind": "ABSENCE", "reason": p.get("reason"), "capacity_loss": int(p.get("units", 1))}
    )
    return f"{r['code']}: {p.get('units', 1)} absent {p['start']} → {p['end']}"


def _add_order(data, p) -> str:
    o = p["order"]
    if any(x["id"] == o["id"] for x in data["orders"]):
        raise ChangeError(f"order {o['id']} already exists")
    data["orders"].append(o)
    data["operations"].extend(p.get("operations", []))
    data["precedences"].extend(p.get("precedences", []))
    return f"order {o['number']} added ({len(p.get('operations', []))} operations)"


def _cancel_order(data, p) -> str:
    o = _order(data, p["order_id"])
    data["orders"] = [x for x in data["orders"] if x["id"] != o["id"]]
    ops = {op["id"] for op in data["operations"] if op["order_id"] == o["id"]}
    data["operations"] = [op for op in data["operations"] if op["order_id"] != o["id"]]
    data["precedences"] = [pr for pr in data["precedences"] if pr["pred"] not in ops and pr["succ"] not in ops]
    data["baseline"] = [b for b in data.get("baseline", []) if b["op_id"] not in ops]
    return f"order {o['number']} cancelled"


def _change_priority(data, p) -> str:
    o = _order(data, p["order_id"])
    for k in ("priority", "expedite", "customer_priority", "planner_priority", "strategic"):
        if k in p:
            o[k] = p[k]
    return f"order {o['number']} priority changed"


def _change_due(data, p) -> str:
    o = _order(data, p["order_id"])
    o["due"] = p["due"]
    return f"order {o['number']} due {p['due']}"


def _change_qty(data, p) -> str:
    o = _order(data, p["order_id"])
    f = float(p["quantity"]) / float(o["quantity"])
    o["quantity"] = float(p["quantity"])
    for op in data["operations"]:
        if op["order_id"] == o["id"]:
            op["quantity"] = op["quantity"] * f
            for mu in op.get("materials", []):
                mu["quantity"] = mu["quantity"] * f
    return f"order {o['number']} quantity {p['quantity']}"


def _material_delay(data, p) -> str:
    n = 0
    for m in data["materials"]:
        if p.get("material_id") and p["material_id"] not in (m["id"], m["code"]):
            continue
        for s in m.get("supplies", []):
            if p.get("supply_id") and s["id"] != p["supply_id"] and s.get("ref") != p["supply_id"]:
                continue
            if p.get("supplier_id") and s.get("supplier_id") != p["supplier_id"]:
                continue
            if s.get("kind") == "ON_HAND" or s.get("time") is None:
                continue
            if p.get("new_time"):
                s["time"] = p["new_time"]
            else:
                s["time"] = (datetime.fromisoformat(s["time"]) + timedelta(minutes=int(p.get("delay_minutes", 0)))).isoformat()
            n += 1
    if n == 0:
        raise ChangeError("material delay matches no receipt")
    return f"{n} receipt(s) delayed"


def _material_adjust(data, p) -> str:
    for m in data["materials"]:
        if p["material_id"] in (m["id"], m["code"]):
            q = float(p["quantity"])
            if q > 0:
                m.setdefault("supplies", []).append({"id": f"ADJ-{len(m['supplies'])}", "quantity": q, "kind": "ON_HAND"})
                return f"{m['code']} +{q:g}"
            # negative adjustment: reduce on-hand supplies
            left = -q
            for s in m.get("supplies", []):
                if s.get("kind") == "ON_HAND" and left > 0:
                    take = min(s["quantity"], left)
                    s["quantity"] -= take
                    left -= take
            m["supplies"] = [s for s in m["supplies"] if s["quantity"] > 0]
            return f"{m['code']} {q:g}"
    raise ChangeError(f"unknown material {p['material_id']}")


def _set_constraints(data, p) -> str:
    data["constraints"] = {**data.get("constraints", {}), **p["constraints"]}
    return "constraints updated"


def _set_objectives(data, p) -> str:
    data["objectives"] = {**data.get("objectives", {}), **p["objectives"]}
    return "objectives updated"


def _lock_sequence(data, p) -> str:
    r = _res(data, p["resource_id"])
    data.setdefault("sequence_constraints", []).append(
        {"id": p.get("id") or f"LOCK-{r['code']}-{len(data.get('sequence_constraints', []))}", "type": "LOCKED_SEQUENCE", "resource_ids": [r["id"]], "op_ids": list(p["op_ids"]), "description": "locked by planner"}
    )
    return f"sequence of {len(p['op_ids'])} operations locked on {r['code']}"


def _pin_resource(data, p) -> str:
    r = _res(data, p["resource_id"])
    for op in data["operations"]:
        if op["id"] == p["op_id"]:
            op["pinned_resource_id"] = r["id"]
            return f"{p['op_id']} pinned to {r['code']}"
    raise ChangeError(f"unknown operation {p['op_id']}")


_HANDLERS = {
    "ADD_DOWNTIME": _add_downtime,
    "REMOVE_RESOURCE": _remove_resource,
    "ADD_RESOURCE": _add_resource,
    "ADD_SHIFT": _add_shift,
    "SET_CALENDAR": _set_calendar,
    "CHANGE_EFFICIENCY": _change_efficiency,
    "CHANGE_CAPACITY": _change_capacity,
    "ABSENCE": _absence,
    "ADD_ORDER": _add_order,
    "CANCEL_ORDER": _cancel_order,
    "CHANGE_PRIORITY": _change_priority,
    "CHANGE_DUE_DATE": _change_due,
    "CHANGE_QUANTITY": _change_qty,
    "MATERIAL_DELAY": _material_delay,
    "MATERIAL_ADJUST": _material_adjust,
    "SET_CONSTRAINTS": _set_constraints,
    "SET_OBJECTIVES": _set_objectives,
    "LOCK_SEQUENCE": _lock_sequence,
    "PIN_RESOURCE": _pin_resource,
}
