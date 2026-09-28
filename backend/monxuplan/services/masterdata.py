"""Generic master-data service: one typed registry drives list / get / create / update / delete for
every master and transactional entity, with nested children, optimistic locking, audit and
tenant isolation. Entity-specific behaviour (order operation generation, rule validation…) is
added through hooks — never by duplicating CRUD code.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, time
from typing import Any

from sqlalchemy import Boolean, Date, DateTime, Float, Integer, Numeric, String, Text, Time, Uuid, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .. import models as M
from ..core.errors import Conflict, NotFound, ValidationFailed
from . import audit
from .context import Ctx


@dataclass
class ChildDef:
    name: str
    model: type
    fk: str
    order_by: str | None = None


@dataclass
class EntityDef:
    name: str
    model: type
    label: str
    search: list[str] = field(default_factory=list)
    order_by: str = "code"
    read_perm: str = "masterdata:read"
    write_perm: str = "masterdata:write"
    plant_scoped: bool = False
    children: list[ChildDef] = field(default_factory=list)
    readonly: set[str] = field(default_factory=set)
    fixed_filter: dict[str, Any] = field(default_factory=dict)
    refs: dict[str, str] = field(default_factory=dict)  # fk column -> entity name (for labels / lookups)
    before_save: Callable[[Session, Ctx, Any, dict], None] | None = None
    after_create: Callable[[Session, Ctx, Any, dict], None] | None = None
    soft_delete: str | None = None  # column to set False instead of deleting when referenced
    group: str = "Master data"


SYSTEM = {"id", "tenant_id", "created_at", "updated_at", "created_by", "updated_by", "version"}

REGISTRY: dict[str, EntityDef] = {}


def register(d: EntityDef) -> EntityDef:
    REGISTRY[d.name] = d
    return d


def get_def(name: str) -> EntityDef:
    d = REGISTRY.get(name)
    if d is None:
        raise NotFound(f"Unknown entity '{name}'", code="UNKNOWN_ENTITY")
    return d


# ---------------------------------------------------------------------------------------------
# (de)serialisation
# ---------------------------------------------------------------------------------------------


def to_json(v: Any) -> Any:
    if isinstance(v, uuid.UUID):
        return str(v)
    if isinstance(v, datetime | date | time):
        return v.isoformat()
    if isinstance(v, list):
        return [to_json(x) for x in v]
    if isinstance(v, dict):
        return {k: to_json(x) for k, x in v.items()}
    return v


def row_dict(obj: Any, skip: set[str] | None = None) -> dict[str, Any]:
    skip = skip or set()
    out = {}
    for c in obj.__table__.columns:
        if c.key in skip or c.key in ("password_hash", "secret_encrypted", "key_hash", "tenant_id"):
            continue
        out[c.key] = to_json(getattr(obj, c.key))
    return out


def _coerce(col, value: Any) -> Any:
    if value is None:
        return None
    t = col.type
    try:
        if isinstance(t, Uuid):
            return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))
        if isinstance(t, DateTime):
            if isinstance(value, datetime):
                return value
            dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            return dt
        if isinstance(t, Date):
            return value if isinstance(value, date) else date.fromisoformat(str(value)[:10])
        if isinstance(t, Time):
            return value if isinstance(value, time) else time.fromisoformat(str(value))
        if isinstance(t, Boolean):
            if isinstance(value, str):
                return value.strip().lower() in ("1", "true", "yes", "y", "si", "sí", "x")
            return bool(value)
        if isinstance(t, Integer):
            return int(value)
        if isinstance(t, Float | Numeric):
            return float(value)
        if isinstance(t, String | Text):
            s = str(value)
            if isinstance(t, String) and t.length and len(s) > t.length:
                raise ValidationFailed(f"'{col.key}' is longer than {t.length} characters", code="FIELD_TOO_LONG", context={"field": col.key})
            return s
    except (ValueError, TypeError) as exc:
        raise ValidationFailed(f"Invalid value for '{col.key}': {value!r}", code="INVALID_FIELD", context={"field": col.key}) from exc
    return value


def apply_fields(obj: Any, data: dict[str, Any], d: EntityDef | None = None, creating: bool = False) -> dict[str, Any]:
    cols = {c.key: c for c in obj.__table__.columns}
    changed = {}
    for k, v in data.items():
        if k in SYSTEM or k not in cols:
            continue
        if d is not None and k in d.readonly and not creating:
            continue
        nv = _coerce(cols[k], v)
        if getattr(obj, k) != nv:
            setattr(obj, k, nv)
            changed[k] = nv
    return changed


def schema(d: EntityDef) -> dict[str, Any]:
    """Field metadata used by the generic UI forms."""
    fields = []
    for c in d.model.__table__.columns:
        if c.key in SYSTEM:
            continue
        t = c.type
        kind = (
            "uuid" if isinstance(t, Uuid) else "datetime" if isinstance(t, DateTime) else "date" if isinstance(t, Date) else "time" if isinstance(t, Time)
            else "boolean" if isinstance(t, Boolean) else "integer" if isinstance(t, Integer) else "number" if isinstance(t, Float | Numeric) else "json" if c.type.__class__.__name__ in ("JSON", "Variant") else "string"
        )
        fields.append(
            {
                "name": c.key,
                "type": kind,
                "required": not c.nullable and c.default is None and c.server_default is None and not c.primary_key,
                "ref": d.refs.get(c.key),
                "readonly": c.key in d.readonly,
                "max_length": getattr(t, "length", None),
            }
        )
    return {"name": d.name, "label": d.label, "group": d.group, "fields": fields, "children": [{"name": ch.name, "fields": [c.key for c in ch.model.__table__.columns if c.key not in SYSTEM | {ch.fk}]} for ch in d.children]}


# ---------------------------------------------------------------------------------------------
# queries
# ---------------------------------------------------------------------------------------------


def list_rows(s: Session, ctx: Ctx, name: str, q: str | None = None, filters: dict[str, Any] | None = None, sort: str | None = None, offset: int = 0, limit: int = 100, plant_id: uuid.UUID | None = None) -> dict[str, Any]:
    d = get_def(name)
    ctx.require(d.read_perm)
    Mdl = d.model
    stmt = select(Mdl)
    count = select(func.count()).select_from(Mdl)
    conds = []
    for k, v in d.fixed_filter.items():
        conds.append(getattr(Mdl, k).in_(v) if isinstance(v, list | tuple) else getattr(Mdl, k) == v)
    if d.plant_scoped and plant_id is not None and hasattr(Mdl, "plant_id"):
        ctx.require_plant(plant_id)
        conds.append(Mdl.plant_id == plant_id)
    if q and d.search:
        like = f"%{q.lower()}%"
        conds.append(or_(*[func.lower(getattr(Mdl, c)).like(like) for c in d.search]))
    cols = {c.key: c for c in Mdl.__table__.columns}
    for k, v in (filters or {}).items():
        if k not in cols:
            continue
        if isinstance(v, list):
            conds.append(getattr(Mdl, k).in_([_coerce(cols[k], x) for x in v]))
        else:
            conds.append(getattr(Mdl, k) == _coerce(cols[k], v))
    for c in conds:
        stmt = stmt.where(c)
        count = count.where(c)
    order = sort or d.order_by
    desc = order.startswith("-")
    key = order.lstrip("-")
    if key in cols:
        stmt = stmt.order_by(getattr(Mdl, key).desc() if desc else getattr(Mdl, key))
    limit = max(1, min(limit, 5000))
    rows = list(s.scalars(stmt.offset(offset).limit(limit)))
    total = s.scalar(count) or 0
    items = [row_dict(r) for r in rows]
    _add_ref_labels(s, d, items)
    return {"items": items, "total": total, "offset": offset, "limit": limit}


def _add_ref_labels(s: Session, d: EntityDef, items: list[dict]) -> None:
    for col, ent in d.refs.items():
        ids = {it[col] for it in items if it.get(col)}
        if not ids:
            continue
        rd = REGISTRY.get(ent)
        if rd is None:
            continue
        Mdl = rd.model
        label_col = "code" if hasattr(Mdl, "code") else "number" if hasattr(Mdl, "number") else "name"
        labels = {str(i): lbl for i, lbl in s.execute(select(Mdl.id, getattr(Mdl, label_col)).where(Mdl.id.in_([uuid.UUID(x) for x in ids])))}
        for it in items:
            if it.get(col):
                it[col.removesuffix("_id") + "_label"] = labels.get(it[col])


def get_row(s: Session, ctx: Ctx, name: str, id_: uuid.UUID, with_children: bool = True) -> dict[str, Any]:
    d = get_def(name)
    ctx.require(d.read_perm)
    obj = s.get(d.model, id_)
    if obj is None or not _matches_fixed(d, obj):
        raise NotFound(f"{d.label} not found", code="NOT_FOUND")
    out = row_dict(obj)
    _add_ref_labels(s, d, [out])
    if with_children:
        for ch in d.children:
            stmt = select(ch.model).where(getattr(ch.model, ch.fk) == obj.id)
            if ch.order_by:
                stmt = stmt.order_by(getattr(ch.model, ch.order_by))
            out[ch.name] = [row_dict(c, skip={ch.fk}) for c in s.scalars(stmt)]
    return out


def _matches_fixed(d: EntityDef, obj: Any) -> bool:
    for k, v in d.fixed_filter.items():
        val = getattr(obj, k)
        if isinstance(v, list | tuple):
            if val not in v:
                return False
        elif val != v:
            return False
    return True


# ---------------------------------------------------------------------------------------------
# writes
# ---------------------------------------------------------------------------------------------


def create_row(s: Session, ctx: Ctx, name: str, data: dict[str, Any], reason: str | None = None) -> dict[str, Any]:
    d = get_def(name)
    ctx.require(d.write_perm)
    obj = d.model()
    obj.tenant_id = ctx.tenant_id
    for k, v in d.fixed_filter.items():
        if not isinstance(v, list | tuple):
            data.setdefault(k, v)
    apply_fields(obj, data, d, creating=True)
    if d.before_save:
        d.before_save(s, ctx, obj, data)
    if hasattr(obj, "plant_id") and getattr(obj, "plant_id", None) is not None:
        ctx.require_plant(obj.plant_id)
    s.add(obj)
    try:
        with s.begin_nested():
            s.flush()
    except IntegrityError as exc:
        raise Conflict(f"{d.label} could not be created: a record with the same code/number exists or a reference is invalid.", code="INTEGRITY_ERROR", context={"detail": str(exc.orig)[:300]}) from exc
    _write_children(s, ctx, d, obj, data)
    if d.after_create:
        d.after_create(s, ctx, obj, data)
    audit.record(s, ctx, "CREATE", d.name, obj.id, _label(obj), after=audit.snapshot(obj), reason=reason)
    return get_row(s, ctx, name, obj.id)


def update_row(s: Session, ctx: Ctx, name: str, id_: uuid.UUID, data: dict[str, Any], reason: str | None = None) -> dict[str, Any]:
    d = get_def(name)
    ctx.require(d.write_perm)
    obj = s.get(d.model, id_)
    if obj is None or not _matches_fixed(d, obj):
        raise NotFound(f"{d.label} not found", code="NOT_FOUND")
    if hasattr(obj, "version") and "version" in data and data["version"] is not None and int(data["version"]) != obj.version:
        raise Conflict(
            f"{d.label} was modified by someone else (version {obj.version}, you edited version {data['version']}). Reload and apply your change again.",
            code="VERSION_CONFLICT",
            context={"current_version": obj.version},
        )
    before = audit.snapshot(obj)
    apply_fields(obj, data, d)
    if d.before_save:
        d.before_save(s, ctx, obj, data)
    try:
        with s.begin_nested():
            s.flush()
    except IntegrityError as exc:
        raise Conflict(f"{d.label} could not be saved: duplicate code/number or invalid reference.", code="INTEGRITY_ERROR", context={"detail": str(exc.orig)[:300]}) from exc
    _write_children(s, ctx, d, obj, data)
    audit.record(s, ctx, "UPDATE", d.name, obj.id, _label(obj), before=before, after=audit.snapshot(obj), reason=reason)
    return get_row(s, ctx, name, obj.id)


def delete_row(s: Session, ctx: Ctx, name: str, id_: uuid.UUID, reason: str | None = None) -> dict[str, Any]:
    d = get_def(name)
    ctx.require(d.write_perm)
    obj = s.get(d.model, id_)
    if obj is None or not _matches_fixed(d, obj):
        raise NotFound(f"{d.label} not found", code="NOT_FOUND")
    before = audit.snapshot(obj)
    try:
        with s.begin_nested():
            for ch in d.children:
                for c in s.scalars(select(ch.model).where(getattr(ch.model, ch.fk) == obj.id)):
                    s.delete(c)
            s.delete(obj)
            s.flush()
        audit.record(s, ctx, "DELETE", d.name, id_, _label(obj), before=before, reason=reason)
        return {"deleted": True, "deactivated": False}
    except IntegrityError as exc:
        if d.soft_delete:
            obj = s.get(d.model, id_)
            setattr(obj, d.soft_delete, False)
            s.flush()
            audit.record(s, ctx, "DEACTIVATE", d.name, id_, _label(obj), before=before, reason=reason or "referenced by other records")
            return {"deleted": False, "deactivated": True, "reason": "The record is referenced by other data (orders, routings, plans): it was deactivated instead of deleted."}
        raise Conflict(f"{d.label} is referenced by other records and cannot be deleted.", code="IN_USE") from exc


def _write_children(s: Session, ctx: Ctx, d: EntityDef, obj: Any, data: dict[str, Any]) -> None:
    for ch in d.children:
        if ch.name not in data or data[ch.name] is None:
            continue
        rows = data[ch.name]
        if not isinstance(rows, list):
            raise ValidationFailed(f"'{ch.name}' must be a list")
        existing = {c.id: c for c in s.scalars(select(ch.model).where(getattr(ch.model, ch.fk) == obj.id))}
        keep: set[uuid.UUID] = set()
        for i, row in enumerate(rows):
            rid = row.get("id")
            c = existing.get(uuid.UUID(rid)) if rid else None
            if c is None:
                c = ch.model()
                c.tenant_id = ctx.tenant_id
                setattr(c, ch.fk, obj.id)
                s.add(c)
            try:
                apply_fields(c, row, None, creating=True)
            except ValidationFailed as exc:
                exc.message = f"{ch.name}[{i + 1}]: {exc.message}"
                raise
            s.flush()
            keep.add(c.id)
        for cid, c in existing.items():
            if cid not in keep:
                s.delete(c)
        s.flush()


def _label(obj: Any) -> str:
    for k in ("code", "number", "name", "username"):
        if hasattr(obj, k) and getattr(obj, k):
            return str(getattr(obj, k))
    return str(obj.id)


# ---------------------------------------------------------------------------------------------
# hooks
# ---------------------------------------------------------------------------------------------


def _validate_rule(s: Session, ctx: Ctx, obj: Any, data: dict) -> None:
    from monxuplan_engine.contract import RuleSpec
    from monxuplan_engine.rules import validate_rule

    errors = validate_rule(RuleSpec(id=obj.code or "new", name=obj.name or "", condition=obj.condition or {}, actions=obj.actions or []))
    if errors:
        raise ValidationFailed("Invalid rule: " + "; ".join(errors), code="INVALID_RULE", context={"errors": errors})


def _validate_resource(s: Session, ctx: Ctx, obj: Any, data: dict) -> None:
    if obj.kind not in ("MACHINE", "HUMAN", "TOOL", "LABOR_POOL", "WORK_CENTER", "SUBCONTRACTOR", "STORAGE", "TRANSPORT", "ROOM"):
        raise ValidationFailed(f"Unknown resource kind {obj.kind}", code="INVALID_KIND")
    if (obj.capacity or 0) < 0:
        raise ValidationFailed("Capacity cannot be negative")
    if not (0 < (obj.efficiency or 1) <= 5):
        raise ValidationFailed("Efficiency must be between 0 and 5 (1 = 100 %)")


def _validate_item(s: Session, ctx: Ctx, obj: Any, data: dict) -> None:
    if obj.max_lot is not None and obj.min_lot is not None and obj.max_lot < obj.min_lot:
        raise ValidationFailed("Maximum lot is smaller than minimum lot")
    if obj.item_type not in ("FINISHED", "SEMI_FINISHED", "RAW", "PACKAGING"):
        raise ValidationFailed(f"Unknown item type {obj.item_type}")


def _order_created(s: Session, ctx: Ctx, obj: Any, data: dict) -> None:
    """Generate the order's operations from the item's routing when none were given."""
    if data.get("operations"):
        return
    generate_order_operations(s, ctx, obj)


def generate_order_operations(s: Session, ctx: Ctx, order: M.ProductionOrder) -> int:
    routing = s.get(M.Routing, order.routing_id) if order.routing_id else s.scalar(select(M.Routing).where(M.Routing.item_id == order.item_id, M.Routing.is_active.is_(True)))
    if routing is None:
        return 0
    order.routing_id = routing.id
    if order.bom_id is None:
        order.bom_id = s.scalar(select(M.Bom.id).where(M.Bom.item_id == order.item_id, M.Bom.is_active.is_(True)))
    n = 0
    for ro in s.scalars(select(M.RoutingOperation).where(M.RoutingOperation.routing_id == routing.id).order_by(M.RoutingOperation.seq)):
        s.add(M.ProductionOrderOperation(tenant_id=ctx.tenant_id, order_id=order.id, routing_operation_id=ro.id, seq=ro.seq, code=ro.code, name=ro.name, status="PLANNED"))
        n += 1
    s.flush()
    return n


# ---------------------------------------------------------------------------------------------
# registry
# ---------------------------------------------------------------------------------------------

register(EntityDef("plants", M.Plant, "Plant", ["code", "name"], refs={"default_calendar_id": "calendars", "site_id": "sites"}, group="Organisation"))
register(EntityDef("sites", M.Site, "Site", ["code", "name"], group="Organisation"))
register(EntityDef("areas", M.PlanningArea, "Planning area", ["code", "name"], order_by="sort_order", plant_scoped=True, refs={"plant_id": "plants"}, group="Organisation"))
register(EntityDef("work-centers", M.WorkCenter, "Work center", ["code", "name"], plant_scoped=True, refs={"plant_id": "plants", "area_id": "areas", "calendar_id": "calendars"}, group="Organisation"))
register(
    EntityDef(
        "calendars", M.Calendar, "Calendar", ["code", "name"],
        children=[ChildDef("shifts", M.CalendarShift, "calendar_id", "weekday"), ChildDef("exceptions", M.CalendarException, "calendar_id", "start_local")],
        refs={"parent_id": "calendars"}, group="Calendars",
    )
)
register(
    EntityDef(
        "resources", M.Resource, "Resource", ["code", "name"], plant_scoped=True, soft_delete="is_active",
        refs={"plant_id": "plants", "area_id": "areas", "work_center_id": "work-centers", "calendar_id": "calendars"}, before_save=_validate_resource, group="Resources",
    )
)
register(EntityDef("machines", M.Resource, "Machine", ["code", "name"], plant_scoped=True, soft_delete="is_active", fixed_filter={"kind": ["MACHINE", "WORK_CENTER", "SUBCONTRACTOR"]}, refs={"plant_id": "plants", "area_id": "areas", "calendar_id": "calendars"}, before_save=_validate_resource, group="Resources"))
register(EntityDef("tools", M.Resource, "Tool", ["code", "name"], plant_scoped=True, soft_delete="is_active", fixed_filter={"kind": "TOOL"}, refs={"plant_id": "plants", "calendar_id": "calendars"}, before_save=_validate_resource, group="Resources"))
register(EntityDef("resource-groups", M.ResourceGroup, "Resource group", ["code", "name"], children=[ChildDef("members", M.ResourceGroupMember, "group_id")], refs={"plant_id": "plants"}, group="Resources"))
register(EntityDef("maintenance", M.Maintenance, "Maintenance", ["description", "kind"], order_by="start", refs={"resource_id": "resources"}, group="Resources"))
register(EntityDef("downtimes", M.Downtime, "Downtime", ["reason"], order_by="-start", refs={"resource_id": "resources"}, group="Resources"))
register(EntityDef("skills", M.Skill, "Skill", ["code", "name"], group="Labour"))
register(EntityDef("labor-pools", M.LaborPool, "Labour pool", ["code", "name"], plant_scoped=True, refs={"plant_id": "plants", "resource_id": "resources", "skill_id": "skills"}, group="Labour"))
register(EntityDef("operators", M.Operator, "Operator", ["code", "name"], plant_scoped=True, soft_delete="is_active", children=[ChildDef("skills", M.OperatorSkill, "operator_id")], refs={"plant_id": "plants", "labor_pool_id": "labor-pools", "calendar_id": "calendars"}, group="Labour"))
register(EntityDef("operator-absences", M.OperatorAbsence, "Operator absence", ["reason"], order_by="start", refs={"operator_id": "operators"}, group="Labour"))
register(EntityDef("tool-compatibility", M.ToolCompatibility, "Tool compatibility", [], order_by="tool_id", refs={"tool_id": "tools", "machine_id": "machines"}, group="Resources"))
register(EntityDef("product-families", M.ProductFamily, "Product family", ["code", "name"], group="Products"))
register(EntityDef("uoms", M.UnitOfMeasure, "Unit of measure", ["code", "name"], group="Products"))
register(EntityDef("items", M.Item, "Item", ["code", "name"], soft_delete="is_active", refs={"family_id": "product-families"}, before_save=_validate_item, group="Products"))
register(EntityDef("products", M.Item, "Product", ["code", "name"], soft_delete="is_active", fixed_filter={"item_type": ["FINISHED", "SEMI_FINISHED"]}, refs={"family_id": "product-families"}, before_save=_validate_item, group="Products"))
register(EntityDef("materials", M.Item, "Material", ["code", "name"], soft_delete="is_active", fixed_filter={"item_type": ["RAW", "PACKAGING"]}, refs={"family_id": "product-families"}, before_save=_validate_item, group="Products"))
register(EntityDef("boms", M.Bom, "BOM", ["version_code"], order_by="item_id", children=[ChildDef("lines", M.BomLine, "bom_id", "position")], refs={"item_id": "items"}, soft_delete="is_active", group="Products"))
register(EntityDef("routings", M.Routing, "Routing", ["version_code"], order_by="item_id", children=[ChildDef("operations", M.RoutingOperation, "routing_id", "seq"), ChildDef("precedences", M.OperationPrecedence, "routing_id")], refs={"item_id": "items", "plant_id": "plants"}, soft_delete="is_active", group="Products"))
register(EntityDef("routing-operations", M.RoutingOperation, "Routing operation", ["code", "name"], order_by="seq", children=[ChildDef("resources", M.OperationResource, "routing_operation_id", "preference")], refs={"routing_id": "routings", "labor_pool_id": "labor-pools", "tool_id": "tools"}, group="Products"))
register(EntityDef("setup-matrices", M.SetupMatrix, "Setup matrix", ["code", "name", "attribute"], children=[ChildDef("entries", M.SetupMatrixEntry, "matrix_id", "from_value")], refs={"resource_id": "resources", "group_id": "resource-groups"}, group="Planning configuration"))
register(EntityDef("setup-rules", M.SetupRule, "Setup rule", ["code", "description"], soft_delete="is_active", group="Planning configuration"))
register(EntityDef("sequence-rules", M.SequenceRule, "Sequence rule", ["code", "description"], soft_delete="is_active", group="Planning configuration"))
register(EntityDef("planning-rules", M.PlanningRule, "Planning rule", ["code", "name"], order_by="priority", before_save=_validate_rule, soft_delete="is_active", write_perm="admin:config", group="Planning configuration"))
register(EntityDef("optimization-profiles", M.OptimizationProfile, "Optimisation profile", ["code", "name"], write_perm="admin:config", group="Planning configuration"))
register(EntityDef("customers", M.Customer, "Customer", ["code", "name"], group="Business partners"))
register(EntityDef("suppliers", M.Supplier, "Supplier", ["code", "name"], group="Business partners"))
register(EntityDef("transfer-lanes", M.TransferLane, "Transfer lane", ["from_code", "to_code"], order_by="from_code", group="Supply chain"))
register(EntityDef("sales-orders", M.SalesOrder, "Sales order", ["number"], order_by="-number", read_perm="orders:read", write_perm="orders:write", children=[ChildDef("lines", M.SalesOrderLine, "sales_order_id", "line_no")], refs={"customer_id": "customers"}, group="Orders"))
register(
    EntityDef(
        "production-orders", M.ProductionOrder, "Production order", ["number", "notes"], order_by="due_date", read_perm="orders:read", write_perm="orders:write", plant_scoped=True,
        children=[ChildDef("operations", M.ProductionOrderOperation, "order_id", "seq")], refs={"item_id": "items", "customer_id": "customers", "plant_id": "plants", "routing_id": "routings", "bom_id": "boms"},
        after_create=_order_created, group="Orders",
    )
)
register(EntityDef("purchase-orders", M.PurchaseOrder, "Purchase order", ["number"], order_by="-number", read_perm="orders:read", write_perm="orders:write", children=[ChildDef("lines", M.PurchaseOrderLine, "purchase_order_id", "line_no")], refs={"supplier_id": "suppliers", "plant_id": "plants"}, group="Orders"))
register(EntityDef("inventory", M.Inventory, "Inventory", ["location"], order_by="item_id", read_perm="orders:read", write_perm="orders:write", plant_scoped=True, refs={"item_id": "items", "plant_id": "plants"}, group="Inventory"))
register(EntityDef("demands", M.Demand, "Demand", ["demand_type", "source"], order_by="period_start", read_perm="orders:read", write_perm="orders:write", refs={"item_id": "items", "plant_id": "plants"}, group="Demand"))
