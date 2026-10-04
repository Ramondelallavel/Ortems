"""Publication gate: only the current version, validated by policy, re-validated independently on its
stored schedule, complete, with explicit and recorded overrides."""

import uuid
from datetime import timedelta

import pytest


def _session(api):
    from monxuplan.core.db import new_session

    return new_session(uuid.UUID(api.ok(api.get("/auth/me"))["tenant_id"]), "tests")


@pytest.fixture()
def whatif(planner, sevilla, base_plan):
    """A what-if copy of the Sevilla live scenario with its own optimised plan; the plant's published
    plan and settings are restored afterwards."""
    from monxuplan.models import Plant

    with _session(planner) as s:
        plant = s.get(Plant, uuid.UUID(sevilla["id"]))
        before = (plant.published_plan_id, dict(plant.settings or {}))
    sc = planner.ok(planner.post(f"/scenarios/{sevilla['live_scenario_id']}/clone", json={"name": f"gate-{uuid.uuid4().hex[:6]}"}), 201)
    r = planner.ok(planner.post("/planning/run", json={"scenario_id": sc["id"], "solver": {"provider": "heuristic", "time_limit_s": 5}}), 202)
    run = planner.wait_run(r["run_id"])
    assert run["status"] == "SUCCEEDED", run
    yield sc, run["plan_id"]
    with _session(planner) as s:
        plant = s.get(Plant, uuid.UUID(sevilla["id"]))
        plant.published_plan_id, plant.settings = before
        s.commit()


def _blockers(r):
    assert r.status_code == 422, r.text
    body = r.json()["error"]
    assert body["code"] == "PUBLISH_BLOCKED", body
    return {b["code"]: b for b in body["context"]["blockers"]}


def _tamper(planner, plan_id, overlap=False, drop=False):
    """Change stored rows behind the engine's back (a corrupted import, a bad manual SQL fix…)."""
    from sqlalchemy import delete, select

    from monxuplan.models import ScheduledOperation as SO

    with _session(planner) as s:
        rows = s.execute(select(SO).where(SO.plan_id == uuid.UUID(plan_id)).order_by(SO.resource_key, SO.setup_start)).scalars().all()
        if drop:
            s.execute(delete(SO).where(SO.id == rows[-1].id))
        if overlap:
            a, b = next((a, b) for a, b in zip(rows, rows[1:], strict=False) if a.resource_key == b.resource_key)
            span = b.end - b.setup_start
            b.setup_start = a.setup_start + timedelta(minutes=1)
            b.start = b.setup_start
            b.end = b.setup_start + span
        s.commit()


def test_incomplete_or_violating_plan_is_blocked_and_force_is_recorded(client, planner, whatif):
    from conftest import Api

    _sc, pid = whatif
    _tamper(planner, pid, overlap=True, drop=True)
    b = _blockers(planner.post(f"/plans/{pid}/publish", json={}))
    assert "UNSCHEDULED_OPERATIONS" in b and "HARD_VIOLATIONS" in b
    assert any(d["type"] in ("CAPACITY_OVERLAP", "SETUP_INSUFFICIENT") for d in b["HARD_VIOLATIONS"]["details"])
    # force always needs a reason
    r = planner.post(f"/plans/{pid}/publish", json={"force": True})
    assert r.status_code == 422 and r.json()["error"]["code"] == "REASON_REQUIRED"
    pub = planner.ok(planner.post(f"/plans/{pid}/publish", json={"force": True, "reason": "customer escalation, fixed on the floor"}))
    assert pub["status"] == "PUBLISHED"
    assert {o["code"] for o in pub["publish_overrides"]} >= {"UNSCHEDULED_OPERATIONS", "HARD_VIOLATIONS"}
    admin = Api(client, "admin")
    audit = admin.ok(admin.get("/audit", params={"entity_id": pid}))
    rows = audit["items"] if isinstance(audit, dict) else audit
    assert any(a["action"] == "PLAN_PUBLISHED_FORCED" for a in rows)


def test_only_the_current_version_can_be_published(planner, whatif):
    sc, first = whatif
    r = planner.ok(planner.post("/planning/run", json={"scenario_id": sc["id"], "solver": {"provider": "heuristic", "time_limit_s": 5}}), 202)
    assert planner.wait_run(r["run_id"])["status"] == "SUCCEEDED"
    for body in ({}, {"force": True, "reason": "try anyway"}):
        r = planner.post(f"/plans/{first}/publish", json=body)
        assert r.status_code == 409 and r.json()["error"]["code"] == "NOT_CURRENT_VERSION", r.text


def test_plant_policy_requires_validation(client, planner, sevilla, whatif):
    from conftest import Api

    _sc, pid = whatif
    admin = Api(client, "admin")
    admin.ok(admin.put(f"/plants/{sevilla['id']}/settings", json={"publish_requires_validation": True}))
    r = planner.post(f"/plans/{pid}/publish", json={})
    assert "NOT_VALIDATED" in _blockers(r)
    bad = admin.put(f"/plants/{sevilla['id']}/settings", json={"publish_requires_validation": "yes"})
    assert bad.status_code == 422


def test_planning_past_critical_data_problems_needs_a_reason_and_is_audited(client):
    """Critical data problems block planning; planning anyway is explicit (force + reason), recorded on
    the run and in the audit log. A what-if never forces on the user's behalf."""
    from conftest import Api
    from sqlalchemy import select

    from monxuplan.models import Item, ProductionOrder

    admin = Api(client, "admin")
    qro = next(p for p in admin.ok(admin.get("/plants")) if p["code"] == "QRO")
    with _session(admin) as s:
        item = s.scalar(select(Item).where(Item.make_or_buy == "MAKE").order_by(Item.code))
        po = ProductionOrder(tenant_id=item.tenant_id, number=f"DQ-{uuid.uuid4().hex[:6]}", plant_id=uuid.UUID(qro["id"]), item_id=item.id, quantity=1, status="RELEASED", due_date=item.created_at + timedelta(days=3650))
        s.add(po)  # an open order without operations: ORDER_WITHOUT_OPERATIONS (critical)
        s.commit()
        po_id = po.id
    run_id = None
    try:
        body = {"scenario_id": qro["live_scenario_id"], "solver": {"provider": "heuristic", "time_limit_s": 2}}
        r = admin.post("/planning/run", json=body)
        assert r.status_code == 409 and r.json()["error"]["code"] == "DATA_QUALITY_BLOCK", r.text
        assert "ORDER_WITHOUT_OPERATIONS" in {i["code"] for i in r.json()["error"]["context"]["issues"]}
        r = admin.post("/planning/run", json={**body, "force": True})
        assert r.status_code == 422 and r.json()["error"]["code"] == "REASON_REQUIRED", r.text
        w = admin.ok(admin.post("/scenarios/what-if", json={"plant_id": qro["id"], "kind": "OVERTIME", "params": {}, "name": f"dq-{uuid.uuid4().hex[:6]}"}), 201)
        assert "run_id" not in w and w["run_blocked"]["code"] == "DATA_QUALITY_BLOCK", w
        admin.ok(admin.post(f"/scenarios/{w['id']}/archive"))
        run_id = admin.ok(admin.post("/planning/run", json={**body, "force": True, "force_reason": "order import fixed after the run"}), 202)["run_id"]
        run = admin.ok(admin.get(f"/planning/runs/{run_id}"))
        assert run["params"]["overridden_issues"] and run["params"]["force_reason"] == "order import fixed after the run"
        log = admin.ok(admin.get("/audit", params={"entity_id": qro["live_scenario_id"]}))
        rows = log["items"] if isinstance(log, dict) else log
        forced = [a for a in rows if a["action"] == "PLANNING_RUN_FORCED"]
        assert forced and forced[0]["reason"] == "order import fixed after the run"
    finally:
        if run_id:
            admin.post(f"/planning/runs/{run_id}/cancel")
            admin.wait_run(run_id)
        with _session(admin) as s:
            s.delete(s.get(ProductionOrder, po_id))
            s.commit()
