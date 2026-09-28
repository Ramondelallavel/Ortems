"""Stored results of plan versions, at any size.

A plan of a plant with 100 000 orders a day holds ~200 000 scheduled operations, 100 000 order
results and ~200 000 pegging links per version. They are written in bulk (PostgreSQL ``COPY``) inside
the plan's transaction and read back with indexed, bounded queries — never as one document.
Operation explanations are stored compressed, one document per resource.
"""

from __future__ import annotations

import gzip
import json
import uuid
from collections import defaultdict
from collections.abc import Iterable, Iterator, Sequence
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from typing import Any

from pydantic import TypeAdapter
from pydantic_core import to_jsonable_python
from sqlalchemy import JSON, Table, func, select
from sqlalchemy.orm import Session

from monxuplan_engine.contract import Binding, Explanation, OrderResult, Solution

from ..models import ConstraintViolation, KpiValue, Plan, PlanDocument, PlanOrder, PlanPeg, PlanUnscheduled, ScheduledOperation

IN_CHUNK = 5000  # ids per IN list, well below every driver's bind-parameter limit
STORAGE_VERSION = 2  # plan.analysis["storage"]: results live in the plan_* tables
LATE_DETAIL_CAP = 500  # late orders kept in kpi_details (all of them are in plan_order)

_EXPLANATIONS = TypeAdapter(dict[str, Explanation])


def chunks(seq: Iterable, size: int = IN_CHUNK) -> Iterator[list]:
    lst = list(seq)
    for i in range(0, len(lst), size):
        yield lst[i : i + size]


def aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def iso(dt: datetime | None) -> str | None:
    """ISO-8601 like the engine's JSON output (``Z`` for UTC)."""
    if dt is None:
        return None
    dt = aware(dt)
    s = dt.isoformat()
    return s[:-6] + "Z" if s.endswith("+00:00") else s


# =============================================================================================
# Bulk writes
# =============================================================================================


def _is_json(col) -> bool:
    t = col.type
    return isinstance(t, JSON) or isinstance(getattr(t, "impl", None), JSON)


def bulk_insert(s: Session, table: Table, columns: Sequence[str], rows: list[tuple]) -> int:
    """Insert ``rows`` (tuples in ``columns`` order) in the session's transaction.

    PostgreSQL: ``COPY … FROM STDIN`` (the fastest path, ~10x an INSERT batch); other databases:
    one executemany. Python-side column defaults are not applied, so every column must be given.
    """
    if not rows:
        return 0
    conn = s.connection()
    if conn.dialect.name == "postgresql":
        cols = [table.c[n] for n in columns]
        json_idx = [i for i, c in enumerate(cols) if _is_json(c)]
        dumps = json.dumps
        quoted = ", ".join(f'"{n}"' for n in columns)
        raw = conn.connection.driver_connection
        with raw.cursor() as cur, cur.copy(f"COPY {table.name} ({quoted}) FROM STDIN") as cp:
            if json_idx:
                for r in rows:
                    vals = list(r)
                    for i in json_idx:
                        v = vals[i]
                        if v is not None:
                            vals[i] = dumps(v)
                    cp.write_row(vals)
            else:
                for r in rows:
                    cp.write_row(r)
    else:
        s.execute(table.insert(), [dict(zip(columns, r, strict=True)) for r in rows])
    return len(rows)


def _uuid_cache() -> Any:
    cache: dict[str | None, uuid.UUID | None] = {}

    def as_uuid(v: str | None) -> uuid.UUID | None:
        try:
            return cache[v]
        except KeyError:
            try:
                u = uuid.UUID(v) if v else None
            except ValueError:
                u = None
            cache[v] = u
            return u

    return as_uuid


def binding_json(b: Binding | None) -> dict[str, Any] | None:
    if b is None:
        return None
    return {"type": b.type, "ref": b.ref, "detail": b.detail, "at": iso(b.at), "wait_minutes": b.wait_minutes}


SO_COLUMNS = (
    "id",
    "tenant_id",
    "plan_id",
    "op_key",
    "order_id",
    "order_key",
    "order_operation_id",
    "resource_id",
    "resource_key",
    "secondary",
    "setup_start",
    "start",
    "end",
    "setup_minutes",
    "run_minutes",
    "working_minutes",
    "overtime_minutes",
    "quantity",
    "is_fixed",
    "fixed_reason",
    "is_locked",
    "is_late",
    "zone",
    "subcontracted",
    "prev_op_key",
    "material_ready",
    "binding",
    "cost",
)


def write_results(s: Session, tenant_id: uuid.UUID, plan: Plan, sol: Solution, op_rows: dict[str, tuple] | None = None) -> dict[str, int]:
    """Every per-plan table of a new plan version, in bulk. ``op_rows`` maps engine operation ids to
    ``(order_id, order_operation_id)`` database ids (from the problem builder)."""
    pid = plan.id
    op_rows = op_rows or {}
    new_id = uuid.uuid4
    as_uuid = _uuid_cache()
    counts: dict[str, int] = {}

    rows = []
    for x in sol.schedule:
        oid, ooid = op_rows.get(x.op_id, (None, None))
        rows.append(
            (
                new_id(),
                tenant_id,
                pid,
                x.op_id,
                oid if oid is not None else as_uuid(x.order_id),
                x.order_id,
                ooid,
                as_uuid(x.resource_id),
                x.resource_id,
                [{"resource_id": a.resource_id, "units": a.units} for a in x.secondary],
                x.setup_start,
                x.start,
                x.end,
                x.setup_minutes,
                x.run_minutes,
                x.working_minutes,
                x.overtime_minutes,
                x.quantity,
                x.fixed,
                x.fixed_reason,
                x.fixed_reason == "LOCKED",
                x.late,
                x.zone,
                x.subcontracted,
                x.prev_op_id,
                x.material_ready,
                binding_json(x.binding),
                x.cost,
            )
        )
    counts["operations"] = bulk_insert(s, ScheduledOperation.__table__, SO_COLUMNS, rows)
    del rows

    causes = {x["order_id"]: x.get("cause") for x in (sol.kpi_details or {}).get("late_orders", [])}
    counts["orders"] = bulk_insert(
        s,
        PlanOrder.__table__,
        ("id", "tenant_id", "plan_id", "order_key", "order_id", "number", "status", "start", "end", "due", "lateness_minutes", "weight", "earliest_possible_end", "limiting", "material_status", "rules_applied", "cause"),
        [
            (
                new_id(),
                tenant_id,
                pid,
                o.order_id,
                as_uuid(o.order_id),
                o.number,
                o.status,
                o.start,
                o.end,
                o.due,
                o.lateness_minutes,
                o.weight,
                o.earliest_possible_end,
                binding_json(o.limiting),
                o.material_status,
                list(o.rules_applied),
                causes.get(o.order_id),
            )
            for o in sol.orders
        ],
    )
    counts["pegging"] = bulk_insert(
        s,
        PlanPeg.__table__,
        ("id", "tenant_id", "plan_id", "material_id", "supply_id", "supply_kind", "supply_ref", "supply_order_id", "supply_time", "consumer_op_id", "consumer_order_id", "need_time", "quantity"),
        [(new_id(), tenant_id, pid, p.material_id, p.supply_id, p.supply_kind, p.supply_ref, p.supply_order_id, p.supply_time, p.consumer_op_id, p.consumer_order_id, p.need_time, p.quantity) for p in sol.pegging],
    )
    counts["unscheduled"] = bulk_insert(
        s,
        PlanUnscheduled.__table__,
        ("id", "tenant_id", "plan_id", "op_key", "order_key", "reason", "message", "details"),
        [(new_id(), tenant_id, pid, u.op_id, u.order_id, u.reason, u.message, to_jsonable_python(u.details)) for u in sol.unscheduled],
    )
    counts["violations"] = bulk_insert(
        s,
        ConstraintViolation.__table__,
        ("id", "tenant_id", "plan_id", "severity", "hardness", "type", "message", "order_key", "op_key", "resource_key", "material_key", "start", "end", "details"),
        [(new_id(), tenant_id, pid, v.severity, v.hardness, v.type, v.message[:4000], v.order_id, v.op_id, v.resource_id, v.material_id, v.start, v.end, to_jsonable_python(v.details)) for v in sol.violations],
    )
    computed = datetime.now(UTC)
    bulk_insert(
        s,
        KpiValue.__table__,
        ("id", "tenant_id", "plan_id", "code", "value", "computed_at"),
        [(new_id(), tenant_id, pid, k, float(v) if isinstance(v, int | float) else None, computed) for k, v in sol.kpis.items()],
    )
    counts["documents"] = write_documents(s, tenant_id, pid, build_documents(sol))
    return counts


# ------------------------------------------------------------------ read models (plan_document)

DOC_EXPLANATIONS, DOC_CAPACITY, DOC_CALENDAR, DOC_MATERIAL, DOC_CHAINS = "EXPLANATIONS", "CAPACITY", "CALENDAR", "MATERIAL", "CHAINS"


def build_documents(sol: Solution) -> list[tuple[str, str, int, bytes]]:
    """``(kind, key, item_count, json_bytes)`` of every read model of a solution: explanations per
    resource and, when the engine state is at hand, capacity profiles, resource calendars and
    material projections."""
    from monxuplan_engine.views import plan_views

    out: list[tuple[str, str, int, bytes]] = []
    if sol.explanations:
        res_of = {x.op_id: x.resource_id for x in sol.schedule}
        by_res: dict[str, dict[str, Explanation]] = defaultdict(dict)
        for op_id, ex in sol.explanations.items():
            rk = res_of.get(op_id)
            if rk is not None:  # unscheduled operations keep their reason in plan_unscheduled
                by_res[rk][op_id] = ex
        out.extend((DOC_EXPLANATIONS, rk, len(ops), _EXPLANATIONS.dump_json(ops)) for rk, ops in by_res.items())
    state = getattr(sol, "_state", None)
    if state is not None:
        views = plan_views(state)
        dumps = json.dumps
        out.extend((DOC_CAPACITY, size, len(v["rows"]), dumps(v, separators=(",", ":")).encode()) for size, v in views["CAPACITY"].items())
        out.extend((DOC_CALENDAR, rid, len(v["working"]), dumps(v, separators=(",", ":")).encode()) for rid, v in views["CALENDAR"].items())
        out.extend((DOC_MATERIAL, mid, len(v["points"]), dumps(v, separators=(",", ":")).encode()) for mid, v in views["MATERIAL"].items())
        out.extend((DOC_CHAINS, shard, len(v), dumps(v, separators=(",", ":")).encode()) for shard, v in views["CHAINS"].items())
    return out


def _gz(raw: bytes) -> bytes:
    return gzip.compress(raw, compresslevel=3)


def write_documents(s: Session, tenant_id: uuid.UUID, plan_id: uuid.UUID, docs: list[tuple[str, str, int, bytes]]) -> int:
    if not docs:
        return 0
    raws = [d[3] for d in docs]
    if len(raws) > 8:  # zlib releases the GIL: compress in parallel
        with ThreadPoolExecutor(max_workers=4) as pool:
            blobs = list(pool.map(_gz, raws, chunksize=16))
    else:
        blobs = [_gz(r) for r in raws]
    return bulk_insert(
        s,
        PlanDocument.__table__,
        ("id", "tenant_id", "plan_id", "kind", "key", "item_count", "data"),
        [(uuid.uuid4(), tenant_id, plan_id, kind, key, n, blob) for (kind, key, n, _raw), blob in zip(docs, blobs, strict=True)],
    )


def slim_kpi_details(details: dict[str, Any]) -> dict[str, Any]:
    """kpi_details without the unbounded lists (their full content is in plan_order)."""
    out = dict(details or {})
    late = out.get("late_orders") or []
    if len(late) > LATE_DETAIL_CAP:
        out["late_orders"] = late[:LATE_DETAIL_CAP]
    out["late_orders_total"] = len(late)
    return out


# =============================================================================================
# Reads
# =============================================================================================

_ORDER_COLS = (
    PlanOrder.order_key,
    PlanOrder.number,
    PlanOrder.status,
    PlanOrder.start,
    PlanOrder.end,
    PlanOrder.due,
    PlanOrder.lateness_minutes,
    PlanOrder.weight,
    PlanOrder.earliest_possible_end,
    PlanOrder.limiting,
    PlanOrder.material_status,
    PlanOrder.rules_applied,
    PlanOrder.cause,
)


def _legacy(plan: Plan, key: str) -> list[dict[str, Any]] | None:
    """Plans stored before the plan_* tables kept these lists in ``plan.analysis``."""
    an = plan.analysis or {}
    if an.get("storage", 1) >= STORAGE_VERSION:
        return None
    return list(an.get(key) or [])


def order_dict(r) -> dict[str, Any]:
    """Same shape as the engine's OrderResult JSON (plus the first cause of lateness)."""
    return {
        "order_id": r.order_key,
        "number": r.number,
        "status": r.status,
        "start": iso(r.start),
        "end": iso(r.end),
        "due": iso(r.due),
        "lateness_minutes": r.lateness_minutes,
        "weight": r.weight,
        "earliest_possible_end": iso(r.earliest_possible_end),
        "limiting": r.limiting,
        "material_status": r.material_status,
        "rules_applied": r.rules_applied or [],
        "cause": r.cause,
    }


def order_results(s: Session, plan: Plan, keys: Iterable[str] | None = None) -> dict[str, dict[str, Any]]:
    """Order results of a plan by order key — all of them, or only ``keys`` (chunked lookups)."""
    out: dict[str, dict[str, Any]] = {}
    if keys is None:
        for r in s.execute(select(*_ORDER_COLS).where(PlanOrder.plan_id == plan.id)):
            out[r.order_key] = order_dict(r)
    else:
        for part in chunks(set(keys)):
            for r in s.execute(select(*_ORDER_COLS).where(PlanOrder.plan_id == plan.id, PlanOrder.order_key.in_(part))):
                out[r.order_key] = order_dict(r)
    if not out:
        legacy = _legacy(plan, "orders")
        if legacy:
            wanted = None if keys is None else set(keys)
            out = {o["order_id"]: o for o in legacy if wanted is None or o["order_id"] in wanted}
    return out


def order_result(s: Session, plan: Plan, key: str) -> dict[str, Any] | None:
    return order_results(s, plan, [key]).get(key)


def order_count(s: Session, plan: Plan, status: str | None = None) -> int:
    q = select(func.count()).select_from(PlanOrder).where(PlanOrder.plan_id == plan.id)
    if status:
        q = q.where(PlanOrder.status == status)
    n = s.scalar(q) or 0
    if not n:
        legacy = _legacy(plan, "orders")
        if legacy:
            return sum(1 for o in legacy if status is None or o.get("status") == status)
    return n


def order_page(s: Session, plan: Plan, status: Sequence[str] | None = None, offset: int = 0, limit: int = 500, order_by: str = "lateness") -> list[dict[str, Any]]:
    q = select(*_ORDER_COLS).where(PlanOrder.plan_id == plan.id)
    if status:
        q = q.where(PlanOrder.status.in_(list(status)))
    q = q.order_by(PlanOrder.lateness_minutes.desc(), PlanOrder.order_key) if order_by == "lateness" else q.order_by(PlanOrder.due, PlanOrder.order_key)
    rows = [order_dict(r) for r in s.execute(q.offset(offset).limit(limit))]
    if not rows and offset == 0:
        legacy = _legacy(plan, "orders")
        if legacy:
            sel = [o for o in legacy if not status or o.get("status") in status]
            sel.sort(key=(lambda o: -(o.get("lateness_minutes") or 0)) if order_by == "lateness" else (lambda o: o.get("due") or ""))
            return sel[:limit]
    return rows


def material_status_counts(s: Session, plan: Plan) -> dict[str, int]:
    rows = s.execute(select(PlanOrder.material_status, func.count()).where(PlanOrder.plan_id == plan.id).group_by(PlanOrder.material_status)).all()
    if rows:
        return {k or "NONE": n for k, n in rows}
    counts: dict[str, int] = defaultdict(int)
    for o in _legacy(plan, "orders") or []:
        counts[o.get("material_status") or "NONE"] += 1
    return dict(counts)


def material_status_keys(s: Session, plan: Plan, statuses: Iterable[str]) -> dict[str, str]:
    """Order keys whose material status is one of ``statuses`` (→ status)."""
    wanted = list(statuses)
    rows = s.execute(select(PlanOrder.order_key, PlanOrder.material_status).where(PlanOrder.plan_id == plan.id, PlanOrder.material_status.in_(wanted))).all()
    if rows:
        return dict(rows)
    return {o["order_id"]: o.get("material_status") for o in _legacy(plan, "orders") or [] if o.get("material_status") in wanted}


def status_counts(s: Session, plan: Plan) -> dict[str, int]:
    rows = s.execute(select(PlanOrder.status, func.count()).where(PlanOrder.plan_id == plan.id).group_by(PlanOrder.status)).all()
    if rows:
        return dict(rows)
    counts: dict[str, int] = defaultdict(int)
    for o in _legacy(plan, "orders") or []:
        counts[o.get("status") or "?"] += 1
    return dict(counts)


def cause_counts(s: Session, plan: Plan) -> tuple[dict[str, int], dict[str, int]]:
    """First causes of late / unscheduled orders: by category and by resource (all orders)."""
    by_cat: dict[str, int] = defaultdict(int)
    by_res: dict[str, int] = defaultdict(int)
    for (cause,) in s.execute(select(PlanOrder.cause).where(PlanOrder.plan_id == plan.id, PlanOrder.cause.is_not(None))):
        if not isinstance(cause, dict):
            continue
        by_cat[cause.get("category") or "Unknown"] += 1
        if cause.get("resource"):
            by_res[cause["resource"]] += 1
    return dict(by_cat), dict(by_res)


_PEG_COLS = (
    PlanPeg.material_id,
    PlanPeg.supply_id,
    PlanPeg.supply_kind,
    PlanPeg.supply_ref,
    PlanPeg.supply_order_id,
    PlanPeg.supply_time,
    PlanPeg.consumer_op_id,
    PlanPeg.consumer_order_id,
    PlanPeg.need_time,
    PlanPeg.quantity,
)


def peg_dict(r) -> dict[str, Any]:
    return {
        "material_id": r.material_id,
        "supply_id": r.supply_id,
        "supply_kind": r.supply_kind,
        "supply_ref": r.supply_ref,
        "supply_order_id": r.supply_order_id,
        "supply_time": iso(r.supply_time),
        "consumer_op_id": r.consumer_op_id,
        "consumer_order_id": r.consumer_order_id,
        "need_time": iso(r.need_time),
        "quantity": r.quantity,
    }


def pegs(
    s: Session,
    plan: Plan,
    *,
    consumer_orders: Iterable[str] | None = None,
    supply_orders: Iterable[str] | None = None,
    material_id: str | None = None,
    consumer_op: str | None = None,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    """Pegging links of a plan, filtered (every filter is backed by an index)."""
    base = select(*_PEG_COLS).where(PlanPeg.plan_id == plan.id)
    if material_id is not None:
        base = base.where(PlanPeg.material_id == material_id)
    if consumer_op is not None:
        base = base.where(PlanPeg.consumer_op_id == consumer_op)
    out: list[dict[str, Any]] = []
    if consumer_orders is not None or supply_orders is not None:
        c_keys, s_keys = list(consumer_orders or []), list(supply_orders or [])
        seen: set[tuple] = set()
        for field, keys in ((PlanPeg.consumer_order_id, c_keys), (PlanPeg.supply_order_id, s_keys)):
            for part in chunks(keys):
                for r in s.execute(base.where(field.in_(part))):
                    k = (r.consumer_op_id, r.material_id, r.supply_id, r.quantity)
                    if k not in seen:
                        seen.add(k)
                        out.append(peg_dict(r))
    else:
        q = base.limit(limit) if limit else base
        out = [peg_dict(r) for r in s.execute(q)]
    if not out:
        legacy = _legacy(plan, "pegging")
        if legacy:
            c_set = set(consumer_orders) if consumer_orders is not None else None
            s_set = set(supply_orders) if supply_orders is not None else None

            def keep(p: dict[str, Any]) -> bool:
                if material_id is not None and p.get("material_id") != material_id:
                    return False
                if consumer_op is not None and p.get("consumer_op_id") != consumer_op:
                    return False
                if c_set is None and s_set is None:
                    return True
                return (c_set is not None and p.get("consumer_order_id") in c_set) or (s_set is not None and p.get("supply_order_id") in s_set)

            out = [p for p in legacy if keep(p)]
            if limit:
                out = out[:limit]
    return out


_UNS_COLS = (PlanUnscheduled.op_key, PlanUnscheduled.order_key, PlanUnscheduled.reason, PlanUnscheduled.message, PlanUnscheduled.details)


def unscheduled_dict(r) -> dict[str, Any]:
    return {"op_id": r.op_key, "order_id": r.order_key, "reason": r.reason, "message": r.message, "details": r.details or {}}


def unscheduled(s: Session, plan: Plan, *, op_key: str | None = None, order_key: str | None = None, reason: str | None = None, offset: int = 0, limit: int | None = None) -> list[dict[str, Any]]:
    q = select(*_UNS_COLS).where(PlanUnscheduled.plan_id == plan.id)
    if op_key is not None:
        q = q.where(PlanUnscheduled.op_key == op_key)
    if order_key is not None:
        q = q.where(PlanUnscheduled.order_key == order_key)
    if reason is not None:
        q = q.where(PlanUnscheduled.reason == reason)
    q = q.order_by(PlanUnscheduled.order_key, PlanUnscheduled.op_key).offset(offset)
    if limit:
        q = q.limit(limit)
    out = [unscheduled_dict(r) for r in s.execute(q)]
    if not out and offset == 0:
        legacy = _legacy(plan, "unscheduled")
        if legacy:
            out = [u for u in legacy if (op_key is None or u.get("op_id") == op_key) and (order_key is None or u.get("order_id") == order_key) and (reason is None or u.get("reason") == reason)]
            if limit:
                out = out[:limit]
    return out


def unscheduled_count(s: Session, plan: Plan) -> int:
    counts = (plan.analysis or {}).get("counts") or {}
    if "unscheduled" in counts:
        return int(counts["unscheduled"])
    n = s.scalar(select(func.count()).select_from(PlanUnscheduled).where(PlanUnscheduled.plan_id == plan.id)) or 0
    return n or len(_legacy(plan, "unscheduled") or [])


def document(s: Session, plan: Plan, kind: str, key: str) -> Any | None:
    """One read model of a plan (None when the plan has no such document)."""
    blob = s.scalar(select(PlanDocument.data).where(PlanDocument.plan_id == plan.id, PlanDocument.kind == kind, PlanDocument.key == key))
    return None if blob is None else json.loads(gzip.decompress(blob))


def documents(s: Session, plan: Plan, kind: str, keys: Iterable[str] | None = None) -> dict[str, Any]:
    """Several read models of one kind, by key (all of them when ``keys`` is None)."""
    base = select(PlanDocument.key, PlanDocument.data).where(PlanDocument.plan_id == plan.id, PlanDocument.kind == kind)
    rows = []
    if keys is None:
        rows = s.execute(base).all()
    else:
        for part in chunks(set(keys)):
            rows.extend(s.execute(base.where(PlanDocument.key.in_(part))).all())
    return {k: json.loads(gzip.decompress(b)) for k, b in rows}


def has_documents(s: Session, plan: Plan, kind: str) -> bool:
    return s.scalar(select(PlanDocument.id).where(PlanDocument.plan_id == plan.id, PlanDocument.kind == kind).limit(1)) is not None


def explanations_for(s: Session, plan: Plan, resource_key: str) -> dict[str, Any]:
    """Explanations of every operation of one resource in a plan (one compressed document)."""
    return document(s, plan, DOC_EXPLANATIONS, resource_key) or {}


def explanation(s: Session, plan: Plan, resource_key: str | None, op_key: str) -> dict[str, Any] | None:
    if not resource_key:
        return None
    return explanations_for(s, plan, resource_key).get(op_key)


def order_results_models(s: Session, plan: Plan) -> list[OrderResult]:
    """Order results as engine models (for comparisons), built without re-validation."""
    out = []
    for r in s.execute(select(*_ORDER_COLS).where(PlanOrder.plan_id == plan.id)):
        lim = r.limiting
        out.append(
            OrderResult.fast(
                order_id=r.order_key,
                number=r.number,
                status=r.status,
                start=aware(r.start),
                end=aware(r.end),
                due=aware(r.due),
                lateness_minutes=r.lateness_minutes,
                weight=r.weight,
                earliest_possible_end=aware(r.earliest_possible_end),
                limiting=binding_from(lim) if lim else None,
                material_status=r.material_status,
                rules_applied=list(r.rules_applied or []),
            )
        )
    if not out:
        legacy = _legacy(plan, "orders")
        if legacy:
            out = [OrderResult.model_validate(o) for o in legacy]
    return out


def parse_dt(v: Any) -> datetime | None:
    if v is None or isinstance(v, datetime):
        return aware(v)
    return aware(datetime.fromisoformat(str(v).replace("Z", "+00:00")))


def binding_from(d: dict[str, Any] | None) -> Binding:
    d = d or {}
    return Binding.fast(type=d.get("type") or "NONE", ref=d.get("ref"), detail=d.get("detail"), at=parse_dt(d.get("at")), wait_minutes=int(d.get("wait_minutes") or 0))
