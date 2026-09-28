"""Planning assistant — answers questions about the current plan from real data only.

Two modes, always labelled in the response:

* ``GROUNDED`` (default, no external service): intent detection + templated answers built from the
  stored plan (KPIs, root-cause chains, bottlenecks, pegging, capacity). Every fact carries its
  source so the UI can link to it.
* ``LLM`` (optional, ``MONXU_ASSISTANT_LLM=1`` and Anthropic credentials): Claude receives the question
  and *read-only tools* over the same functions; it composes the answer from tool results. It has no
  tool that changes data — the assistant never decides or edits the plan. It may *suggest* a what-if
  the planner can run explicitly. If the call fails, the grounded answer is returned with a note.
"""

from __future__ import annotations

import json
import logging
import os
import re
import uuid
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..core.config import get_settings
from ..core.errors import NotFound, ValidationFailed
from ..models import ConstraintViolation, Item, Plan, Plant, ProductionOrder, Resource, Scenario
from . import plan_store
from .context import Ctx
from .views import _get_plan

log = logging.getLogger("monxuplan.assistant")

DEFAULT_MODEL = "claude-opus-5"
MAX_TOOL_ROUNDS = 6


# =============================================================================================
# plan resolution & small helpers
# =============================================================================================


def _resolve_plan(s: Session, ctx: Ctx, plant_id: uuid.UUID | None, plan_id: uuid.UUID | None) -> Plan:
    if plan_id:
        return _get_plan(s, ctx, plan_id)
    plant = s.get(Plant, plant_id) if plant_id else None
    if plant is None:
        raise ValidationFailed("Select a plant or a plan", code="NO_PLAN")
    sc = s.get(Scenario, plant.live_scenario_id) if plant.live_scenario_id else None
    if sc is None or sc.head_plan_id is None:
        raise NotFound("There is no plan for this plant yet — run the planner first.", code="NO_PLAN")
    return _get_plan(s, ctx, sc.head_plan_id)


def _lang(question: str, ctx: Ctx) -> str:
    q = question.lower()
    if re.search(r"[¿¡ñáéíóú]|\b(por qué|porque|qué|cuál|cuales|cuáles|orden|retras|cuello|máquina|maquina|pedido|material|falta|cómo|como va)\b", q):
        return "es"
    return "es" if ctx.locale == "es" and not re.search(r"\b(why|what|which|how|late|order)\b", q) else "en"


T = {
    "no_match": {
        "en": "I can answer questions about the current plan from its data. Try for example:\n- *Why is order {order} late?*\n- *What is the bottleneck?*\n- *Which orders are late?*\n- *Which orders are affected by {material}?*\n- *How loaded is {resource}?*\n- *How is the plan?*",
        "es": "Respondo preguntas sobre el plan actual a partir de sus datos. Por ejemplo:\n- *¿Por qué va tarde la orden {order}?*\n- *¿Cuál es el cuello de botella?*\n- *¿Qué órdenes van tarde?*\n- *¿Qué órdenes dependen de {material}?*\n- *¿Qué carga tiene {resource}?*\n- *¿Cómo está el plan?*",
    },
    "order_on_time": {"en": "**{number}** is planned to finish on {end}, before its due date {due} (on time).", "es": "**{number}** termina el {end}, antes de su fecha de entrega {due} (a tiempo)."},
    "order_late": {"en": "**{number}** is planned to finish on {end}, **{late} after** its due date {due}.", "es": "**{number}** termina el {end}, **{late} después** de su fecha de entrega {due}."},
    "order_unscheduled": {"en": "**{number}** could not be scheduled in this plan.", "es": "**{number}** no se ha podido planificar en este plan."},
    "chain": {"en": "Reason chain (from the plan's constraint analysis):", "es": "Cadena de causas (análisis de restricciones del plan):"},
    "infinite": {"en": "With unlimited capacity it could finish on {eft} — the rest of the delay comes from capacity and sequencing.", "es": "Con capacidad ilimitada podría terminar el {eft}; el resto del retraso viene de capacidad y secuencia."},
    "deadline_impossible": {"en": "Even with unlimited capacity it cannot meet the due date (lead time/material bound {eft}).", "es": "Ni con capacidad ilimitada llega a la fecha (límite por plazo o material: {eft})."},
    "extra_capacity": {"en": "Estimated additional capacity needed on the limiting resources: {h} h.", "es": "Capacidad adicional estimada en los recursos limitantes: {h} h."},
    "bottleneck_head": {"en": "Bottlenecks of plan {plan} (ranked by overload, induced waiting, orders affected, utilisation):", "es": "Cuellos de botella del plan {plan} (por sobrecarga, espera inducida, órdenes afectadas y utilización):"},
    "no_bottleneck": {"en": "No resource is overloaded or causing significant waiting in plan {plan}.", "es": "Ningún recurso está sobrecargado ni provoca esperas significativas en el plan {plan}."},
    "late_head": {"en": "{n} orders are late or unscheduled in plan {plan}. Largest delays:", "es": "{n} órdenes van tarde o sin planificar en el plan {plan}. Mayores retrasos:"},
    "no_late": {"en": "All orders of plan {plan} are planned on time.", "es": "Todas las órdenes del plan {plan} están planificadas a tiempo."},
    "causes": {"en": "Causes: {causes}.", "es": "Causas: {causes}."},
    "shortage_head": {"en": "Material shortages in plan {plan} (demand that no stock or receipt covers):", "es": "Faltas de material en el plan {plan} (demanda sin stock ni recepción que la cubra):"},
    "no_shortage": {"en": "No material shortage in plan {plan}.", "es": "No hay faltas de material en el plan {plan}."},
    "late_supply": {"en": "Operations waiting for late supplies:", "es": "Operaciones esperando suministros tardíos:"},
    "impact": {"en": "**{material}** feeds {n} orders ({c} customers){rev}:", "es": "**{material}** alimenta {n} órdenes ({c} clientes){rev}:"},
    "impact_none": {"en": "No order of plan {plan} consumes **{material}**.", "es": "Ninguna orden del plan {plan} consume **{material}**."},
    "resource": {"en": "**{code}** ({name}) in plan {plan}: {sched} h scheduled of {cap} h available ({util}).", "es": "**{code}** ({name}) en el plan {plan}: {sched} h planificadas de {cap} h disponibles ({util})."},
    "resource_bn": {"en": "It is ranked #{rank} bottleneck ({kind}).", "es": "Está en el puesto #{rank} de cuellos de botella ({kind})."},
    "whatif": {"en": "I don't change the plan. You can evaluate this as a what-if scenario: **{label}** (creates a copy of the live plan and replans it; the live plan is not touched).", "es": "No modifico el plan. Puedes evaluarlo como escenario what-if: **{label}** (crea una copia del plan vivo y la replanifica; el plan vivo no cambia)."},
    "summary": {"en": "Plan **{plan}** ({status}, {feasible}): OTIF {otif}, {late} late orders, {uns} unscheduled, utilisation {util}, setup {setup} h. Solver: {provider} — {sstatus}.", "es": "Plan **{plan}** ({status}, {feasible}): OTIF {otif}, {late} órdenes tarde, {uns} sin planificar, utilización {util}, cambios {setup} h. Solver: {provider} — {sstatus}."},
    "feasible": {"en": "feasible", "es": "factible"},
    "infeasible": {"en": "with hard violations", "es": "con violaciones duras"},
    "incomplete": {"en": "incomplete: {n} operations could not be scheduled", "es": "incompleto: {n} operaciones sin planificar"},
    "op_head": {"en": "Operation **{op}**:", "es": "Operación **{op}**:"},
}


def _t(key: str, lang: str, **kw: Any) -> str:
    return T[key][lang].format(**kw)


def _fmt_dt(v: str | None, tz: str) -> str:
    if not v:
        return "—"
    from datetime import datetime
    from zoneinfo import ZoneInfo

    dt = datetime.fromisoformat(v.replace("Z", "+00:00"))
    return dt.astimezone(ZoneInfo(tz)).strftime("%a %d %b %H:%M")


def _fmt_minutes(m: int | float | None) -> str:
    m = int(m or 0)
    d, rem = divmod(m, 1440)
    h, mi = divmod(rem, 60)
    parts = ([f"{d} d"] if d else []) + ([f"{h} h"] if h else []) + ([f"{mi} min"] if mi and not d else [])
    return " ".join(parts) or "0 min"


# =============================================================================================
# read tools (shared by grounded mode and LLM mode)
# =============================================================================================


class Tools:
    """Read-only accessors over one plan. Each returns JSON-serialisable data with sources."""

    def __init__(self, s: Session, ctx: Ctx, plan: Plan):
        self.s, self.ctx, self.plan = s, ctx, plan
        plant = s.get(Plant, plan.plant_id)
        self.tz = plant.timezone if plant else "UTC"
        self.sources: list[dict[str, Any]] = []

    def _src(self, kind: str, ref: str, label: str) -> None:
        if not any(x["kind"] == kind and x["ref"] == ref for x in self.sources):
            self.sources.append({"kind": kind, "ref": ref, "label": label})

    def plan_overview(self) -> dict[str, Any]:
        p = self.plan
        k = p.kpis or {}
        md = p.solver_metadata or {}
        self._src("plan", str(p.id), p.number)
        return {
            "plan": p.number,
            "status": p.status,
            "feasible": p.feasible,
            "kpis": {x: k.get(x) for x in ("otif", "on_time_delivery", "late_orders", "orders_unscheduled", "average_delay_h", "maximum_delay_h", "utilization", "setup_h", "overtime_h", "material_shortages", "constraint_violations_hard", "throughput_units")},
            "solver": {"provider": md.get("provider"), "status": md.get("status"), "gap": md.get("gap"), "runtime_s": md.get("runtime_s")},
            "late_causes": (p.kpi_details or {}).get("late_causes", {}),
            "unscheduled_operations": plan_store.unscheduled_count(self.s, p),
            "hard_violations_placed": self.s.scalar(
                select(func.count()).select_from(ConstraintViolation).where(ConstraintViolation.plan_id == p.id, ConstraintViolation.hardness == "HARD", ConstraintViolation.type != "UNSCHEDULED")
            )
            or 0,
        }

    def find_order(self, number: str) -> ProductionOrder | None:
        o = self.s.scalar(select(ProductionOrder).where(ProductionOrder.number == number, ProductionOrder.plant_id == self.plan.plant_id))
        if o is None:
            o = self.s.scalar(select(ProductionOrder).where(ProductionOrder.number.ilike(number), ProductionOrder.plant_id == self.plan.plant_id))
        return o

    def order(self, number: str) -> dict[str, Any]:
        from .views import order_detail

        o = self.find_order(number)
        if o is None:
            return {"error": f"order {number} not found in this plant"}
        d = order_detail(self.s, self.ctx, self.plan.id, str(o.id))
        it = self.s.get(Item, o.item_id)
        self._src("order", str(o.id), o.number)
        res = d.get("result") or {}
        return {
            "number": o.number,
            "item": it.code if it else None,
            "quantity": o.quantity,
            "due": res.get("due"),
            "planned_start": res.get("start"),
            "planned_end": res.get("end"),
            "status": res.get("status"),
            "lateness_minutes": res.get("lateness_minutes"),
            "material_status": res.get("material_status"),
            "earliest_possible_end_infinite_capacity": (d.get("deadline") or {}).get("earliest_possible_infinite_capacity") or res.get("earliest_possible_end"),
            "required_additional_capacity_h": (d.get("deadline") or {}).get("required_additional_capacity_h"),
            "first_cause": (d.get("deadline") or {}).get("cause"),
            "root_cause_chain": [{"code": st.get("code"), "text": st.get("text"), "ref": st.get("ref")} for st in d.get("root_cause", [])],
            "unscheduled": [{"op": u["op_id"], "reason": u["reason"], "message": u["message"]} for u in d.get("unscheduled", [])],
            "operations": [{"op": r["op_key"], "resource": self._res_code(r["resource_key"]), "start": r["start"], "end": r["end"], "binding": (r.get("binding") or {}).get("type")} for r in d.get("operations", [])],
        }

    def _res_code(self, rid: str | None) -> str | None:
        if not rid:
            return None
        try:
            r = self.s.get(Resource, uuid.UUID(rid))
        except ValueError:
            return rid
        return r.code if r else rid

    def late_orders(self, limit: int = 10) -> dict[str, Any]:
        details = self.plan.kpi_details or {}
        late = details.get("late_orders", [])
        self._src("kpi", "late_orders", "Late orders")
        return {
            "count": details.get("late_orders_total", len(late)),
            "causes": (self.plan.kpi_details or {}).get("late_causes", {}),
            "orders": [{"number": x["number"], "status": x["status"], "lateness_minutes": x.get("lateness_minutes"), "due": x.get("due"), "end": x.get("end"), "cause": (x.get("cause") or {}).get("category"), "cause_text": (x.get("cause") or {}).get("text")} for x in late[: max(1, min(limit, 50))]],
        }

    def bottlenecks(self, limit: int = 5) -> dict[str, Any]:
        bn = (self.plan.analysis or {}).get("bottlenecks", [])[: max(1, min(limit, 20))]
        out = []
        for b in bn:
            code = self._res_code(b.get("resource_id")) if b.get("resource_id") else b.get("ref")
            if b.get("resource_id"):
                self._src("resource", b["resource_id"], code or "")
            out.append({**{k: b.get(k) for k in ("rank", "kind", "capacity_minutes", "scheduled_minutes", "requirement_minutes", "overload_minutes", "utilization", "induced_wait_minutes", "orders_affected")}, "resource": code, "causes": [{"label": c.get("label"), "minutes": c.get("minutes"), "share": c.get("share")} for c in b.get("causes", [])[:4]]})
        return {"bottlenecks": out}

    def material_shortages(self) -> dict[str, Any]:
        from .materials import availability

        a = availability(self.s, self.ctx, self.plan.id)
        self._src("materials", str(self.plan.id), "Material availability")
        return {
            "shortages": [{"material": x["material"], "material_id": x["material_id"], "shortfall": round(x["shortfall"], 3), "uom": x.get("uom"), "orders": len(x["orders"]), "operations": len(x["operations"])} for x in a["shortages"][:15]],
            "late_supply": [{"material": x["material"], "operations": x["operations"], "wait_minutes": x["wait_minutes"], "orders": len(x["orders"])} for x in a["late_supply"][:15]],
            "status_counts": a["status_counts"],
        }

    def material_impact(self, code: str) -> dict[str, Any]:
        from .materials import impact_of_material

        it = self.s.scalar(select(Item).where(Item.code == code)) or self.s.scalar(select(Item).where(Item.code.ilike(code)))
        if it is None:
            return {"error": f"material {code} not found"}
        d = impact_of_material(self.s, self.ctx, self.plan.id, str(it.id))
        self._src("item", str(it.id), it.code)
        return {"material": it.code, "orders_affected": d["orders_affected"], "customers_affected": d["customers_affected"], "revenue_exposure": d["revenue_exposure"], "orders": [{k: r[k] for k in ("number", "customer", "due", "planned_end", "status", "direct")} for r in d["orders"][:20]]}

    def resource_load(self, code: str) -> dict[str, Any]:
        r = self.s.scalar(select(Resource).where(Resource.code == code, Resource.plant_id == self.plan.plant_id)) or self.s.scalar(select(Resource).where(Resource.code.ilike(code), Resource.plant_id == self.plan.plant_id))
        if r is None:
            return {"error": f"resource {code} not found in this plant"}
        res = (self.plan.kpi_details or {}).get("resources", {}).get(str(r.id), {})
        bn = next((b for b in (self.plan.analysis or {}).get("bottlenecks", []) if b.get("resource_id") == str(r.id)), None)
        self._src("resource", str(r.id), r.code)
        return {"resource": r.code, "name": r.name, "status": r.status, "kind": r.kind, "load": res, "bottleneck": {k: bn.get(k) for k in ("rank", "kind", "overload_minutes", "induced_wait_minutes", "orders_affected")} if bn else None}

    def explain_operation(self, op_id: str) -> dict[str, Any]:
        from .views import operation_detail

        try:
            d = operation_detail(self.s, self.ctx, self.plan.id, op_id)
        except NotFound:
            return {"error": f"operation {op_id} not in this plan"}
        self._src("operation", op_id, op_id)
        sch = d.get("scheduled") or {}
        return {
            "op": op_id,
            "resource": self._res_code(sch.get("resource_key")),
            "start": sch.get("start"),
            "end": sch.get("end"),
            "binding": sch.get("binding"),
            "reasons": [{"code": x.get("code"), "text": x.get("text")} for x in (d.get("explanation") or {}).get("reasons", [])],
            "alternatives": (d.get("explanation") or {}).get("alternatives", [])[:5],
            "unscheduled": d.get("unscheduled"),
        }


# =============================================================================================
# grounded (rule-based) answers
# =============================================================================================

ORDER_RE = re.compile(r"\b([A-Z]{1,6}[-_/]?\d{2,}[-\w]*)\b", re.I)
OP_RE = re.compile(r"\b([A-Z]{1,6}[-_]?\d{2,}[-\w]*/\d{3})\b", re.I)


def _codes_in(q: str, known: list[str]) -> list[str]:
    ql = q.lower()
    found = [c for c in known if re.search(r"(?<![\w-])" + re.escape(c.lower()) + r"(?![\w-])", ql)]
    return sorted(found, key=len, reverse=True)


def grounded_answer(tools: Tools, question: str, lang: str) -> dict[str, Any]:
    s, plan, tz = tools.s, tools.plan, tools.tz
    q = question.strip()
    ql = q.lower()
    lines: list[str] = []
    actions: list[dict[str, Any]] = []
    intent = "UNKNOWN"

    res_codes = [c for c in s.scalars(select(Resource.code).where(Resource.plant_id == plan.plant_id))]
    mat_codes = list(s.scalars(select(Item.code).where(Item.item_type.in_(["RAW", "PACKAGING", "SEMI_FINISHED"]))))
    op_m = OP_RE.search(q)
    res_hit = _codes_in(q, res_codes)
    mat_hit = _codes_in(q, mat_codes)
    order_hit = None
    if not op_m:
        for m in ORDER_RE.finditer(q):
            cand = m.group(1)
            if cand.lower() in {c.lower() for c in res_codes + mat_codes}:
                continue
            if tools.find_order(cand) is not None:
                order_hit = cand
                break

    whatif = re.search(r"\b(what if|what happens if|qué pasa si|que pasa si|y si|si se (rompe|avería|averia|para))\b|\b(breaks? down|fails?|avería|averia|rompe)\b", ql)

    if op_m:
        intent = "OPERATION"
        d = tools.explain_operation(op_m.group(1).upper())
        if "error" in d:
            lines.append(d["error"])
        else:
            lines.append(_t("op_head", lang, op=d["op"]) + f" {d['resource']} · {_fmt_dt(d['start'], tz)} → {_fmt_dt(d['end'], tz)}")
            for r in d["reasons"]:
                lines.append(f"- {r['text']}")
            if d.get("unscheduled"):
                lines.append(f"- {d['unscheduled']['reason']}: {d['unscheduled']['message']}")
    elif whatif and res_hit:
        intent = "WHAT_IF"
        code = res_hit[0]
        hours = 8
        m = re.search(r"(\d+)\s*(h|hours|horas)", ql)
        if m:
            hours = int(m.group(1))
        rid = s.scalar(select(Resource.id).where(Resource.code == code, Resource.plant_id == plan.plant_id))
        label = f"{code} down {hours} h" if lang == "en" else f"{code} parada {hours} h"
        lines.append(_t("whatif", lang, label=label))
        from datetime import timedelta

        from ..core.clock import now as _now

        t0 = _now().replace(second=0, microsecond=0)
        actions.append({"type": "WHAT_IF", "kind": "BREAKDOWN", "label": label, "params": {"resource_id": str(rid), "start": t0.isoformat(), "end": (t0 + timedelta(hours=hours)).isoformat(), "reason": label}})
        d = tools.resource_load(code)
        if d.get("bottleneck"):
            lines.append(_t("resource_bn", lang, rank=d["bottleneck"]["rank"], kind=d["bottleneck"]["kind"]))
    elif order_hit:
        intent = "ORDER"
        d = tools.order(order_hit)
        st = d.get("status")
        if st == "UNSCHEDULED" or (d.get("unscheduled") and not d.get("planned_end")):
            lines.append(_t("order_unscheduled", lang, number=d["number"]))
        elif (d.get("lateness_minutes") or 0) > 0:
            lines.append(_t("order_late", lang, number=d["number"], end=_fmt_dt(d["planned_end"], tz), due=_fmt_dt(d["due"], tz), late=_fmt_minutes(d["lateness_minutes"])))
        else:
            lines.append(_t("order_on_time", lang, number=d["number"], end=_fmt_dt(d["planned_end"], tz), due=_fmt_dt(d["due"], tz)))
        if d["root_cause_chain"]:
            lines.append("")
            lines.append(_t("chain", lang))
            seen_txt: set[str] = set()
            for stp in d["root_cause_chain"]:
                txt = stp["text"] or ""
                m = re.match(r"^(\S+): (.*)$", txt)
                if m and m.group(2).startswith(m.group(1)):
                    txt = m.group(2)
                if txt in seen_txt:
                    continue
                seen_txt.add(txt)
                lines.append(f"- {txt}")
        for u in d.get("unscheduled", []):
            lines.append(f"- {u['op']}: {u['message']}")
        eft = d.get("earliest_possible_end_infinite_capacity")
        if (d.get("lateness_minutes") or 0) > 0 and eft:
            if d.get("due") and eft > d["due"]:
                lines.append(_t("deadline_impossible", lang, eft=_fmt_dt(eft, tz)))
            else:
                lines.append(_t("infinite", lang, eft=_fmt_dt(eft, tz)))
            if d.get("required_additional_capacity_h"):
                lines.append(_t("extra_capacity", lang, h=d["required_additional_capacity_h"]))
        actions.append({"type": "OPEN", "target": "order", "ref": d["number"]})
    elif mat_hit:
        intent = "MATERIAL_IMPACT"
        d = tools.material_impact(mat_hit[0])
        if "error" in d:
            lines.append(d["error"])
        elif not d["orders_affected"]:
            lines.append(_t("impact_none", lang, plan=plan.number, material=d["material"]))
        else:
            rev = f", {d['revenue_exposure']:,.0f} €" if d.get("revenue_exposure") else ""
            lines.append(_t("impact", lang, material=d["material"], n=d["orders_affected"], c=d["customers_affected"], rev=rev))
            for r in d["orders"][:12]:
                lines.append(f"- {r['number']} · {r['customer'] or '—'} · due {_fmt_dt(r['due'], tz)} · {r['status'] or '—'}")
    elif res_hit:
        intent = "RESOURCE"
        d = tools.resource_load(res_hit[0])
        load = d.get("load") or {}
        cap = load.get("capacity_h", 0)
        sched = load.get("busy_h", 0)
        util = f"{load['utilization']:.0f} %" if load.get("utilization") is not None else "—"
        lines.append(_t("resource", lang, code=d["resource"], name=d["name"], plan=plan.number, sched=sched, cap=cap, util=util))
        if d.get("bottleneck"):
            lines.append(_t("resource_bn", lang, rank=d["bottleneck"]["rank"], kind=d["bottleneck"]["kind"]))
    elif re.search(r"bottleneck|cuello|constraint|restricci|limit", ql):
        intent = "BOTTLENECKS"
        d = tools.bottlenecks(5)
        if not d["bottlenecks"]:
            lines.append(_t("no_bottleneck", lang, plan=plan.number))
        else:
            lines.append(_t("bottleneck_head", lang, plan=plan.number))
            for b in d["bottlenecks"]:
                extra = []
                if b.get("overload_minutes"):
                    extra.append(("overload " if lang == "en" else "sobrecarga ") + _fmt_minutes(b["overload_minutes"]))
                if b.get("induced_wait_minutes"):
                    extra.append(("induced waiting " if lang == "en" else "espera inducida ") + _fmt_minutes(b["induced_wait_minutes"]))
                if b.get("orders_affected"):
                    extra.append(f"{b['orders_affected']} " + ("orders" if lang == "en" else "órdenes"))
                util = f"{100 * (b.get('utilization') or 0):.0f} %"
                lines.append(f"{b['rank']}. **{b['resource'] or b['kind']}** ({b['kind']}, {util}) — " + ", ".join(extra))
    elif re.search(r"shortage|falta|rotura|stock|material|supply|suministro", ql):
        intent = "MATERIALS"
        d = tools.material_shortages()
        if not d["shortages"]:
            lines.append(_t("no_shortage", lang, plan=plan.number))
        else:
            lines.append(_t("shortage_head", lang, plan=plan.number))
            for x in d["shortages"][:10]:
                lines.append(f"- **{x['material']}**: {x['shortfall']:g} {x['uom'] or ''} · {x['orders']} " + ("orders" if lang == "en" else "órdenes"))
        if d["late_supply"]:
            lines.append(_t("late_supply", lang))
            for x in d["late_supply"][:8]:
                lines.append(f"- **{x['material']}**: {x['operations']} ops · {_fmt_minutes(x['wait_minutes'])}")
    elif re.search(r"\blate\b|delay|retras|tarde|atrasad|behind|otif|deadline", ql):
        intent = "LATE_ORDERS"
        d = tools.late_orders(10)
        if not d["count"]:
            lines.append(_t("no_late", lang, plan=plan.number))
        else:
            lines.append(_t("late_head", lang, n=d["count"], plan=plan.number))
            for x in d["orders"]:
                late = _fmt_minutes(x["lateness_minutes"]) if x["lateness_minutes"] else x["status"]
                lines.append(f"- **{x['number']}** · {late} · {x['cause'] or '—'}: {x['cause_text'] or ''}")
            if d["causes"]:
                lines.append(_t("causes", lang, causes=", ".join(f"{k} {v}" for k, v in sorted(d["causes"].items(), key=lambda kv: -kv[1]))))
    elif re.search(r"plan|summary|resumen|status|estado|kpi|how|cómo|como", ql):
        intent = "SUMMARY"
        d = tools.plan_overview()
        k = d["kpis"]
        lines.append(
            _t(
                "summary",
                lang,
                plan=d["plan"],
                status=d["status"],
                feasible=_t("feasible", lang) if d["feasible"] else (_t("incomplete", lang, n=d["unscheduled_operations"]) if not d["hard_violations_placed"] else _t("infeasible", lang)),
                otif=f"{k['otif']:.1f} %" if k.get("otif") is not None else "—",
                late=int(k.get("late_orders") or 0),
                uns=int(k.get("orders_unscheduled") or 0),
                util=f"{k['utilization']:.1f} %" if k.get("utilization") is not None else "—",
                setup=k.get("setup_h") or 0,
                provider=d["solver"]["provider"],
                sstatus=d["solver"]["status"],
            )
        )
    if intent == "UNKNOWN":
        o = s.scalar(select(ProductionOrder.number).where(ProductionOrder.plant_id == plan.plant_id).limit(1))
        lines.append(_t("no_match", lang, order=o or "WO-1001", material=mat_codes[0] if mat_codes else "MAT-A", resource=res_codes[0] if res_codes else "CNC-01"))
    return {"intent": intent, "answer": "\n".join(lines), "actions": actions}


# =============================================================================================
# optional LLM mode (Claude with read-only tools)
# =============================================================================================

TOOL_SPECS = [
    {"name": "plan_overview", "description": "KPIs, feasibility, solver status and late-order causes of the current plan.", "input_schema": {"type": "object", "properties": {}, "additionalProperties": False}},
    {"name": "order", "description": "Planned dates, status, lateness, root-cause chain and operations of one production order.", "input_schema": {"type": "object", "properties": {"number": {"type": "string", "description": "Order number, e.g. WO-24017"}}, "required": ["number"], "additionalProperties": False}},
    {"name": "late_orders", "description": "Late and unscheduled orders with their first cause, largest delays first.", "input_schema": {"type": "object", "properties": {"limit": {"type": "integer"}}, "required": [], "additionalProperties": False}},
    {"name": "bottlenecks", "description": "Ranked bottleneck resources with measured overload, induced waiting and causes.", "input_schema": {"type": "object", "properties": {"limit": {"type": "integer"}}, "required": [], "additionalProperties": False}},
    {"name": "material_shortages", "description": "Materials with uncovered demand and operations waiting for late supplies.", "input_schema": {"type": "object", "properties": {}, "additionalProperties": False}},
    {"name": "material_impact", "description": "Orders and customers depending on one material (reverse pegging).", "input_schema": {"type": "object", "properties": {"code": {"type": "string"}}, "required": ["code"], "additionalProperties": False}},
    {"name": "resource_load", "description": "Capacity, load, utilisation and bottleneck rank of one resource.", "input_schema": {"type": "object", "properties": {"code": {"type": "string"}}, "required": ["code"], "additionalProperties": False}},
    {"name": "explain_operation", "description": "Why an operation is placed where it is: binding constraint, reasons, alternatives.", "input_schema": {"type": "object", "properties": {"op_id": {"type": "string", "description": "Operation id <order number>/<seq>, e.g. WO-24017/020"}}, "required": ["op_id"], "additionalProperties": False}},
]

SYSTEM_PROMPT = """You are the planning assistant inside MonxuPlan, an advanced planning and scheduling system, answering a production planner at {plant}.
Current plan: {plan}. Plant time zone: {tz}.

Answer only from the results of your tools. If the tools do not contain the answer, say what is missing instead of estimating. Quote order numbers, resource codes, quantities and dates exactly as the tools return them; convert timestamps to the plant time zone.
You cannot change the plan and must not say that you did. When a change could help (extra shift, alternative machine, expediting a supply), describe it as a what-if scenario the planner can evaluate in MonxuPlan.
Reply in the language of the question, concisely, in Markdown."""


def llm_enabled() -> bool:
    if os.environ.get("MONXU_ASSISTANT_LLM", "0") not in ("1", "true", "yes"):
        return False
    try:
        import anthropic  # noqa: F401
    except ImportError:
        return False
    return True


def llm_answer(tools: Tools, question: str, history: list[dict[str, str]] | None, plant_name: str) -> dict[str, Any]:
    import anthropic

    client = anthropic.Anthropic()
    model = get_settings().assistant_model or DEFAULT_MODEL
    messages: list[dict[str, Any]] = []
    for h in (history or [])[-6:]:
        if h.get("role") in ("user", "assistant") and h.get("content"):
            messages.append({"role": h["role"], "content": str(h["content"])[:4000]})
    messages.append({"role": "user", "content": question[:4000]})
    system = SYSTEM_PROMPT.format(plant=plant_name, plan=tools.plan.number, tz=tools.tz)
    dispatch = {
        "plan_overview": lambda a: tools.plan_overview(),
        "order": lambda a: tools.order(str(a["number"])),
        "late_orders": lambda a: tools.late_orders(int(a.get("limit") or 10)),
        "bottlenecks": lambda a: tools.bottlenecks(int(a.get("limit") or 5)),
        "material_shortages": lambda a: tools.material_shortages(),
        "material_impact": lambda a: tools.material_impact(str(a["code"])),
        "resource_load": lambda a: tools.resource_load(str(a["code"])),
        "explain_operation": lambda a: tools.explain_operation(str(a["op_id"])),
    }
    calls: list[str] = []
    for _ in range(MAX_TOOL_ROUNDS):
        response = client.beta.messages.create(
            model=model,
            max_tokens=16000,
            system=system,
            tools=TOOL_SPECS,
            messages=messages,
            output_config={"effort": "medium"},
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        )
        if response.stop_reason == "refusal":
            return {"answer": None, "error": "The language model declined to answer; showing the rule-based answer.", "tool_calls": calls}
        if response.stop_reason != "tool_use":
            text = "\n".join(b.text for b in response.content if b.type == "text").strip()
            return {"answer": text, "tool_calls": calls, "model": response.model}
        messages.append({"role": "assistant", "content": response.content})
        results = []
        for block in response.content:
            if block.type != "tool_use":
                continue
            calls.append(block.name)
            fn = dispatch.get(block.name)
            try:
                if fn is None:
                    raise ValueError(f"unknown tool {block.name}")
                out = fn(block.input if isinstance(block.input, dict) else json.loads(block.input))
                results.append({"type": "tool_result", "tool_use_id": block.id, "content": json.dumps(out, default=str)[:60000]})
            except Exception as exc:  # noqa: BLE001 - reported to the model as a tool error
                results.append({"type": "tool_result", "tool_use_id": block.id, "content": f"error: {exc}", "is_error": True})
        messages.append({"role": "user", "content": results})
    return {"answer": None, "error": "The language model did not finish within the tool-call limit; showing the rule-based answer.", "tool_calls": calls}


# =============================================================================================
# entry point
# =============================================================================================


def ask(s: Session, ctx: Ctx, question: str, plant_id: uuid.UUID | None = None, plan_id: uuid.UUID | None = None, history: list[dict[str, str]] | None = None) -> dict[str, Any]:
    ctx.require("plan:read")
    if not question or not question.strip():
        raise ValidationFailed("Ask a question", code="EMPTY_QUESTION")
    if len(question) > 2000:
        raise ValidationFailed("Question too long (max 2000 characters)", code="QUESTION_TOO_LONG")
    plan = _resolve_plan(s, ctx, plant_id, plan_id)
    tools = Tools(s, ctx, plan)
    lang = _lang(question, ctx)
    grounded = grounded_answer(tools, question, lang)
    out = {
        "question": question,
        "plan": {"id": str(plan.id), "number": plan.number},
        "mode": "GROUNDED",
        "intent": grounded["intent"],
        "answer": grounded["answer"],
        "actions": grounded["actions"],
        "sources": tools.sources,
        "note": "Answer built from the plan's stored data by MonxuPlan's rule-based assistant.",
    }
    if llm_enabled():
        plant = s.get(Plant, plan.plant_id)
        try:
            r = llm_answer(tools, question, history, plant.name if plant else "the plant")
        except Exception as exc:  # noqa: BLE001 - never fail the request because the LLM is unavailable
            log.warning("assistant LLM call failed: %s", exc.__class__.__name__)
            r = {"answer": None, "error": f"The language model is not reachable ({exc.__class__.__name__}); showing the rule-based answer."}
        if r.get("answer"):
            out.update(mode="LLM", answer=r["answer"], sources=tools.sources, tool_calls=r.get("tool_calls", []), model=r.get("model"), note="Answer written by a language model from MonxuPlan's read-only plan data (tools used: " + (", ".join(r.get("tool_calls") or []) or "none") + "). It cannot change the plan.")
        else:
            out["note"] = r.get("error") or out["note"]
    return out
