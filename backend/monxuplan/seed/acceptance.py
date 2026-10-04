"""Acceptance dataset (§195): a small but complete factory delivered as import files.

20 machines in 7 areas, 20 products, 15 raw materials, 100 production orders with 300 operations,
BOMs, routings with alternatives, inventory and open purchase orders. Files are produced exactly as a
customer would provide them (Excel sheets and one semicolon-separated CSV with DD/MM/YYYY dates) so
the acceptance test exercises the import wizard end to end.

    python -m monxuplan.seed.acceptance ./acceptance_data     # writes the files
"""

from __future__ import annotations

import csv
import io
import random
from datetime import date, timedelta
from pathlib import Path

IMPORT_ORDER = ["calendars", "customers", "suppliers", "items", "resources", "boms", "routings", "inventory", "purchase-orders", "production-orders"]

AREAS = {"Cutting": ["SAW-1", "SAW-2"], "Turning": ["LAT-1", "LAT-2", "LAT-3", "LAT-4"], "Milling": ["MIL-1", "MIL-2", "MIL-3", "MIL-4"], "Grinding": ["GRD-1", "GRD-2"], "Welding": ["WLD-1", "WLD-2"], "Assembly": ["ASM-1", "ASM-2", "ASM-3"], "Packing": ["PKG-1", "PKG-2", "PKG-3"]}


def _xlsx(header: list[str], rows: list[list]) -> bytes:
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.title = "data"
    ws.append(header)
    for r in rows:
        ws.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _csv(header: list[str], rows: list[list], delimiter: str = ";") -> bytes:
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=delimiter)
    w.writerow(header)
    w.writerows(rows)
    return buf.getvalue().encode("utf-8")


def build_files(today: date, seed: int = 195) -> dict[str, tuple[str, bytes]]:
    rng = random.Random(seed)
    files: dict[str, tuple[str, bytes]] = {}

    # calendars: two shifts Mon–Fri with breaks; 24×5 for the welding robots
    cal_rows = []
    for wd in range(5):
        cal_rows.append(["ACC-2S", "Two shifts", "Europe/Madrid", wd, "M", "06:00", "14:00", "REGULAR", "10:00-10:20"])
        cal_rows.append(["ACC-2S", "Two shifts", "Europe/Madrid", wd, "T", "14:00", "22:00", "REGULAR", "18:00-18:20"])
        cal_rows.append(["ACC-24x5", "24x5", "Europe/Madrid", wd, "D", "00:00", "24:00", "REGULAR", ""])
    files["calendars"] = ("calendars.xlsx", _xlsx(["calendar", "name", "timezone", "weekday", "shift", "start", "end", "kind", "breaks"], cal_rows))

    customers = [("C-ALFA", "Alfa Motors", 1, "yes"), ("C-BETA", "Beta Rail", 3, ""), ("C-GAMMA", "Gamma Agro", 5, ""), ("C-DELTA", "Delta Marine", 4, ""), ("C-EPS", "Epsilon Energy", 2, "yes"), ("C-ZETA", "Zeta Tools", 6, "")]
    files["customers"] = ("customers.xlsx", _xlsx(["code", "name", "priority", "strategic"], [list(c) for c in customers]))
    suppliers = [("S-STEEL", "Aceros del Sur", 7), ("S-PARTS", "Componentes Norte", 10), ("S-PACK", "Embalajes Levante", 4)]
    files["suppliers"] = ("suppliers.xlsx", _xlsx(["code", "name", "lead_time_days"], [list(x) for x in suppliers]))

    # items
    families = ["SHAFT", "BRACKET", "HOUSING", "FRAME"]
    rms = [(f"RM-{k:02d}", f"Raw material {k:02d}", "RAW", "BUY", "kg" if k <= 8 else "pcs", ["S-STEEL", "S-PARTS", "S-PACK"][0 if k <= 8 else 1 if k <= 13 else 2]) for k in range(1, 16)]
    products = [(f"P-{k:03d}", f"{families[(k - 1) % 4].title()} model {k:03d}", "FINISHED", "MAKE", "pcs", families[(k - 1) % 4]) for k in range(1, 21)]
    item_rows = [[c, n, t, m, u, None, 0, 5] for c, n, t, m, u, _s in rms] + [[c, n, t, m, u, f, 0, 0] for c, n, t, m, u, f in products]
    files["items"] = ("items.xlsx", _xlsx(["code", "description", "type", "make_or_buy", "unit", "family", "safety_stock", "lead_time_days"], item_rows))

    # resources
    res_rows = []
    for area, codes in AREAS.items():
        for code in codes:
            cal = "ACC-24x5" if area == "Welding" else "ACC-2S"  # welding robots run 24×5
            group = {"Turning": "LATHES", "Milling": "MILLS", "Assembly": "ASSEMBLY", "Packing": "PACKING"}.get(area, "")
            res_rows.append([code, f"{area} {code[-1]}", "MACHINE", area, cal, 1, 1.0, 45 if area in ("Milling", "Grinding") else 30, group])
    files["resources"] = ("resources.xlsx", _xlsx(["code", "name", "kind", "area", "calendar", "capacity", "efficiency", "cost_per_hour", "groups"], res_rows))

    # BOMs: two raw materials per product
    bom_rows = []
    prod_rm: dict[str, list[tuple[str, float]]] = {}
    for c, *_ in products:
        a, b = rng.sample(range(1, 16), 2)
        qa, qb = round(rng.uniform(0.5, 3.0), 2), float(rng.choice([1, 2, 4]))
        prod_rm[c] = [(f"RM-{a:02d}", qa), (f"RM-{b:02d}", qb)]
        bom_rows.append([c, f"RM-{a:02d}", qa, 0, 10])
        bom_rows.append([c, f"RM-{b:02d}", qb, 0, 30])
    files["boms"] = ("boms.xlsx", _xlsx(["parent", "component", "quantity_per", "scrap_pct", "operation_seq"], bom_rows))

    # routings: 3 operations per product (cut → machine → assemble/pack), alternatives inside each area
    rt_rows = []
    for k, (c, _n, _t, _m, _u, fam) in enumerate(products):
        saw = AREAS["Cutting"][k % 2]
        rt_rows.append([c, 10, "CUT", "Cut blanks", saw, AREAS["Cutting"][(k + 1) % 2], 20, round(rng.uniform(0.6, 1.2), 2), "", f"family={fam}"])
        if fam in ("SHAFT", "HOUSING"):
            lat = AREAS["Turning"][k % 4]
            alts = ",".join(x for x in AREAS["Turning"] if x != lat)
            rt_rows.append([c, 20, "TURN", "Turning", lat, alts, 45, round(rng.uniform(3.0, 6.0), 2), "", f"family={fam}"])
        else:
            mil = AREAS["Milling"][k % 4]
            alts = AREAS["Milling"][(k + 1) % 4]
            rt_rows.append([c, 20, "MILL", "Milling", mil, alts, 60, round(rng.uniform(5.0, 9.0), 2), "", f"family={fam}"])
        if fam == "FRAME":
            rt_rows.append([c, 30, "WELD", "Welding", AREAS["Welding"][k % 2], AREAS["Welding"][(k + 1) % 2], 15, round(rng.uniform(2.0, 4.0), 2), "", ""])
        elif fam == "SHAFT":
            # precision grinding: only GRD-1 is qualified for shafts → the plant's bottleneck
            rt_rows.append([c, 30, "GRIND", "Precision grinding", "GRD-1", "", 30, round(rng.uniform(9.0, 14.0), 2), "", ""])
        else:
            asm = AREAS["Assembly"][k % 3]
            rt_rows.append([c, 30, "ASM", "Assembly and pack", asm, ",".join(x for x in AREAS["Assembly"] if x != asm), 10, round(rng.uniform(2.0, 4.0), 2), "", ""])
    files["routings"] = ("routings.xlsx", _xlsx(["item_code", "seq", "operation_code", "operation_name", "resource", "alternatives", "setup_minutes", "run_minutes_per_unit", "labor_pool", "setup_attributes"], rt_rows))

    # orders: 100 × 3 operations = 300 operations
    order_rows = []
    demand: dict[str, float] = {}
    work_days = [today + timedelta(days=d) for d in range(3, 40) if (today + timedelta(days=d)).weekday() < 5]
    for n in range(1, 101):
        p = products[rng.randrange(20)][0]
        qty = rng.choice([40, 50, 60, 80, 100, 120])
        due = work_days[min(len(work_days) - 1, int(rng.triangular(0, len(work_days) - 1, len(work_days) * 0.4)))]
        cust = customers[rng.randrange(len(customers))][0]
        prio = rng.choice([2, 3, 5, 5, 5, 7])
        rush = "yes" if n % 23 == 0 else ""
        order_rows.append([f"ACC-{n:04d}", p, qty, f"{due:%d/%m/%Y} 22:00", prio, cust, rush])
        for rm, q in prod_rm[p]:
            demand[rm] = demand.get(rm, 0) + q * qty
    files["production-orders"] = ("orders.csv", _csv(["Order", "Item", "Qty", "Due", "Priority", "Customer", "Urgent"], order_rows))

    # stock covers ~70 % of demand, open POs the rest (arriving within 2–9 days)
    inv_rows, po_rows = [], []
    for k, (rm, need) in enumerate(sorted(demand.items())):
        on_hand = round(need * 0.7, 1)
        inv_rows.append([rm, "MAIN", on_hand])
        sup = next(x[5] for x in rms if x[0] == rm)
        po_rows.append([f"PO-{k + 1:04d}", 1, sup, rm, round(need * 0.45, 1), (today + timedelta(days=2 + k % 8)).isoformat() + " 08:00", "yes"])
    files["inventory"] = ("inventory.xlsx", _xlsx(["item_code", "location", "on_hand"], inv_rows))
    files["purchase-orders"] = ("purchase_orders.xlsx", _xlsx(["po_number", "line", "supplier", "item_code", "quantity", "expected_date", "confirmed"], po_rows))
    return files


def main(argv: list[str] | None = None) -> None:  # pragma: no cover
    import argparse

    ap = argparse.ArgumentParser(
        prog="python -m monxuplan.seed.acceptance",
        description="Write the acceptance dataset (Excel/CSV files of a complete small plant, dated from today) into a folder, "
        "in the order they are imported: calendars, customers, suppliers, items, resources, BOMs, routings, inventory, purchase and production orders.",
    )
    ap.add_argument("folder", nargs="?", default="acceptance_data", help="output folder (created if needed; default: ./acceptance_data)")
    out = Path(ap.parse_args(argv).folder)
    out.mkdir(parents=True, exist_ok=True)
    files = build_files(date.today())
    for entity in IMPORT_ORDER:
        name, data = files[entity]
        (out / name).write_bytes(data)
        print(f"{entity:18s} → {out / name}")


if __name__ == "__main__":  # pragma: no cover
    main()
