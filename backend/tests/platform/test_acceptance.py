"""§195 acceptance test: a new company imports its factory from files and runs the full loop.

import → validate → plan → Gantt → constraints → bottlenecks → KPIs → explain → scenario →
modify machine → replan → compare → publish.
"""

from datetime import date

from conftest import Api

from monxuplan.seed.acceptance import IMPORT_ORDER, build_files


def test_acceptance_195(client):
    from monxuplan.services.tenants import create_tenant

    info = create_tenant("Acceptance Industries", "acceptance", "acc.admin", "admin@acceptance.example", "Accept-Pass-2026", plant_code="ACC", plant_name="Acceptance plant", timezone="Europe/Madrid")
    api = Api(client, "acc.admin", "Accept-Pass-2026", tenant="acceptance")
    plant_id, scenario_id = info["plant_id"], info["scenario_id"]

    # 1. import every file through the wizard (auto-mapping must recognise the customer's headers)
    files = build_files(date.today())
    for entity in IMPORT_ORDER:
        name, data = files[entity]
        job = api.ok(api.post("/imports", data={"entity": entity, "plant_id": plant_id}, files={"file": (name, data)}), 201)
        if entity == "production-orders":
            api.ok(api.put(f"/imports/{job['id']}/mapping", json={"mapping": job["mapping"], "options": {"date_format": "DMY"}}))
        v = api.ok(api.post(f"/imports/{job['id']}/validate"))
        assert v["stats"]["can_import"], (entity, v["errors"][:5])
        c = api.ok(api.post(f"/imports/{job['id']}/commit"))
        assert c["status"] == "IMPORTED"
    orders = api.ok(api.get("/orders", params={"plant_id": plant_id, "limit": 500}))
    assert orders["total"] == 100
    machines = api.ok(api.get("/master-data/machines", params={"plant_id": plant_id}))
    assert machines["total"] == 20
    assert api.ok(api.get("/master-data/products"))["total"] == 20

    # 2. data quality: no blocking error
    dq = api.ok(api.get("/data-quality", params={"plant_id": plant_id}))
    assert dq["summary"]["errors"] == 0, [c for c in dq["checks"] if c["status"] == "ERROR"]

    # 3. plan
    r = api.ok(api.post("/planning/run", json={"scenario_id": scenario_id, "solver": {"provider": "hybrid", "time_limit_s": 15}}), 202)
    run = api.wait_run(r["run_id"])
    assert run["status"] == "SUCCEEDED", run.get("error_message")
    plan = api.ok(api.get(f"/plans/{run['plan_id']}"))
    assert plan["operations"] == 300
    assert plan["hard_violations_placed"] == 0
    assert plan["unscheduled_count"] == 0, "materials are covered by stock + receipts; every operation must be scheduled"

    # 4. Gantt and constraint checks
    g = api.ok(api.get(f"/plans/{plan['id']}/gantt"))
    assert len(g["operations"]) == 300 and len(g["resources"]) == 20
    by_res = {}
    for o in g["operations"]:
        by_res.setdefault(o["resource_id"], []).append((o["setup_start"], o["end"]))
    for iv in by_res.values():
        iv.sort()
        assert all(a[1] <= b[0] for a, b in zip(iv, iv[1:])), "unary machines must never overlap"
    val = api.ok(api.post(f"/plans/{plan['id']}/validate"))
    assert val

    # 5. bottlenecks & KPIs
    bn = api.ok(api.get("/bottlenecks", params={"plan_id": plan["id"]}))["bottlenecks"]
    assert bn, "the milling/turning load must produce a ranked bottleneck"
    kpis = api.ok(api.get("/kpis", params={"plan_id": plan["id"]}))["values"]
    assert kpis["otif"] is not None and 0 <= kpis["otif"] <= 100

    # 6. explain one operation on the top bottleneck
    res_bn = [b for b in bn if b.get("resource_id")]
    assert res_bn, bn[:3]
    top = res_bn[0]["resource_id"]
    grd1 = next(r["id"] for r in g["resources"] if r["code"] == "GRD-1")
    assert top == grd1, "GRD-1 (single qualified grinder) must be the top resource bottleneck"
    op = next(o for o in g["operations"] if o["resource_id"] == top)
    exp = api.ok(api.get(f"/plans/{plan['id']}/operations/{op['id']}/explore"))
    assert exp

    # 7. scenario: clone live, make the bottleneck machine 30 % slower (worn tooling), replan
    sc = api.ok(api.post(f"/scenarios/{scenario_id}/clone", json={"name": "Slow bottleneck"}), 201)
    api.ok(api.post(f"/scenarios/{sc['id']}/changes", json={"type": "CHANGE_EFFICIENCY", "payload": {"resource_id": top, "efficiency": 0.7}, "description": "bottleneck at 70 %"}), 201)
    r2 = api.ok(api.post("/planning/run", json={"scenario_id": sc["id"], "solver": {"provider": "hybrid", "time_limit_s": 15}, "force": True}), 202)
    run2 = api.wait_run(r2["run_id"])
    assert run2["status"] == "SUCCEEDED", run2.get("error_message")

    # 8. compare
    cmp_ = api.ok(api.get("/analytics/compare", params=[("plan_ids", plan["id"]), ("plan_ids", run2["plan_id"])]))
    assert len(cmp_["plans"]) == 2

    # 9. publish the live plan
    pub = api.ok(api.post(f"/plans/{plan['id']}/publish", json={"reason": "acceptance"}))
    assert pub["status"] == "PUBLISHED"
