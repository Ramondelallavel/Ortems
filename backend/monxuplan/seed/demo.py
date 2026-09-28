"""Demo factory: Monxu Manufacturing — Sevilla Plant (plus a small Querétaro plant).

Deterministic (seeded) and relative to "today", so the demo always looks current:

* 5 areas (Cutting, Machining, Assembly, Painting, Packaging) + heat treatment,
* 11 machines named in the brief + heat-treatment oven + subcontractor, 24 tools, 5 labour pools with
  64 operators on two shifts (individual calendars, some absences),
* 50 finished products in 5 families, 20 machined sub-assemblies, raw materials, purchased
  components, paint and packaging (≈165 items), multi-level BOMs and routings with alternatives,
* sequence-dependent setups (paint colour matrix, CNC family matrix, cutting grade matrix),
* 1 000+ production orders: ~650 completed with MES actuals (history, plan vs actual) and ~350 open
  (urgent, normal, past-due, make-to-order and make-to-stock),
* purchase orders (one supplier late), a material shortage, maintenance on CNC-04 today,
  operations in progress, planning rules, sequence rules, optimisation presets and forecasts.

Nothing here simulates solver results: plans are produced by running the real engine.
"""

from __future__ import annotations

import random
import uuid
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from monxuplan_engine.objectives import PRESETS

from ..core.clock import now
from ..core.db import new_session
from ..core.security import encrypt_secret
from ..models import (
    ActualProduction,
    Bom,
    BomLine,
    Calendar,
    CalendarException,
    CalendarShift,
    Company,
    Customer,
    Demand,
    Integration,
    Inventory,
    Item,
    LaborPool,
    Maintenance,
    OperationResource,
    Operator,
    OperatorAbsence,
    OperatorSkill,
    OptimizationProfile,
    PlanningArea,
    PlanningRule,
    Plant,
    ProductFamily,
    ProductionOrder,
    ProductionOrderOperation,
    PurchaseOrder,
    PurchaseOrderLine,
    Resource,
    ResourceGroup,
    ResourceGroupMember,
    Routing,
    RoutingOperation,
    SalesOrder,
    SalesOrderLine,
    Scenario,
    ScenarioChange,
    SequenceRule,
    SetupMatrix,
    SetupMatrixEntry,
    SetupRule,
    Site,
    Skill,
    Supplier,
    Tenant,
    ToolCompatibility,
    UnitOfMeasure,
    WorkCenter,
)
from ..services.auth import create_user, ensure_roles

SEED = 20260928
TZ = "Europe/Madrid"
DEMO_PASSWORD = "Monxu-Demo-2026"

FAMILIES = [
    ("HYD", "Hydraulic cylinders", "#2f6fb3"),
    ("VAL", "Valve blocks", "#6a8f3b"),
    ("BRK", "Brackets & supports", "#9a6b2f"),
    ("PMP", "Pump housings", "#7a4f9a"),
    ("FRM", "Welded frames", "#4f7f86"),
]
COLORS = ["RAL9010", "RAL7035", "RAL1023", "RAL5010", "RAL3000", "RAL9005"]  # light → dark
GRADES = ["S355", "42CrMo4", "AISI304", "AL6082"]


def _local(d: date, t: time) -> datetime:
    return datetime.combine(d, t).replace(tzinfo=ZoneInfo(TZ)).astimezone(UTC)


class Builder:
    def __init__(self, s: Session, tenant_id: uuid.UUID, today: date, rng: random.Random) -> None:
        self.s = s
        self.tid = tenant_id
        self.today = today
        self.rng = rng
        self.stats: dict[str, int] = {}

    def add(self, obj: Any) -> Any:
        if hasattr(obj, "tenant_id") and getattr(obj, "tenant_id", None) is None:
            obj.tenant_id = self.tid
        self.s.add(obj)
        name = type(obj).__name__
        self.stats[name] = self.stats.get(name, 0) + 1
        return obj


def seed_demo(reset: bool = False, today: date | None = None) -> dict[str, Any]:
    """Create the demo tenant. Returns counts. Idempotent unless ``reset``."""
    with new_session(None, "seed") as s:
        existing = s.scalar(select(Tenant).where(Tenant.slug == "monxu"))
        if existing is not None and not reset:
            return {"status": "exists", "tenant_id": str(existing.id)}
        if existing is not None and reset:
            _delete_tenant(s, existing.id)
            s.commit()
        tenant = Tenant(name="Monxu Manufacturing", slug="monxu", settings={"demo": True})
        s.add(tenant)
        s.commit()
        tid = tenant.id
    rng = random.Random(SEED)
    local_today = today or now().astimezone(ZoneInfo(TZ)).date()
    with new_session(tid, "seed") as s:
        b = Builder(s, tid, local_today, rng)
        result = _build(b)
        s.commit()
    result["tenant_id"] = str(tid)
    return result


def _delete_tenant(s: Session, tid: uuid.UUID) -> None:
    from ..core.db import Base

    # delete in reverse dependency order, tenant scoped
    for table in reversed(Base.metadata.sorted_tables):
        if "tenant_id" in table.c:
            s.execute(table.delete().where(table.c.tenant_id == tid))
    s.execute(Tenant.__table__.delete().where(Tenant.__table__.c.id == tid))


def _build(b: Builder) -> dict[str, Any]:
    s, rng, today = b.s, b.rng, b.today
    ensure_roles(s, b.tid)
    company = b.add(Company(code="MONXU", name="Monxu Manufacturing", currency="EUR", locale="es"))
    s.flush()
    site = b.add(Site(company_id=company.id, code="SEV", name="Sevilla", country="ES"))
    site_mx = b.add(Site(company_id=company.id, code="QRO", name="Querétaro", country="MX"))
    s.flush()

    # ------------------------------------------------------------------ calendars
    def calendar(code: str, name: str, tz: str, shifts: list[tuple[int, str, str, str, str]], breaks: bool = True) -> Calendar:
        c = b.add(Calendar(code=code, name=name, timezone=tz))
        s.flush()
        for wd, sc, st, en, kind in shifts:
            brk = [{"start": "10:00", "end": "10:20"}] if (breaks and st == "06:00") else ([{"start": "18:00", "end": "18:20"}] if (breaks and st == "14:00") else [])
            b.add(CalendarShift(calendar_id=c.id, weekday=wd, shift_code=sc, start_time=time.fromisoformat(st), end_time=time.fromisoformat(en), kind=kind, breaks=brk))
        return c

    two = [(d, "M", "06:00", "14:00", "REGULAR") for d in range(5)] + [(d, "T", "14:00", "22:00", "REGULAR") for d in range(5)] + [(5, "OT", "06:00", "14:00", "OVERTIME")]
    cal2 = calendar("SEV-2S", "Sevilla two shifts Mon–Fri (+Sat overtime)", TZ, two)
    cal1 = calendar("SEV-1S", "Sevilla one shift Mon–Fri", TZ, [(d, "M", "06:00", "14:00", "REGULAR") for d in range(5)])
    cal3 = calendar("SEV-24x5", "Sevilla 24×5 (ovens)", TZ, [(d, "N", "00:00", "00:00", "REGULAR") for d in range(5)], breaks=False)
    cal_m = calendar("SEV-OP-M", "Operators morning shift", TZ, [(d, "M", "06:00", "14:00", "REGULAR") for d in range(5)] + [(5, "OT", "06:00", "14:00", "OVERTIME")])
    cal_t = calendar("SEV-OP-T", "Operators afternoon shift", TZ, [(d, "T", "14:00", "22:00", "REGULAR") for d in range(5)])
    cal_qro = calendar("QRO-2S", "Querétaro two shifts", "America/Mexico_City", [(d, "M", "07:00", "15:00", "REGULAR") for d in range(6)] + [(d, "T", "15:00", "23:00", "REGULAR") for d in range(5)])
    s.flush()
    # holidays (Spain national day, All Saints observed) and an extra Saturday shift
    for cal in (cal2, cal1, cal3, cal_m, cal_t):
        for hd, reason in ((date(today.year, 10, 12), "Fiesta Nacional de España"), (date(today.year, 11, 1), "Todos los Santos"), (date(today.year, 12, 8), "Inmaculada Concepción")):
            b.add(CalendarException(calendar_id=cal.id, start_local=datetime.combine(hd, time(0)), end_local=datetime.combine(hd + timedelta(days=1), time(0)), kind="HOLIDAY", reason=reason))

    plant = b.add(Plant(site_id=site.id, code="SEV", name="Sevilla Plant", timezone=TZ, country="ES", default_calendar_id=cal2.id))
    plant_mx = b.add(Plant(site_id=site_mx.id, code="QRO", name="Querétaro Plant", timezone="America/Mexico_City", country="MX", default_calendar_id=cal_qro.id))
    s.flush()

    areas = {}
    for k, (code, name) in enumerate([("CUT", "Cutting"), ("MACH", "Machining"), ("HT", "Heat treatment"), ("ASM", "Assembly"), ("PNT", "Painting"), ("PCK", "Packaging")]):
        areas[code] = b.add(PlanningArea(plant_id=plant.id, code=code, name=name, sort_order=k))
    s.flush()
    wcs = {}
    for code, name, area, cal in [("WC-CUT", "Cutting cell", "CUT", cal2), ("WC-CNC", "CNC machining", "MACH", cal2), ("WC-HT", "Heat treatment", "HT", cal3), ("WC-ASM", "Assembly lines", "ASM", cal2), ("WC-PNT", "Paint shop", "PNT", cal2), ("WC-PCK", "Packing", "PCK", cal1)]:
        wcs[code] = b.add(WorkCenter(plant_id=plant.id, area_id=areas[area].id, code=code, name=name, calendar_id=cal.id))
    s.flush()

    # ------------------------------------------------------------------ resources
    res: dict[str, Resource] = {}

    def machine(code: str, name: str, area: str, wc: str, cal: Calendar | None, **kw) -> Resource:
        r = b.add(Resource(plant_id=plant.id, area_id=areas[area].id, work_center_id=wcs[wc].id, code=code, name=name, kind=kw.pop("kind", "MACHINE"), calendar_id=cal.id if cal else None, **kw))
        res[code] = r
        return r

    machine("CUT-01", "Band saw Behringer HBE 320", "CUT", "WC-CUT", cal2, cost_per_hour=38, energy_kw=7.5, co2_kg_per_kwh=0.19, attributes={"max_diameter": 320})
    machine("CUT-02", "Laser cutter 4 kW", "CUT", "WC-CUT", cal2, cost_per_hour=62, energy_kw=22, co2_kg_per_kwh=0.19, efficiency=0.9, attributes={"max_diameter": 200})
    for k, (eff, cost, axes) in enumerate([(0.9, 58, 3), (0.95, 60, 3), (1.0, 72, 5), (0.92, 70, 5)], 1):
        machine(f"CNC-0{k}", f"CNC machining centre #{k} ({axes}-axis)", "MACH", "WC-CNC", cal2, efficiency=eff, cost_per_hour=cost, overtime_cost_per_hour=cost * 1.5, setup_cost_per_hour=45, energy_kw=15, co2_kg_per_kwh=0.19, setup_combine="MAX", detached_setup=True, attributes={"axes": axes}, initial_state={"family": ["HYD", "VAL", "PMP", "BRK"][k - 1]})
    machine("HT-01", "Tempering furnace (batch 50)", "HT", "WC-HT", cal3, cost_per_hour=85, energy_kw=90, co2_kg_per_kwh=0.19)
    machine("ASM-01", "Assembly line 1", "ASM", "WC-ASM", cal2, cost_per_hour=41)
    machine("ASM-02", "Assembly line 2", "ASM", "WC-ASM", cal2, cost_per_hour=41, efficiency=0.9)
    machine("PAINT-01", "Powder paint line", "PNT", "WC-PNT", cal2, cost_per_hour=55, setup_cost_per_hour=60, energy_kw=40, co2_kg_per_kwh=0.19, initial_state={"color": "RAL9010"}, detached_setup=True)
    machine("PACK-01", "Packing station 1", "PCK", "WC-PCK", cal1, cost_per_hour=30)
    machine("PACK-02", "Packing station 2", "PCK", "WC-PCK", cal1, cost_per_hour=30)
    sub_ht = b.add(Resource(plant_id=plant.id, area_id=areas["HT"].id, code="SUB-HT", name="Tratamientos Térmicos del Sur (subcontractor)", kind="SUBCONTRACTOR", is_finite=False))
    res["SUB-HT"] = sub_ht
    s.flush()
    # tools
    tools = {}
    for k in range(1, 31):
        code = f"T-{k:02d}"
        copies = 1 if k in (18, 22) else 2
        t = b.add(Resource(plant_id=plant.id, area_id=areas["MACH"].id, code=code, name=f"Fixture/tool {code}", kind="TOOL", capacity=copies, calendar_id=None, is_finite=True))
        tools[code] = t
    s.flush()
    for code in ("CNC-01", "CNC-02", "CNC-03", "CNC-04"):
        for tcode in ("T-18",):
            if code != "CNC-01":  # T-18 does not fit CNC-01
                b.add(ToolCompatibility(tool_id=tools[tcode].id, machine_id=res[code].id))
    b.add(Maintenance(resource_id=tools["T-22"].id, start=_local(today + timedelta(days=3), time(6)), end=_local(today + timedelta(days=4), time(14)), kind="PREVENTIVE", description="Regrinding T-22"))

    groups = {}
    for code, name, members in [("CUTTING", "Cutting", ["CUT-01", "CUT-02"]), ("CNC", "Fresado / CNC", ["CNC-01", "CNC-02", "CNC-03", "CNC-04"]), ("CNC-5AX", "5-axis CNC", ["CNC-03", "CNC-04"]), ("ASSEMBLY", "Assembly", ["ASM-01", "ASM-02"]), ("PACKING", "Packing", ["PACK-01", "PACK-02"])]:
        g = b.add(ResourceGroup(plant_id=plant.id, code=code, name=name))
        s.flush()
        groups[code] = g
        for m in members:
            b.add(ResourceGroupMember(group_id=g.id, resource_id=res[m].id))

    # skills, labour pools and operators
    skills = {code: b.add(Skill(code=code, name=name)) for code, name in [("CUT", "Cutting"), ("CNC", "CNC machining"), ("CNC5", "5-axis programming"), ("ASM", "Assembly"), ("PAINT", "Painting"), ("PACK", "Packing"), ("QA", "Quality inspection")]}
    s.flush()
    pools = {}
    operator_count = 0
    first_names = ["Alba", "Bruno", "Carla", "Dario", "Elena", "Fermin", "Gema", "Hugo", "Irene", "Jorge", "Lucia", "Mario", "Nerea", "Oscar", "Paula", "Raul", "Sara", "Tomas", "Ursula", "Victor", "Yolanda", "Zoe"]
    for code, name, skill, per_shift, mpo in [("POOL-CUT", "Cutting operators", "CUT", 3, 1), ("POOL-CNC", "CNC operators", "CNC", 5, 1), ("POOL-ASM", "Assembly operators", "ASM", 5, 1), ("POOL-PNT", "Paint operators", "PAINT", 3, 1), ("POOL-PCK", "Packers", "PACK", 3, 1)]:
        r = b.add(Resource(plant_id=plant.id, code=code, name=name, kind="LABOR_POOL", capacity=per_shift * 2, calendar_id=None))
        s.flush()
        lp = b.add(LaborPool(plant_id=plant.id, resource_id=r.id, code=code, name=name, skill_id=skills[skill].id, machines_per_operator=mpo))
        s.flush()
        pools[code] = lp
        res[code] = r
        for shift_cal in (cal_m, cal_t):
            for _ in range(per_shift + (1 if code in ("POOL-ASM", "POOL-PCK") else 0)):
                operator_count += 1
                op = b.add(Operator(plant_id=plant.id, code=f"OP{operator_count:03d}", name=f"{first_names[operator_count % len(first_names)]} {chr(65 + operator_count % 26)}.", labor_pool_id=lp.id, calendar_id=shift_cal.id, cost_per_hour=24))
                s.flush()
                b.add(OperatorSkill(operator_id=op.id, skill_id=skills[skill].id, level=rng.randint(1, 3)))
                if skill == "CNC" and rng.random() < 0.4:
                    b.add(OperatorSkill(operator_id=op.id, skill_id=skills["CNC5"].id, level=2))
                if rng.random() < 0.18:
                    b.add(OperatorSkill(operator_id=op.id, skill_id=skills["QA"].id, level=1))
    # absences: two CNC operators on holiday next week (labour becomes a constraint)
    cnc_ops = list(s.scalars(select(Operator).where(Operator.labor_pool_id == pools["POOL-CNC"].id)))
    for op in cnc_ops[:2]:
        b.add(OperatorAbsence(operator_id=op.id, start=_local(today + timedelta(days=7), time(0)), end=_local(today + timedelta(days=12), time(0)), reason="Vacation"))
    # machines ~ extra kinds so the total reaches 100+ resources incl. operators
    # Querétaro: small secondary plant
    for code, name in [("QRO-CUT-01", "Saw Querétaro"), ("QRO-CNC-01", "CNC Querétaro 1"), ("QRO-CNC-02", "CNC Querétaro 2"), ("QRO-ASM-01", "Assembly Querétaro 1"), ("QRO-ASM-02", "Assembly Querétaro 2"), ("QRO-PACK-01", "Packing Querétaro")]:
        b.add(Resource(plant_id=plant_mx.id, code=code, name=name, kind="MACHINE", calendar_id=cal_qro.id, cost_per_hour=35))
    for code, name, kind in [("FLT-01", "Forklift 1", "TRANSPORT"), ("FLT-02", "Forklift 2", "TRANSPORT"), ("WH-SEV", "Sevilla warehouse", "STORAGE")]:
        b.add(Resource(plant_id=plant.id, code=code, name=name, kind=kind, capacity=1, is_finite=False))
    s.flush()

    # maintenance: CNC-04 today 14:00–18:00 (example of the brief), PAINT-01 on Saturday
    b.add(Maintenance(resource_id=res["CNC-04"].id, start=_local(today, time(14)), end=_local(today, time(18)), kind="PREVENTIVE", description="Spindle bearing inspection"))
    b.add(Maintenance(resource_id=res["CNC-03"].id, start=_local(today + timedelta(days=9), time(6)), end=_local(today + timedelta(days=9), time(14)), kind="PLANNED", description="Ballscrew replacement"))
    b.add(Maintenance(resource_id=res["PAINT-01"].id, start=_local(today + timedelta(days=4), time(18)), end=_local(today + timedelta(days=4), time(22)), kind="PREVENTIVE", description="Booth filter change"))

    # ------------------------------------------------------------------ setup matrices, rules
    paint = b.add(SetupMatrix(code="PAINT-COLOR", name="Paint colour changeover", attribute="color", same_minutes=5, default_minutes=40, resource_id=res["PAINT-01"].id))
    cncm = b.add(SetupMatrix(code="CNC-FAMILY", name="CNC fixture change by family", attribute="family", same_minutes=10, default_minutes=45, group_id=groups["CNC"].id))
    cutm = b.add(SetupMatrix(code="CUT-GRADE", name="Cutting blade/program change by grade", attribute="grade", same_minutes=5, default_minutes=20, group_id=groups["CUTTING"].id))
    s.flush()
    for i, a in enumerate(COLORS):
        for j, c in enumerate(COLORS):
            if a == c:
                continue
            m = 15 + 6 * (j - i) if j > i else 35 + 12 * (i - j)  # light → dark cheap, dark → light expensive
            b.add(SetupMatrixEntry(matrix_id=paint.id, from_value=a, to_value=c, minutes=m))
    for a, c, m in [("HYD", "VAL", 60), ("VAL", "HYD", 50), ("PMP", "HYD", 55), ("BRK", "FRM", 25), ("FRM", "BRK", 25)]:
        b.add(SetupMatrixEntry(matrix_id=cncm.id, from_value=a, to_value=c, minutes=m))
    for a, c, m in [("AISI304", "S355", 35), ("AL6082", "42CrMo4", 30)]:
        b.add(SetupMatrixEntry(matrix_id=cutm.id, from_value=a, to_value=c, minutes=m))
    b.add(SetupRule(code="CLEAN-AL-STEEL", description="Chip cleaning after aluminium before steel on CNC", resource_ids=[str(res[c].id) for c in ("CNC-01", "CNC-02", "CNC-03", "CNC-04")], when_prev={"grade": "AL6082"}, when_next={"grade": ["S355", "42CrMo4", "AISI304"]}, add_minutes=15))
    b.add(SequenceRule(code="NO-YELLOW-AFTER-BLACK", type="NOT_IMMEDIATELY_AFTER", description="RAL1023 cannot immediately follow RAL9005 on the paint line", resource_ids=[str(res["PAINT-01"].id)], prev_match={"color": "RAL9005"}, next_match={"color": "RAL1023"}))
    b.add(PlanningRule(code="R001", name="Hydraulic bodies on CNC-03", description="IF product_family = HYD AND resource_group = CNC THEN preferred_machine = CNC-03", condition={"all": [{"field": "order.family", "op": "eq", "value": "HYD"}, {"field": "op.resource_groups", "op": "contains", "value": "CNC"}]}, actions=[{"type": "PREFER_RESOURCE", "resource": "CNC-03"}], priority=10))
    b.add(PlanningRule(code="R002", name="High-priority customers", description="IF customer_priority >= 8 THEN priority_weight += 3", condition={"field": "order.customer_priority", "op": "gte", "value": 8}, actions=[{"type": "ADD_WEIGHT", "value": 3}], priority=20))
    b.add(PlanningRule(code="R003", name="Safety time for expedited orders", description="IF expedite THEN plan to finish 4 h before the due date", condition={"field": "order.expedite", "op": "eq", "value": True}, actions=[{"type": "ADD_SAFETY_TIME", "minutes": 240}], priority=30))
    b.add(PlanningRule(code="R004", name="Stainless steel not on laser", description="IF grade = AISI304 THEN never cut on CUT-02", condition={"all": [{"field": "order.attributes.grade", "op": "eq", "value": "AISI304"}, {"field": "resource.code", "op": "eq", "value": "CUT-02"}]}, actions=[{"type": "FORBID_RESOURCE", "resource": "CUT-02"}], priority=40))

    # optimisation profiles (presets are editable copies)
    for code, p in PRESETS.items():
        b.add(
            OptimizationProfile(
                code=code,
                name=p["label"],
                preset=code,
                objectives={"mode": p["mode"], "weights": p["weights"], "levels": p.get("levels", []), "preset": code},
                constraints={"allow_overtime": False, "materials": "HARD"},
                solver={"provider": "hybrid", "profile": "QUICK", "time_limit_s": 20, "dispatch_rules": ["HYBRID_APS"]},
                is_default=code == "BALANCED",
                description=f"Preset '{p['label']}'",
            )
        )

    # ------------------------------------------------------------------ units, families, items
    for code, name, dim, integer in [("pcs", "Pieces", "count", True), ("kg", "Kilograms", "mass", False), ("m", "Metres", "length", False), ("l", "Litres", "volume", False), ("set", "Sets", "count", True), ("box", "Boxes", "count", True)]:
        b.add(UnitOfMeasure(code=code, name=name, dimension=dim, integer=integer))
    fam = {}
    for code, name, color in FAMILIES:
        fam[code] = b.add(ProductFamily(code=code, name=name, color_hint=color))
    s.flush()

    items: dict[str, Item] = {}

    def item(code: str, name: str, **kw) -> Item:
        it = b.add(Item(code=code, name=name, **kw))
        items[code] = it
        return it

    # raw materials
    raw = []
    for g in GRADES:
        for dia in (40, 63, 80, 100, 125):
            code = f"BAR-{g}-D{dia}"
            it = item(code, f"Round bar {g} Ø{dia}", item_type="RAW", make_or_buy="BUY", uom="kg", quantity_type="DECIMAL", attributes={"grade": g, "diameter": dia}, purchase_lead_time_days=12, unit_cost=2.1 if g == "S355" else 3.4 if g == "42CrMo4" else 5.8 if g == "AISI304" else 4.2, safety_stock=200)
            raw.append(it)
    for g in GRADES:
        for th in (6, 10, 15, 20):
            item(f"SHEET-{g}-{th}", f"Plate {g} {th} mm", item_type="RAW", make_or_buy="BUY", uom="kg", quantity_type="DECIMAL", attributes={"grade": g, "thickness": th}, purchase_lead_time_days=10, unit_cost=1.9)
    comps = []
    for code, name, lt, cost in [
        ("SK-HYD-063", "Seal kit HYD Ø63", 15, 11.5), ("SK-HYD-080", "Seal kit HYD Ø80", 15, 13.2), ("SK-HYD-100", "Seal kit HYD Ø100", 15, 15.9), ("SK-VAL-01", "Valve seal set", 12, 6.1),
        ("BRG-6205", "Bearing 6205-2RS", 21, 4.3), ("BRG-6207", "Bearing 6207-2RS", 21, 6.2), ("BOLT-M10", "Bolt DIN933 M10x40 8.8", 5, 0.12), ("BOLT-M12", "Bolt DIN933 M12x50 8.8", 5, 0.18),
        ("NUT-M10", "Nut M10", 5, 0.04), ("NUT-M12", "Nut M12", 5, 0.05), ("ROD-CHR-40", "Chromed rod Ø40", 18, 22.0), ("ROD-CHR-50", "Chromed rod Ø50", 18, 28.0),
        ("SPOOL-01", "Valve spool", 25, 18.4), ("SOL-24V", "Solenoid 24 VDC", 28, 31.0), ("GSK-PMP", "Pump gasket", 10, 1.9), ("IMP-PMP", "Impeller cast", 30, 26.0),
    ]:
        comps.append(item(code, name, item_type="RAW", make_or_buy="BUY", uom="pcs", quantity_type="INTEGER", purchase_lead_time_days=lt, unit_cost=cost, safety_stock=20))
    for k in range(1, 61):
        comps.append(item(f"FST-{k:03d}", f"Fastener set #{k}", item_type="RAW", make_or_buy="BUY", uom="set", quantity_type="INTEGER", purchase_lead_time_days=7, unit_cost=0.8))
    paints = {c: item(f"PWD-{c}", f"Powder coating {c}", item_type="RAW", make_or_buy="BUY", uom="kg", quantity_type="DECIMAL", purchase_lead_time_days=8, unit_cost=9.5, attributes={"color": c}, safety_stock=50) for c in COLORS}
    boxes = []
    for code, name in [("BOX-S", "Carton box S"), ("BOX-M", "Carton box M"), ("BOX-L", "Carton box L"), ("PAL-EUR", "Euro pallet"), ("FOAM-01", "Foam insert"), ("WRAP-01", "Stretch film (m)"), ("LBL-01", "Label set"), ("CRATE-01", "Wooden crate"), ("BAG-VCI", "VCI bag"), ("TAPE-01", "Tape roll")]:
        boxes.append(item(code, name, item_type="PACKAGING", make_or_buy="BUY", uom="pcs", quantity_type="INTEGER", purchase_lead_time_days=4, unit_cost=1.1))
    s.flush()

    # semis: 4 machined bodies per family
    semis: dict[str, list[Item]] = {}
    for fcode, _n, _c in FAMILIES:
        semis[fcode] = []
        for k, dia in enumerate((63, 80, 100, 125)):
            grade = {"HYD": "42CrMo4", "VAL": "AISI304" if k % 2 else "S355", "BRK": "S355", "PMP": "AL6082" if k < 2 else "S355", "FRM": "S355"}[fcode]
            it = item(f"MB-{fcode}-{dia}", f"Machined body {fcode} Ø{dia}", item_type="SEMI_FINISHED", make_or_buy="MAKE", uom="pcs", quantity_type="INTEGER", family_id=fam[fcode].id, attributes={"grade": grade, "diameter": dia}, production_lead_time_days=4, safety_stock=0, min_lot=20, lot_multiple=5)
            semis[fcode].append(it)
    # finished products: 10 per family
    fgs: list[Item] = []
    for fcode, fname, _c in FAMILIES:
        for k in range(10):
            dia = (63, 80, 100, 125)[k % 4]
            color = COLORS[(k * 2 + len(fcode)) % len(COLORS)]
            code = f"{fcode}-{dia}{chr(65 + k)}-{color[3:]}"
            fgs.append(item(code, f"{fname[:-1] if fname.endswith('s') else fname} Ø{dia} {chr(65 + k)} {color}", item_type="FINISHED", make_or_buy="MAKE", uom="pcs", quantity_type="INTEGER", family_id=fam[fcode].id, attributes={"color": color, "diameter": dia, "grade": semis[fcode][k % 4].attributes["grade"]}, unit_price=round(rng.uniform(90, 850), 2), max_lot=500, safety_time_minutes=0))
    s.flush()

    # ------------------------------------------------------------------ BOMs
    for fcode, lst in semis.items():
        for it in lst:
            bom = b.add(Bom(item_id=it.id, version_code="1"))
            s.flush()
            dia = it.attributes["diameter"]
            bar = items[f"BAR-{it.attributes['grade']}-D{min((40, 63, 80, 100, 125), key=lambda d: abs(d - dia))}"]
            b.add(BomLine(bom_id=bom.id, component_id=bar.id, quantity_per=round(dia / 20.0, 2), scrap_pct=3, operation_seq=10))
    fam_comp = {"HYD": ["SK-HYD-063", "ROD-CHR-40", "BOLT-M12"], "VAL": ["SK-VAL-01", "SPOOL-01", "SOL-24V"], "BRK": ["BOLT-M10", "NUT-M10"], "PMP": ["BRG-6205", "GSK-PMP", "IMP-PMP"], "FRM": ["BOLT-M12", "NUT-M12"]}
    fg_semi: dict[uuid.UUID, Item] = {}
    for k, it in enumerate(fgs):
        fcode = it.code.split("-")[0]
        semi = semis[fcode][k % 4]
        fg_semi[it.id] = semi
        bom = b.add(Bom(item_id=it.id, version_code="1"))
        s.flush()
        b.add(BomLine(bom_id=bom.id, component_id=semi.id, quantity_per=1, operation_seq=10))
        for c in fam_comp[fcode]:
            code = c
            if c == "SK-HYD-063":
                code = {63: "SK-HYD-063", 80: "SK-HYD-080"}.get(it.attributes["diameter"], "SK-HYD-100")
            if c == "ROD-CHR-40" and it.attributes["diameter"] >= 100:
                code = "ROD-CHR-50"
            qty = 4 if c.startswith(("BOLT", "NUT")) else 1
            b.add(BomLine(bom_id=bom.id, component_id=items[code].id, quantity_per=qty, operation_seq=10))
        b.add(BomLine(bom_id=bom.id, component_id=items[f"FST-{(k % 60) + 1:03d}"].id, quantity_per=1, operation_seq=10))
        b.add(BomLine(bom_id=bom.id, component_id=paints[it.attributes["color"]].id, quantity_per=round(0.05 + it.attributes["diameter"] / 1000.0, 3), operation_seq=20))
        b.add(BomLine(bom_id=bom.id, component_id=items["BOX-M" if it.attributes["diameter"] < 100 else "BOX-L"].id, quantity_per=1, operation_seq=30))
    s.flush()

    # ------------------------------------------------------------------ routings
    rops_by_item: dict[uuid.UUID, list[RoutingOperation]] = {}
    subcontractor = b.add(Supplier(code="SUP-TTS", name="Tratamientos Térmicos del Sur", lead_time_days=3, reliability_pct=92, is_subcontractor=True))
    s.flush()
    cnc_primary = {"HYD": "CNC-03", "VAL": "CNC-04", "BRK": "CNC-01", "PMP": "CNC-02", "FRM": "CNC-01"}
    for fcode, lst in semis.items():
        for it in lst:
            r = b.add(Routing(item_id=it.id, plant_id=plant.id))
            s.flush()
            dia = it.attributes["diameter"]
            cut = b.add(RoutingOperation(routing_id=r.id, seq=10, code="CUT", name="Saw / cut blank", setup_minutes=10, run_minutes_per_unit=round(0.6 + dia / 150.0, 2), labor_pool_id=pools["POOL-CUT"].id, move_minutes=30, setup_attributes={}))
            cnc = b.add(
                RoutingOperation(
                    routing_id=r.id, seq=20, code="CNC", name="CNC machining", setup_minutes=30, run_minutes_per_unit=round(2.2 + dia / 40.0, 2),
                    run_tiers=[{"min_quantity": 1000, "minutes_per_unit": round((2.2 + dia / 40.0) * 0.92, 2)}],
                    labor_pool_id=pools["POOL-CNC"].id, tool_id=tools["T-18"].id if fcode == "HYD" else None, move_minutes=30, transfer_batch=None,
                    instructions="Clamp on fixture, run program P-%s-%d, first-article inspection." % (fcode, dia),
                )
            )
            s.flush()
            b.add(OperationResource(routing_operation_id=cut.id, resource_id=res["CUT-01"].id, role="PRIMARY", preference=0))
            b.add(OperationResource(routing_operation_id=cut.id, resource_id=res["CUT-02"].id, role="ALTERNATIVE", preference=1, speed_factor=1.3))
            prim = cnc_primary[fcode]
            b.add(OperationResource(routing_operation_id=cnc.id, resource_id=res[prim].id, role="PRIMARY", preference=0))
            alts = [c for c in ("CNC-01", "CNC-02", "CNC-03", "CNC-04") if c != prim]
            if fcode in ("VAL",):
                alts = [c for c in alts if c in ("CNC-03",)]  # 5-axis only
            for p, a in enumerate(alts, 1):
                b.add(OperationResource(routing_operation_id=cnc.id, resource_id=res[a].id, role="ALTERNATIVE", preference=p, speed_factor=0.9 if a in ("CNC-01", "CNC-02") else 1.0))
            ops = [cut, cnc]
            if fcode in ("HYD", "PMP"):
                ht = b.add(RoutingOperation(routing_id=r.id, seq=30, code="HT", name="Heat treatment (Q&T)", setup_minutes=0, batch_size=50, minutes_per_batch=240, interruptible=False, wait_minutes=60))
                s.flush()
                b.add(OperationResource(routing_operation_id=ht.id, resource_id=res["HT-01"].id, role="PRIMARY", preference=0))
                b.add(OperationResource(routing_operation_id=ht.id, resource_id=sub_ht.id, role="SUBCONTRACT", preference=2, supplier_id=subcontractor.id, subcontract_lead_time_minutes=3 * 1440, subcontract_cost=120))
                ops.append(ht)
            rops_by_item[it.id] = ops
    for it in fgs:
        r = b.add(Routing(item_id=it.id, plant_id=plant.id))
        s.flush()
        dia = it.attributes["diameter"]
        asm = b.add(RoutingOperation(routing_id=r.id, seq=10, code="ASM", name="Assembly & test", setup_minutes=15, run_minutes_per_unit=round(1.4 + dia / 90.0, 2), labor_pool_id=pools["POOL-ASM"].id, move_minutes=20))
        pnt = b.add(RoutingOperation(routing_id=r.id, seq=20, code="PAINT", name="Powder coating", setup_minutes=10, run_minutes_per_unit=round(0.45 + dia / 400.0, 2), labor_pool_id=pools["POOL-PNT"].id, wait_minutes=120, setup_attributes={"color": it.attributes["color"]}))
        pck = b.add(RoutingOperation(routing_id=r.id, seq=30, code="PACK", name="Pack & label", setup_minutes=5, run_minutes_per_unit=0.35, labor_pool_id=pools["POOL-PCK"].id))
        s.flush()
        b.add(OperationResource(routing_operation_id=asm.id, group_id=groups["ASSEMBLY"].id, role="PRIMARY", preference=0))
        b.add(OperationResource(routing_operation_id=pnt.id, resource_id=res["PAINT-01"].id, role="PRIMARY", preference=0))
        b.add(OperationResource(routing_operation_id=pck.id, group_id=groups["PACKING"].id, role="PRIMARY", preference=0))
        rops_by_item[it.id] = [asm, pnt, pck]
    s.flush()

    # ------------------------------------------------------------------ customers, suppliers
    customers = []
    for k, (name, prio, strat) in enumerate(
        [("Iberhidráulica SA", 9, True), ("Nordic Lift Systems", 7, False), ("AgroMaq Andalucía", 6, False), ("Atlas Construcción", 8, True), ("Portuaria del Sur", 5, False), ("TransEuro Trucks", 7, False), ("Minera Riotinto Serv.", 6, False),
         ("Ferroviaria Levante", 8, True), ("Hidro Energía Norte", 5, False), ("Aeroparts Getafe", 9, True), ("Metalúrgica Tajo", 4, False), ("Grúas del Atlántico", 6, False), ("EcoWind Components", 7, False), ("Balear Náutica", 3, False),
         ("Lusitania Industrial", 5, False), ("Alpha Packaging", 4, False), ("Maquinaria Ebro", 6, False), ("Fluidos Castilla", 5, False), ("Rhône Hydraulique", 7, False), ("Dutch Dredging BV", 6, False), ("Pirineos Forestal", 3, False),
         ("Cantabria Offshore", 6, False), ("Manchega Agrícola", 4, False), ("Canarias Logística", 3, False), ("Galicia Naval", 7, False)]
    ):
        customers.append(b.add(Customer(code=f"C{k + 1:03d}", name=name, priority=prio, is_strategic=strat, country="ES" if k % 5 else "PT")))
    suppliers = {}
    for code, name, lt, rel in [("SUP-ACE", "Aceros del Guadalquivir", 12, 95), ("SUP-SEL", "Sellos Técnicos SL", 15, 88), ("SUP-ROD", "Rodamientos Iberia", 21, 90), ("SUP-FIX", "Tornillería Sevilla", 5, 98), ("SUP-PNT", "Pinturas Polvo Sur", 8, 97), ("SUP-PCK", "Embalajes Aljarafe", 4, 99), ("SUP-HID", "HydroComp GmbH", 25, 85), ("SUP-CST", "Fundiciones Norte", 30, 82)]:
        suppliers[code] = b.add(Supplier(code=code, name=name, lead_time_days=lt, reliability_pct=rel))
    s.flush()

    t_now = _local(today, time(6))
    # ------------------------------------------------------------------ sales orders + production orders
    so_n, wo_n = 23000, 10000
    open_fg_orders: list[ProductionOrder] = []
    all_orders: list[tuple[ProductionOrder, Item]] = []
    so_lines: list[SalesOrderLine] = []
    work_days = [today + timedelta(days=d) for d in range(0, 50) if (today + timedelta(days=d)).weekday() < 5]
    # open FG orders
    for k in range(170):
        it = fgs[rng.randrange(len(fgs))]
        cust = rng.choice(customers)
        so_n += 1
        so = b.add(SalesOrder(number=f"SO-{so_n}", customer_id=cust.id, order_date=today - timedelta(days=rng.randint(5, 30))))
        s.flush()
        r = rng.random()
        if r < 0.04:
            due_day = today - timedelta(days=rng.randint(1, 3))  # past due
        elif r < 0.18:
            due_day = work_days[rng.randint(3, 7)]
        elif r < 0.50:
            due_day = work_days[rng.randint(7, 16)]
        else:
            due_day = work_days[rng.randint(15, 29)]
        due = _local(due_day, time(rng.choice([14, 18, 22])))
        qty = rng.choice([20, 25, 30, 40, 50, 60, 75, 80, 100])
        expedite = rng.random() < 0.05
        line = b.add(SalesOrderLine(sales_order_id=so.id, line_no=10, item_id=it.id, plant_id=plant.id, quantity=qty, requested_date=due - timedelta(days=rng.choice([0, 0, 2])), promised_date=due, due_date=due, priority=cust.priority, unit_price=it.unit_price))
        s.flush()
        so_lines.append(line)
        wo_n += 1
        status = "RELEASED" if due_day <= today + timedelta(days=5) else rng.choice(["PLANNED", "FIRMED", "FIRMED"])
        po_ = b.add(
            ProductionOrder(
                number=f"WO-{wo_n}", plant_id=plant.id, item_id=it.id, quantity=qty, status=status, due_date=due, requested_date=line.requested_date, promised_date=due,
                release_date=t_now - timedelta(days=2) if status == "RELEASED" else t_now + timedelta(days=max(0, (due_day - today).days - 10)),
                priority=min(10, max(1, cust.priority + rng.randint(-2, 2))), expedite=expedite, customer_id=cust.id, sales_order_line_id=line.id, source="ERP",
            )
        )
        open_fg_orders.append(po_)
        all_orders.append((po_, it))
    # a few make-to-stock FG orders
    for k in range(12):
        it = fgs[rng.randrange(len(fgs))]
        wo_n += 1
        due = _local(work_days[rng.randint(10, 30)], time(22))
        po_ = b.add(ProductionOrder(number=f"WO-{wo_n}", plant_id=plant.id, item_id=it.id, quantity=rng.choice([100, 150, 200]), status="PLANNED", due_date=due, priority=3, source="MRP"))
        open_fg_orders.append(po_)
        all_orders.append((po_, it))
    s.flush()
    # semi orders to feed FG orders (grouped by semi item, lot-sized), due 3 working days before the first consumer
    # sub-assemblies already in stock cover most finished-goods orders due in the next days
    semi_stock: dict[uuid.UUID, float] = {}
    for lst in semis.values():
        for it in lst:
            near = sum(o.quantity for o in open_fg_orders if fg_semi[o.item_id].id == it.id and o.due_date <= t_now + timedelta(days=6))
            semi_stock[it.id] = float(int(near * rng.uniform(0.75, 1.0)) + rng.choice([0, 0, 10, 20]))
    need_by_semi: dict[uuid.UUID, list[ProductionOrder]] = {}
    for o in open_fg_orders:
        need_by_semi.setdefault(fg_semi[o.item_id].id, []).append(o)
    semi_by_id = {it.id: it for lst in semis.values() for it in lst}
    for sid, consumers in need_by_semi.items():
        consumers.sort(key=lambda o: o.due_date)
        stock = semi_stock.get(sid, 0)
        bucket: list[ProductionOrder] = []
        bucket_qty = 0.0
        covered = float(stock or 0)
        for o in consumers:
            if covered >= o.quantity:
                covered -= o.quantity
                continue
            need = o.quantity - covered
            covered = 0
            bucket.append(o)
            bucket_qty += need
            if bucket_qty >= 90 or o is consumers[-1]:
                wo_n += 1
                first_due = bucket[0].due_date
                qty = max(20, int(5 * round(bucket_qty / 5 + 0.49)))
                due = first_due - timedelta(days=3)
                semi = semi_by_id[sid]
                released = due <= t_now + timedelta(days=7)
                po_ = b.add(ProductionOrder(number=f"WO-{wo_n}", plant_id=plant.id, item_id=sid, quantity=qty, status="RELEASED" if released else "FIRMED", due_date=due, release_date=t_now - timedelta(days=1) if released else due - timedelta(days=9), priority=max(p.priority for p in bucket), source="MRP", parent_order_id=bucket[0].id))
                all_orders.append((po_, semi))
                bucket, bucket_qty = [], 0.0
    s.flush()

    # operations for open orders; a couple already in progress this morning
    in_progress_budget = {"CUT-01": 1, "CNC-02": 1, "ASM-01": 1}
    op_count = 0
    for o, it in all_orders:
        for ro in rops_by_item[it.id]:
            status = "PLANNED" if o.status in ("PLANNED", "FIRMED") else "RELEASED"
            actual_start = actual_res = None
            done = 0.0
            if ro.seq == 10 and o.status == "RELEASED" and o.due_date < t_now + timedelta(days=3):
                primary = next((orr.resource_id for orr in s.scalars(select(OperationResource).where(OperationResource.routing_operation_id == ro.id, OperationResource.role == "PRIMARY")) if orr.resource_id), None)
                code = next((c for c, r in res.items() if r.id == primary), None)
                if code in in_progress_budget and in_progress_budget[code] > 0:
                    in_progress_budget[code] -= 1
                    status = "IN_PROGRESS"
                    actual_start = t_now - timedelta(minutes=0)
                    actual_res = primary
                    done = round(o.quantity * 0.1)
                    o.status = "IN_PRODUCTION"
            b.add(ProductionOrderOperation(order_id=o.id, routing_operation_id=ro.id, seq=ro.seq, code=ro.code, name=ro.name, status=status, actual_start=actual_start, actual_resource_id=actual_res, completed_quantity=done))
            op_count += 1
        o.routing_id = s.scalar(select(Routing.id).where(Routing.item_id == it.id))
        o.bom_id = s.scalar(select(Bom.id).where(Bom.item_id == it.id))
    s.flush()

    # ------------------------------------------------------------------ inventory and purchase orders sized to demand
    bom_lines_by_item: dict[uuid.UUID, list[BomLine]] = {}
    for bom in s.scalars(select(Bom)):
        bom_lines_by_item[bom.item_id] = list(s.scalars(select(BomLine).where(BomLine.bom_id == bom.id)))
    demand: dict[uuid.UUID, float] = {}
    for o, it in all_orders:
        for ln in bom_lines_by_item.get(it.id, []):
            demand[ln.component_id] = demand.get(ln.component_id, 0.0) + float(o.quantity) * float(ln.quantity_per) / (1 - (ln.scrap_pct or 0) / 100.0)
    code_of = {v.id: k for k, v in items.items()}
    # deliberate stories: (stock share, PO share, PO day offset)
    stories = {
        "BRG-6205": (0.15, 0.35, 10),  # shortage: not enough supply in the horizon
        "IMP-PMP": (0.30, 0.90, 16),  # long lead time casting: late supply
        "SK-HYD-080": (0.10, 1.10, 8),  # supplier delay (was day 3)
        "BAR-42CrMo4-D80": (0.25, 1.10, 6),
        "BAR-42CrMo4-D100": (0.25, 1.10, 6),
        "BAR-42CrMo4-D125": (0.30, 1.10, 6),
        "SOL-24V": (0.30, 1.00, 12),
    }
    sup_for = lambda it: "SUP-ACE" if it.code.startswith(("BAR", "SHEET")) else "SUP-SEL" if it.code.startswith("SK") else "SUP-ROD" if it.code.startswith("BRG") else "SUP-PNT" if it.code.startswith("PWD") else "SUP-PCK" if it.item_type == "PACKAGING" else "SUP-HID" if it.code.startswith(("SOL", "SPOOL", "ROD")) else "SUP-CST" if it.code.startswith("IMP") else "SUP-FIX"
    po_n = 4500
    for it in list(items.values()):
        if it.item_type not in ("RAW", "PACKAGING"):
            continue
        need = demand.get(it.id, 0.0)
        integer = it.quantity_type == "INTEGER"
        rnd = (lambda q: float(int(q + 0.999))) if integer else (lambda q: round(q, 1))
        if it.code in stories:
            stock_share, po_share, day = stories[it.code]
        else:
            stock_share, po_share, day = rng.uniform(0.7, 1.2), rng.uniform(0.4, 0.8), rng.randint(2, 14)
        stock = rnd(max(need * stock_share, 0 if need else rng.uniform(20, 200)))
        if it.code == "BAR-42CrMo4-D63":
            b.add(Inventory(item_id=it.id, plant_id=plant.id, on_hand=stock + 300, quality_hold=300))
        else:
            b.add(Inventory(item_id=it.id, plant_id=plant.id, on_hand=stock))
        if need > 0 and po_share > 0:
            po_n += 1
            sup = suppliers[sup_for(it)]
            po = b.add(PurchaseOrder(number=f"PO-{po_n}", supplier_id=sup.id, plant_id=plant.id))
            s.flush()
            exp = t_now + timedelta(days=day, hours=rng.randint(0, 8))
            original = t_now + timedelta(days=3) if it.code == "SK-HYD-080" else exp
            b.add(PurchaseOrderLine(purchase_order_id=po.id, line_no=10, item_id=it.id, quantity=rnd(need * po_share + 1), expected_date=exp, original_date=original, confirmed=rng.random() > 0.1 or it.code in stories))
    # an overdue receipt (data quality: planning assumes it arrives now)
    po_n += 1
    po = b.add(PurchaseOrder(number=f"PO-{po_n}", supplier_id=suppliers["SUP-FIX"].id, plant_id=plant.id))
    s.flush()
    b.add(PurchaseOrderLine(purchase_order_id=po.id, line_no=10, item_id=items["BOLT-M12"].id, quantity=5000, expected_date=t_now - timedelta(days=2), confirmed=True))
    # semi-finished stock (covers part of the finished-goods demand)
    for lst in semis.values():
        for it in lst:
            b.add(Inventory(item_id=it.id, plant_id=plant.id, on_hand=semi_stock.get(it.id, 0)))
    _ = code_of

    # ------------------------------------------------------------------ history: completed orders with MES actuals
    hist = 0
    hist_ops = 0
    start_hist = today - timedelta(days=60)
    for k in range(650):
        it = rng.choice(fgs + [x for lst in semis.values() for x in lst])
        wo_n += 1
        day = start_hist + timedelta(days=rng.randint(0, 57))
        while day.weekday() >= 5:
            day += timedelta(days=1)
        qty = rng.choice([20, 30, 40, 50, 60, 80, 100])
        due = _local(day + timedelta(days=rng.randint(1, 4)), time(22))
        o = b.add(ProductionOrder(number=f"WO-{wo_n}", plant_id=plant.id, item_id=it.id, quantity=qty, completed_quantity=qty, status="COMPLETED", due_date=due, priority=rng.randint(2, 9), customer_id=rng.choice(customers).id if it.item_type == "FINISHED" else None, source="ERP"))
        s.flush()
        cursor = _local(day, time(rng.choice([6, 8, 10, 14, 16])))
        for ro in rops_by_item[it.id]:
            planned = (ro.setup_minutes or 0) + (ro.run_minutes_per_unit or 0) * qty + (ro.minutes_per_batch or 0) * (-(-qty // (ro.batch_size or 1e9)) if ro.batch_size else 0)
            actual_min = planned * rng.lognormvariate(0.04, 0.12)
            start = cursor + timedelta(minutes=rng.randint(0, 240))
            end = start + timedelta(minutes=actual_min)
            primary = next((orr.resource_id for orr in s.scalars(select(OperationResource).where(OperationResource.routing_operation_id == ro.id)) if orr.resource_id), None)
            if primary is None:
                grp = next((orr.group_id for orr in s.scalars(select(OperationResource).where(OperationResource.routing_operation_id == ro.id)) if orr.group_id), None)
                mem = [m.resource_id for m in s.scalars(select(ResourceGroupMember).where(ResourceGroupMember.group_id == grp))] if grp else []
                primary = rng.choice(mem) if mem else None
            poo = b.add(ProductionOrderOperation(order_id=o.id, routing_operation_id=ro.id, seq=ro.seq, code=ro.code, name=ro.name, status="COMPLETED", completed_quantity=qty, actual_start=start, actual_end=end, actual_resource_id=primary))
            s.flush()
            b.add(ActualProduction(order_operation_id=poo.id, resource_id=primary, start=start, end=end, good_quantity=qty - (1 if rng.random() < 0.1 else 0), scrap_quantity=1 if rng.random() < 0.1 else 0, source="MES"))
            cursor = end + timedelta(minutes=rng.randint(30, 600))
            hist_ops += 1
        o.routing_id = s.scalar(select(Routing.id).where(Routing.item_id == it.id))
        hist += 1
    s.flush()

    # ------------------------------------------------------------------ forecasts / demand (MPS)
    monday = today - timedelta(days=today.weekday())
    for it in fgs:
        base = rng.uniform(20, 90)
        for w in range(26):
            season = 1.0 + 0.25 * (1 if (w // 4) % 3 == 0 else 0)
            b.add(Demand(item_id=it.id, plant_id=plant.id, period_start=monday + timedelta(weeks=w), quantity=round(base * season * rng.uniform(0.7, 1.3)), demand_type="FORECAST", source="Sales plan"))
        if rng.random() < 0.15:
            b.add(Demand(item_id=it.id, plant_id=plant.id, period_start=monday + timedelta(weeks=rng.randint(3, 8)), quantity=rng.randint(80, 200), demand_type="PROMOTIONAL", source="Campaign"))

    # ------------------------------------------------------------------ scenarios
    live = b.add(Scenario(plant_id=plant.id, name="Live plan", is_live=True, kind="LIVE", owner="planner", config={"horizon_days": 42, "frozen_hours": 24, "flexible_days": 7, "profile_code": "BALANCED"}, description="Operational plan of Sevilla Plant"))
    s.flush()
    plant.live_scenario_id = live.id
    b.add(Scenario(plant_id=plant_mx.id, name="Live plan", is_live=True, kind="LIVE", owner="planner", config={"horizon_days": 28, "frozen_hours": 24, "profile_code": "BALANCED"}))
    night = b.add(Scenario(plant_id=plant.id, name="What-if: CNC night shift", kind="WHAT_IF", owner="planner", config={"horizon_days": 42, "frozen_hours": 24, "profile_code": "BALANCED"}, description="Add a night shift (22:00–06:00, Mon–Thu) on the four CNC machines"))
    s.flush()
    b.add(ScenarioChange(scenario_id=night.id, seq=1, type="ADD_SHIFT", payload={"resource_ids": [str(res[c].id) for c in ("CNC-01", "CNC-02", "CNC-03", "CNC-04")], "label": "night", "shifts": [{"weekday": d, "start": "22:00", "end": "06:00", "kind": "REGULAR"} for d in range(4)]}, description="+ Night shift on CNC-01..04"))
    buy = b.add(Scenario(plant_id=plant.id, name="What-if: buy CNC-08", kind="WHAT_IF", owner="planner", config={"horizon_days": 42, "frozen_hours": 24, "profile_code": "BALANCED"}, description="A fifth 5-axis machining centre available from next Monday"))
    s.flush()
    next_monday = today + timedelta(days=(7 - today.weekday()) % 7 or 7)
    b.add(ScenarioChange(scenario_id=buy.id, seq=1, type="ADD_RESOURCE", payload={"clone_of": str(res["CNC-03"].id), "id": str(uuid.uuid5(uuid.NAMESPACE_URL, "monxu-cnc-08")), "code": "CNC-08", "name": "CNC machining centre #8 (5-axis, new)", "available_from": _local(next_monday, time(6)).isoformat()}, description="+ CNC-08 (clone of CNC-03)"))

    # integration connectors (framework; credentials encrypted)
    b.add(Integration(code="ERP-SAP", name="SAP S/4HANA (orders, BOM, routings)", system="SAP", direction="BOTH", settings={"base_url": "https://erp.example.invalid/sap/opu/odata", "entities": ["orders", "boms", "routings", "inventory"], "mode": "REST"}, secret_encrypted=encrypt_secret("demo-not-a-real-secret"), is_active=False))
    b.add(Integration(code="MES-SHOP", name="Shop-floor MES (operations, downtime)", system="MES", direction="IN", settings={"events": ["OperationStarted", "OperationFinished", "MachineDown", "MachineAvailable"], "mode": "WEBHOOK"}, is_active=True))

    # ------------------------------------------------------------------ users
    users = []
    for username, name, roles in [
        ("admin", "Admin Monxu", ["COMPANY_ADMIN"]),
        ("planner", "Planner Sevilla", ["PLANNER"]),
        ("manager", "Production Manager", ["PRODUCTION_MANAGER"]),
        ("supervisor", "Shift Supervisor", ["SUPERVISOR"]),
        ("operator", "CNC Operator", ["OPERATOR"]),
        ("viewer", "Viewer", ["VIEWER"]),
    ]:
        users.append(create_user(s, b.tid, username, f"{username}@monxu.example", name, DEMO_PASSWORD, roles, check_password=False))
    for u in users:
        u.default_plant_id = plant.id
    s.flush()
    return {
        "status": "created",
        "plant_id": str(plant.id),
        "live_scenario_id": str(live.id),
        "counts": {
            "products_finished": len(fgs),
            "sub_assemblies": sum(len(v) for v in semis.values()),
            "materials_total": len(items),
            "resources": s.query(Resource).count(),
            "operators": operator_count,
            "production_orders_open": len(all_orders),
            "production_orders_history": hist,
            "open_operations": op_count,
            "history_operations": hist_ops,
            "sales_order_lines": len(so_lines),
        },
        "users": [u.username for u in users],
        "password": DEMO_PASSWORD,
    }


def main() -> None:  # pragma: no cover
    import argparse
    import json

    from ..core.db import create_all

    ap = argparse.ArgumentParser(description="Create the MonxuPlan demo factory")
    ap.add_argument("--reset", action="store_true")
    args = ap.parse_args()
    create_all()
    print(json.dumps(seed_demo(reset=args.reset), indent=2))


if __name__ == "__main__":  # pragma: no cover
    main()
