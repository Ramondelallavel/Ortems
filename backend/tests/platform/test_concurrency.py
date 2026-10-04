"""Concurrency: one active run per scenario (database-enforced), stale results never promoted, only
the owner of a run promotes it, input revision counts exactly the planning inputs."""

import threading
import uuid

import pytest


def _session(api):
    from monxuplan.core.db import new_session

    return new_session(uuid.UUID(api.ok(api.get("/auth/me"))["tenant_id"]), "tests")


@pytest.fixture()
def scenario(planner, sevilla, base_plan):
    sc = planner.ok(planner.post(f"/scenarios/{sevilla['live_scenario_id']}/clone", json={"name": f"cc-{uuid.uuid4().hex[:6]}"}), 201)
    r = planner.ok(planner.post("/planning/run", json={"scenario_id": sc["id"], "solver": {"provider": "heuristic", "time_limit_s": 5}}), 202)
    assert planner.wait_run(r["run_id"])["status"] == "SUCCEEDED"
    return planner.ok(planner.get(f"/scenarios/{sc['id']}"))


def test_database_allows_one_active_run_per_scenario(planner, scenario):
    from sqlalchemy.exc import IntegrityError

    from monxuplan.models import PlanningRun

    with _session(planner) as s:
        sid = uuid.UUID(scenario["id"])
        s.add(PlanningRun(scenario_id=sid, kind="OPTIMIZE", status="CANCELLED"))
        s.add(PlanningRun(scenario_id=sid, kind="OPTIMIZE", status="QUEUED", worker="hold"))
        s.flush()
        s.add(PlanningRun(scenario_id=sid, kind="OPTIMIZE", status="RUNNING", worker="hold"))
        with pytest.raises(IntegrityError):
            s.flush()
        s.rollback()


def test_concurrent_requests_queue_one_run(client, scenario):
    from conftest import Api

    apis = [Api(client, "planner") for _ in range(4)]
    codes: list[int] = []
    barrier = threading.Barrier(len(apis))

    def go(a):
        barrier.wait()
        codes.append(a.post("/planning/run", json={"scenario_id": scenario["id"], "solver": {"provider": "heuristic", "time_limit_s": 5}}).status_code)

    ts = [threading.Thread(target=go, args=(a,)) for a in apis]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert sorted(codes) == [202, 409, 409, 409], codes
    runs = apis[0].ok(apis[0].get("/planning/runs", params={"scenario_id": scenario["id"]}))
    for r in runs:
        if r["status"] in ("QUEUED", "RUNNING"):
            apis[0].wait_run(r["id"])


def _run_with(planner, scenario, monkeypatch, during):
    """Run the optimiser; ``during(session)`` is executed (and committed) while it computes."""
    from monxuplan.core.db import new_session
    from monxuplan.services import planning

    real = planning.solve
    tid = uuid.UUID(planner.ok(planner.get("/auth/me"))["tenant_id"])

    def solve(problem, **kw):
        out = real(problem, **kw)
        with new_session(tid, "someone-else") as s:
            during(s)
            s.commit()
        return out

    monkeypatch.setattr(planning, "solve", solve)
    r = planner.ok(planner.post("/planning/run", json={"scenario_id": scenario["id"], "solver": {"provider": "heuristic", "time_limit_s": 5}}), 202)
    return planner.wait_run(r["run_id"])


def test_result_is_stale_when_master_data_changes_during_the_run(planner, scenario, monkeypatch):
    from sqlalchemy import select

    from monxuplan.models import Item

    head_before = scenario["head_plan_id"]

    def edit(s):
        it = s.scalar(select(Item).order_by(Item.code).limit(1))
        it.safety_stock = (it.safety_stock or 0) + 1

    run = _run_with(planner, scenario, monkeypatch, edit)
    assert run["status"] == "STALE", run
    assert run["result"]["stale"] and "Planning data changed" in run["result"]["stale"]
    plan = planner.ok(planner.get(f"/plans/{run['plan_id']}"))
    assert plan["status"] == "STALE"
    assert planner.ok(planner.get(f"/scenarios/{scenario['id']}"))["head_plan_id"] == head_before
    # a stale version can never be published
    r = planner.post(f"/plans/{run['plan_id']}/publish", json={"force": True, "reason": "try"})
    assert r.status_code == 409 and r.json()["error"]["code"] == "NOT_CURRENT_VERSION"


def test_result_is_stale_when_the_head_moves_during_the_run(planner, scenario, monkeypatch):
    from monxuplan.models import Scenario

    sid = uuid.UUID(scenario["id"])

    def move_head(s):
        s.get(Scenario, sid).head_plan_id = None  # e.g. another user's undo / restore

    run = _run_with(planner, scenario, monkeypatch, move_head)
    assert run["status"] == "STALE" and "current plan changed" in run["result"]["stale"]
    assert planner.ok(planner.get(f"/scenarios/{scenario['id']}"))["head_plan_id"] is None


def test_only_the_owner_promotes(planner, scenario):
    from monxuplan.models import PlanningRun, Scenario
    from monxuplan.services.planning import _promotion_check

    with _session(planner) as s:
        sc = s.get(Scenario, uuid.UUID(scenario["id"]))
        run = PlanningRun(scenario_id=sc.id, kind="OPTIMIZE", status="RUNNING", worker="worker-B", baseline_plan_id=sc.head_plan_id)
        s.add(run)
        s.flush()
        from monxuplan.models import revision

        run.input_revision = revision.current(s, sc.tenant_id)
        assert _promotion_check(s, run, sc, "worker-A", sc.tenant_id) == "NOT_OWNER"
        assert _promotion_check(s, run, sc, "worker-B", sc.tenant_id) is None
        s.rollback()


def test_input_revision_counts_inputs_only(planner, scenario):
    from sqlalchemy import select

    from monxuplan.models import Item, Plan, revision

    with _session(planner) as s:
        tid = s.scalar(select(Item.tenant_id).limit(1))
        r0 = revision.current(s, tid)
        p = s.get(Plan, uuid.UUID(scenario["head_plan_id"]))
        p.note = "just a note"
        s.flush()
        assert revision.current(s, tid) == r0  # plan versions are outputs
        it = s.scalar(select(Item).limit(1))
        it.name = it.name + " "
        s.flush()
        assert revision.current(s, tid) == r0 + 1
        s.rollback()


def test_worker_shutdown_hands_its_runs_back(planner, scenario):
    """A worker stopped mid-run (SIGTERM) re-queues its own runs at once; other workers' runs stay."""
    from sqlalchemy import update

    from monxuplan import worker
    from monxuplan.models import PlanningRun

    with _session(planner) as s:
        mine = PlanningRun(scenario_id=uuid.UUID(scenario["id"]), kind="OPTIMIZE", status="RUNNING", worker="shutdown-test:1", params={"solver": {"provider": "heuristic", "time_limit_s": 2}})
        s.add(mine)
        s.commit()
        rid = mine.id
    assert worker.requeue_own("other-worker:9") == 0
    assert worker.requeue_own("shutdown-test:1") == 1
    with _session(planner) as s:
        r = s.get(PlanningRun, rid)
        # back in the queue (or already taken over by a running worker): no longer owned by the stopped one
        assert r.worker != "shutdown-test:1" and r.status in ("QUEUED", "RUNNING", "SUCCEEDED", "STALE")
        s.execute(update(PlanningRun).where(PlanningRun.id == rid, PlanningRun.status == "QUEUED").values(status="CANCELLED"))
        s.commit()
    planner.wait_run(str(rid))
