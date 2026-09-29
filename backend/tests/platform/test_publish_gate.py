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
