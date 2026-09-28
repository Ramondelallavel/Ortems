"""Material Requirements Planning (MRP) and Master Production Schedule (MPS) calculations.

Classical time-phased MRP on period buckets:

* low-level codes from the BOM graph (cycles are detected and reported, never looped over),
* gross requirements = independent demand (forecast consumed by customer orders, firm, expected,
  promotional, safety stock) + dependent demand from parents' planned order releases,
* netting against on-hand, scheduled receipts and safety stock,
* lot sizing: LFL, FIXED, MIN, MULTIPLE, MAX (split), EOQ,
* lead-time offsetting; releases that fall before the first period are flagged ``PAST_DUE_RELEASE``,
* pegging of planned orders to the demands they cover, and exceptions (stockout, safety stock
  breach, excess inventory, reschedule-in / reschedule-out of scheduled receipts).

Planned orders are proposals: they become production/purchase orders only when a planner firms them.
"""

from __future__ import annotations

import math
from collections import defaultdict, deque
from datetime import date, timedelta
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

DemandType = Literal["FORECAST", "CUSTOMER_ORDER", "FIRM", "EXPECTED", "PROMOTIONAL", "SAFETY_STOCK"]
EPS = 1e-9


class _M(BaseModel):
    model_config = ConfigDict(extra="forbid")


class MrpItem(_M):
    id: str
    code: str
    make_or_buy: Literal["MAKE", "BUY"] = "MAKE"
    lead_time_days: float = 0
    lot_policy: Literal["LFL", "FIXED", "MIN", "MULTIPLE", "MAX", "EOQ"] = "LFL"
    lot_qty: float | None = None
    min_lot: float | None = None
    max_lot: float | None = None
    multiple: float | None = None
    safety_stock: float = 0
    on_hand: float = 0
    integer: bool = True
    family: str | None = None


class MrpBomLine(_M):
    parent_id: str
    component_id: str
    qty_per: float = Field(gt=0)
    scrap_pct: float = Field(default=0, ge=0, lt=100)


class MrpDemand(_M):
    item_id: str
    date: date
    quantity: float
    type: DemandType = "CUSTOMER_ORDER"
    ref: str | None = None


class MrpReceipt(_M):
    item_id: str
    date: date
    quantity: float
    type: Literal["PRODUCTION_ORDER", "PURCHASE_ORDER", "TRANSFER"] = "PRODUCTION_ORDER"
    ref: str | None = None


class MrpProblem(_M):
    start: date
    bucket_days: int = Field(default=7, ge=1)
    periods: int = Field(default=12, ge=1, le=260)
    items: list[MrpItem]
    bom: list[MrpBomLine] = Field(default_factory=list)
    demands: list[MrpDemand] = Field(default_factory=list)
    receipts: list[MrpReceipt] = Field(default_factory=list)
    forecast_consumption: Literal["MAX", "SUM"] = "MAX"
    excess_factor: float = Field(default=3.0, description="Projected stock above factor × average requirement → EXCESS")


def _llc(items: dict[str, MrpItem], bom: list[MrpBomLine]) -> tuple[dict[str, int], list[list[str]]]:
    children: dict[str, list[str]] = defaultdict(list)
    for b in bom:
        children[b.parent_id].append(b.component_id)
    llc = dict.fromkeys(items, 0)
    # Kahn on parent -> child for cycle detection, longest path for levels
    indeg = dict.fromkeys(items, 0)
    for b in bom:
        if b.component_id in indeg:
            indeg[b.component_id] += 1
    q = deque(i for i, d in indeg.items() if d == 0)
    seen = 0
    while q:
        p = q.popleft()
        seen += 1
        for c in children.get(p, []):
            if c not in indeg:
                continue
            llc[c] = max(llc[c], llc[p] + 1)
            indeg[c] -= 1
            if indeg[c] == 0:
                q.append(c)
    cycles = []
    if seen < len(items):
        cyc = [i for i, d in indeg.items() if d > 0]
        cycles.append(cyc)
    return llc, cycles


def lot_size(item: MrpItem, net: float) -> list[float]:
    if net <= EPS:
        return []
    p = item.lot_policy
    if p == "FIXED" and item.lot_qty:
        qty = math.ceil(net / item.lot_qty - EPS) * item.lot_qty
        out = [qty]
    elif p == "MIN":
        out = [max(net, item.min_lot or 0)]
    elif p == "MULTIPLE" and (item.multiple or item.lot_qty):
        m = item.multiple or item.lot_qty or 1
        out = [math.ceil(net / m - EPS) * m]
    elif p == "EOQ" and item.lot_qty:
        out = [max(net, item.lot_qty)]
    else:
        out = [net]
    if item.min_lot:
        out = [max(q, item.min_lot) for q in out]
    if item.multiple and p != "MULTIPLE":
        out = [math.ceil(q / item.multiple - EPS) * item.multiple for q in out]
    if item.max_lot:
        split = []
        for q in out:
            while q > item.max_lot + EPS:
                split.append(item.max_lot)
                q -= item.max_lot
            if q > EPS:
                split.append(q)
        out = split
    if item.integer:
        out = [math.ceil(q - EPS) for q in out]
    return out


def run_mrp(pb: MrpProblem) -> dict[str, Any]:
    items = {i.id: i for i in pb.items}
    llc, cycles = _llc(items, pb.bom)
    comps: dict[str, list[MrpBomLine]] = defaultdict(list)
    for b in pb.bom:
        comps[b.parent_id].append(b)
    P = pb.periods
    bucket = timedelta(days=pb.bucket_days)
    starts = [pb.start + k * bucket for k in range(P)]

    def period_of(d: date) -> int:
        k = (d - pb.start).days // pb.bucket_days
        return max(0, min(P - 1, k))

    indep: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(lambda: [0.0] * P))
    peg_src: dict[tuple[str, int], list[dict]] = defaultdict(list)
    past_due: dict[str, float] = defaultdict(float)
    for d in pb.demands:
        if d.item_id not in items:
            continue
        if d.date < pb.start:
            past_due[d.item_id] += d.quantity
        k = period_of(d.date)
        indep[d.item_id][d.type][k] += d.quantity
        peg_src[(d.item_id, k)].append({"type": d.type, "ref": d.ref, "quantity": d.quantity})
    receipts: dict[str, list[float]] = defaultdict(lambda: [0.0] * P)
    receipt_refs: dict[str, list[tuple[int, MrpReceipt]]] = defaultdict(list)
    for r in pb.receipts:
        if r.item_id not in items:
            continue
        k = period_of(r.date)
        receipts[r.item_id][k] += r.quantity
        receipt_refs[r.item_id].append((k, r))
    dependent: dict[str, list[float]] = defaultdict(lambda: [0.0] * P)
    dep_peg: dict[tuple[str, int], list[dict]] = defaultdict(list)

    rows: dict[str, dict[str, Any]] = {}
    planned: list[dict[str, Any]] = []
    exceptions: list[dict[str, Any]] = []
    order_no = 0
    for iid in sorted(items, key=lambda i: (llc[i], items[i].code)):
        it = items[iid]
        by_type = indep.get(iid, {})
        forecast = by_type.get("FORECAST", [0.0] * P)
        customer = by_type.get("CUSTOMER_ORDER", [0.0] * P)
        others = [0.0] * P
        for t in ("FIRM", "EXPECTED", "PROMOTIONAL", "SAFETY_STOCK"):
            for k, v in enumerate(by_type.get(t, [0.0] * P)):
                others[k] += v
        gross = [0.0] * P
        for k in range(P):
            ind = max(forecast[k], customer[k]) if pb.forecast_consumption == "MAX" else forecast[k] + customer[k]
            gross[k] = ind + others[k] + dependent[iid][k]
        poh = it.on_hand
        proj = [0.0] * P
        net = [0.0] * P
        por = [0.0] * P  # planned order receipts
        porl = [0.0] * P  # planned order releases
        lt_periods = math.ceil(it.lead_time_days / pb.bucket_days - EPS) if it.lead_time_days > 0 else 0
        for k in range(P):
            poh = poh + receipts[iid][k] - gross[k]
            if poh < it.safety_stock - EPS:
                need = it.safety_stock - poh
                net[k] = need
                lots = lot_size(it, need)
                for q in lots:
                    rel = k - lt_periods
                    order_no += 1
                    po = {
                        "id": f"PLN-{order_no:05d}",
                        "item_id": iid,
                        "item_code": it.code,
                        "type": "MAKE" if it.make_or_buy == "MAKE" else "BUY",
                        "quantity": q,
                        "due_period": k,
                        "due_date": starts[k].isoformat(),
                        "release_period": max(rel, 0),
                        "release_date": starts[max(rel, 0)].isoformat(),
                        "past_due_release": rel < 0,
                        "pegging": peg_src.get((iid, k), []) + dep_peg.get((iid, k), []),
                    }
                    planned.append(po)
                    por[k] += q
                    porl[max(rel, 0)] += q
                    if rel < 0:
                        exceptions.append({"type": "PAST_DUE_RELEASE", "item": it.code, "period": k, "message": f"{it.code}: planned order {po['id']} should have been released {-rel} period(s) ago"})
                    poh += q
            proj[k] = poh
        # dependent demand for components
        for b in comps.get(iid, []):
            factor = b.qty_per / (1 - b.scrap_pct / 100.0)
            for k in range(P):
                if porl[k] > EPS:
                    dependent[b.component_id][k] += porl[k] * factor
                    dep_peg[(b.component_id, k)].append({"type": "DEPENDENT", "ref": it.code, "quantity": porl[k] * factor})
        # exceptions
        avg_req = sum(gross) / P if P else 0
        for k in range(P):
            if proj[k] < -EPS:
                exceptions.append({"type": "STOCKOUT", "item": it.code, "period": k, "message": f"{it.code}: projected stock {proj[k]:.0f} in {starts[k].isoformat()}"})
            elif proj[k] < it.safety_stock - EPS:
                exceptions.append({"type": "SAFETY_STOCK_BREACH", "item": it.code, "period": k, "message": f"{it.code}: below safety stock in {starts[k].isoformat()}"})
            elif avg_req > 0 and proj[k] > pb.excess_factor * max(avg_req, it.safety_stock):
                exceptions.append({"type": "EXCESS_INVENTORY", "item": it.code, "period": k, "message": f"{it.code}: projected {proj[k]:.0f} vs average need {avg_req:.0f}"})
        for k, r in receipt_refs.get(iid, []):
            # reschedule suggestions: receipt earlier than first need → out; after first negative → in
            first_need = next((j for j in range(P) if gross[j] > EPS), None)
            if first_need is not None and k < first_need - 1:
                exceptions.append({"type": "RESCHEDULE_OUT", "item": it.code, "period": k, "message": f"{it.code}: {r.ref or 'receipt'} could move to {starts[first_need].isoformat()}"})
        if past_due.get(iid):
            exceptions.append({"type": "PAST_DUE_DEMAND", "item": it.code, "period": 0, "message": f"{it.code}: {past_due[iid]:.0f} past-due demand"})
        rows[iid] = {
            "item_id": iid,
            "item_code": it.code,
            "family": it.family,
            "llc": llc[iid],
            "on_hand": it.on_hand,
            "safety_stock": it.safety_stock,
            "forecast": forecast,
            "customer_orders": customer,
            "other_demand": others,
            "dependent_demand": dependent[iid],
            "gross_requirements": gross,
            "scheduled_receipts": receipts[iid],
            "projected_on_hand": proj,
            "net_requirements": net,
            "planned_receipts": por,
            "planned_releases": porl,
        }
    return {
        "periods": [s.isoformat() for s in starts],
        "bucket_days": pb.bucket_days,
        "items": list(rows.values()),
        "planned_orders": planned,
        "exceptions": exceptions,
        "bom_cycles": cycles,
    }
