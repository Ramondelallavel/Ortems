"""Stored plan results and read models (plan_order, plan_peg, plan_unscheduled, plan_document).

The read models written with a plan version must say exactly what an engine replay of the stored
plan says — they replace that replay on every screen — and the paged / windowed endpoints built on
them must return consistent slices."""

import json
import uuid
from datetime import UTC, datetime, timedelta


def _session(planner):
    from monxuplan.core.db import new_session

    tenant = planner.ok(planner.get("/auth/me"))["tenant_id"]
    return new_session(uuid.UUID(tenant), "tests")


def test_results_are_stored_in_tables(planner, base_plan):
    pid = base_plan["id"]
    hdr = planner.ok(planner.get(f"/plans/{pid}"))
    assert hdr["operations"] > 0
    orders = planner.ok(planner.get(f"/plans/{pid}/orders", params={"limit": 50000}))
    assert orders and {"order_id", "number", "status", "lateness_minutes", "material_status"} <= set(orders[0])
    lateness = [o["lateness_minutes"] for o in orders]
    assert lateness == sorted(lateness, reverse=True)
    by_due = planner.ok(planner.get(f"/plans/{pid}/orders", params={"limit": 50000, "sort": "due"}))
    assert sorted(o["order_id"] for o in by_due) == sorted(o["order_id"] for o in orders)
    late = planner.ok(planner.get(f"/plans/{pid}/orders", params={"status": "LATE,UNSCHEDULED"}))
    assert all(o["status"] in ("LATE", "UNSCHEDULED") for o in late)
    unscheduled = planner.ok(planner.get(f"/plans/{pid}/unscheduled"))
    assert hdr["unscheduled_count"] == len(unscheduled)
    with _session(planner) as s:
        from monxuplan.models import Plan
        from monxuplan.services import plan_store

        plan = s.get(Plan, uuid.UUID(pid))
        an = plan.analysis
        assert an["storage"] == plan_store.STORAGE_VERSION and "orders" not in an and "pegging" not in an
        assert an["counts"]["orders"] == len(orders)
        assert plan_store.has_documents(s, plan, plan_store.DOC_EXPLANATIONS)


def test_order_detail_and_explanation_from_stored_results(planner, base_plan):
    pid = base_plan["id"]
    op = planner.ok(planner.get(f"/plans/{pid}/schedule", params={"limit": 1}))[0]
    d = planner.ok(planner.get(f"/plans/{pid}/operations/{op['op_key']}"))
    assert d["explanation"]["op_id"] == op["op_key"] and d["explanation"]["reasons"]
    detail = planner.ok(planner.get(f"/plans/{pid}/order-detail/{op['order_key']}"))
    assert detail["result"]["order_id"] == op["order_key"]
    assert any(o["op_key"] == op["op_key"] for o in detail["operations"])
    chain = planner.ok(planner.get(f"/plans/{pid}/order-chain/{op['order_key']}"))
    assert op["op_key"] in chain["operations"]


def test_read_models_match_the_engine_replay(planner, base_plan):
    """Capacity profiles, resource calendars, material projections and order chains stored with the
    plan are identical to the same computations on a replay of the stored plan."""
    from monxuplan.models import Plan
    from monxuplan.services import engine_view, plan_store
    from monxuplan_engine import views as ev

    def same(a, b):
        return json.loads(json.dumps(a, default=str)) == json.loads(json.dumps(b, default=str))

    with _session(planner) as s:
        plan = s.get(Plan, uuid.UUID(base_plan["id"]))
        cp, res = engine_view.replay(s, plan)
        for size in ("hour", "shift", "day", "week"):
            assert same(plan_store.document(s, plan, plan_store.DOC_CAPACITY, size), ev.capacity_view(res, size)), size
        cals = plan_store.documents(s, plan, plan_store.DOC_CALENDAR)
        assert cals and all(same(doc, ev.resource_calendar(res, cp.res_index[rid])) for rid, doc in cals.items())
        mats = plan_store.documents(s, plan, plan_store.DOC_MATERIAL)
        assert mats and all(same(doc, ev.material_projection(res, cp.mat_index[mid])) for mid, doc in mats.items())
        for o in cp.orders[:25]:
            shard = plan_store.document(s, plan, plan_store.DOC_CHAINS, ev.chain_shard(o.id))
            got, want = shard[o.id], ev.order_chain(res, o.idx)
            assert sorted(got["operations"]) == sorted(want["operations"])
            assert sorted(json.dumps(d, sort_keys=True) for d in got["dependencies"]) == sorted(json.dumps(d, sort_keys=True) for d in want["dependencies"])


def test_capacity_paging_search_and_sort(planner, base_plan):
    pid = base_plan["id"]
    full = planner.ok(planner.get("/capacity/load", params={"plan_id": pid, "bucket": "day"}))
    n = full["rows_total"]
    assert n == len(full["rows"]) and n > 3
    page = planner.ok(planner.get("/capacity/load", params={"plan_id": pid, "bucket": "day", "offset": 1, "limit": 2}))
    assert page["rows_total"] == n and [r["id"] for r in page["rows"]] == [r["id"] for r in full["rows"][1:3]]
    code = full["rows"][0]["code"]
    found = planner.ok(planner.get("/capacity/load", params={"plan_id": pid, "bucket": "day", "q": code}))
    assert any(r["code"] == code for r in found["rows"])
    loaded = planner.ok(planner.get("/capacity/load", params={"plan_id": pid, "bucket": "day", "sort": "load"}))

    def peak(r):
        return max((max(b["scheduled"], b["requirement"]) / b["capacity"]) if b["capacity"] else 0 for b in r["buckets"])

    peaks = [peak(r) for r in loaded["rows"] if all(b["capacity"] or not (b["scheduled"] or b["requirement"]) for b in r["buckets"])]
    assert peaks == sorted(peaks, reverse=True)


def test_gantt_windows_blocks_and_find(planner, base_plan):
    pid = base_plan["id"]
    full = planner.ok(planner.get(f"/plans/{pid}/gantt"))
    ops = full["operations"]
    assert ops and full["resources"]
    rid = ops[0]["resource_id"]
    rows_only = planner.ok(planner.get(f"/plans/{pid}/gantt", params={"operations": "false"}))
    assert rows_only["operations"] == [] and len(rows_only["resources"]) == len(full["resources"])
    lane = planner.ok(planner.get(f"/plans/{pid}/gantt", params={"resources": "false", "resource_ids": [rid], "start": full["window"]["start"], "end": full["window"]["end"]}))
    assert lane["resources"] == [] and {o["resource_id"] for o in lane["operations"]} == {rid}
    assert len(lane["operations"]) == sum(1 for o in ops if o["resource_id"] == rid)
    blocks = planner.ok(planner.get(f"/plans/{pid}/gantt/blocks", params={"resource_ids": [rid], "start": full["window"]["start"], "end": full["window"]["end"], "resolution_minutes": 1440}))
    lanes = blocks["lanes"][rid]
    assert sum(b[2] for b in lanes) == len(lane["operations"])
    starts = [datetime.fromisoformat(b[0]) for b in lanes]
    assert starts == sorted(starts)
    for a, b in zip(lanes, lanes[1:], strict=False):  # blocks are separated by more than the resolution
        assert datetime.fromisoformat(b[0]) - datetime.fromisoformat(a[1]) > timedelta(minutes=1440)
    hit = planner.ok(planner.get(f"/plans/{pid}/operations/find", params={"q": ops[0]["id"]}))
    assert hit and hit[0]["op_id"] == ops[0]["id"] and hit[0]["resource_id"] == rid


def test_order_book_sorted_and_filtered_by_plan_result(planner, sevilla, base_plan):
    q = {"plant_id": sevilla["id"], "limit": 5000}
    by_lateness = planner.ok(planner.get("/orders", params={**q, "sort": "lateness_minutes", "dir": "desc"}))
    lat = [o["lateness_minutes"] or 0 for o in by_lateness["items"] if o["lateness_minutes"] is not None]
    assert lat == sorted(lat, reverse=True)
    late = planner.ok(planner.get("/orders", params={**q, "plan_status": "LATE"}))
    assert all(o["plan_status"] == "LATE" for o in late["items"])
    assert late["total"] == len(late["items"])
    numbers = planner.ok(planner.get("/orders", params={**q, "sort": "number"}))
    nums = [o["number"] for o in numbers["items"]]
    assert nums == sorted(nums)


def test_move_session_matches_a_fresh_load(planner, sevilla, base_plan):
    """Applying a move derives the next version's move base (problem, compiled problem, baseline)
    from the new schedule instead of reading and compiling the stored version: it must be the same
    as a fresh load, and a following move must give the same plan either way. The re-placed
    operations get new explanations, the others keep the ones of the previous version."""
    from monxuplan.models import Plan, Scenario
    from monxuplan.services import plan_store, planning
    from monxuplan_engine.repair import move as engine_move

    sid = sevilla["live_scenario_id"]
    with _session(planner) as s:
        head = s.get(Scenario, uuid.UUID(sid)).head_plan_id
    g = planner.ok(planner.get(f"/plans/{head}/gantt"))
    movable = [o for o in g["operations"] if not o["fixed"] and not o["locked"] and o["zone"] != "FROZEN"]
    op = movable[len(movable) // 3]
    start = (datetime.fromisoformat(op["start"]) + timedelta(hours=1)).isoformat()
    body = {"op_id": op["id"], "resource_id": op["resource_id"], "start": start, "replan": "DOWNSTREAM", "reason": "test", "accept_violations": True}
    new = planner.ok(planner.post(f"/plans/{head}/moves", json=body))
    try:
        with _session(planner) as s:
            plan = s.get(Plan, uuid.UUID(new["id"]))
            derived = planning._MOVE_BASES.get(planning._base_key(plan))
            assert derived is not None
            planning.forget_move_base(plan.id)
            fresh = planning.move_base(s, plan)
            assert derived.cp.baseline == fresh.cp.baseline
            assert [o.fixed for o in derived.cp.ops] == [o.fixed for o in fresh.cp.ops]
            assert derived.cp.lo == fresh.cp.lo
            fixed = {o.id: o.fixed for o in fresh.problem.operations if o.fixed is not None}
            assert {o.id: o.fixed for o in derived.problem.operations if o.fixed is not None} == fixed
            # a second move from either base
            other = next(o for o in movable if o["id"] != op["id"] and o["resource_id"] != op["resource_id"])
            t2 = datetime.fromisoformat(other["start"]) + timedelta(minutes=30)

            def planned(base):
                out = engine_move(base.problem, other["id"], other["resource_id"], t2, cp=base.cp, baseline_solution=base.baseline, explain="NONE")
                return sorted((x.op_id, x.resource_id, x.setup_start, x.end) for x in out.solution.schedule), out.comparison

            assert planned(derived) == planned(fresh)
            # explanations: every scheduled operation has one; kept operations keep the parent's
            parent = s.get(Plan, head)
            docs = plan_store.documents(s, plan, plan_store.DOC_EXPLANATIONS)
            before = plan_store.documents(s, parent, plan_store.DOC_EXPLANATIONS)
            SO = plan_store.ScheduledOperation
            from sqlalchemy import select

            rows = s.execute(select(SO.op_key, SO.resource_key, SO.fixed_reason, SO.is_locked).where(SO.plan_id == plan.id)).all()
            assert all(r.op_key in docs.get(r.resource_key, {}) for r in rows)
            prev = {k: v for d in before.values() for k, v in d.items()}
            assert not any(r.fixed_reason == "KEPT" for r in rows)  # left in place is not "fixed"
            inherited = sum(1 for r in rows if docs[r.resource_key][r.op_key] == prev.get(r.op_key))
            assert inherited > len(rows) // 2
            moved = docs[op["resource_id"]][op["id"]]
            assert moved["op_id"] == op["id"]
            assert [r.op_key for r in rows if r.is_locked] == [op["id"]] or op["id"] in {r.op_key for r in rows if r.is_locked}
            assert plan.snapshot_id == parent.snapshot_id
    finally:
        back = planner.ok(planner.post(f"/scenarios/{sid}/undo"))
        assert back["id"] == str(head)


def test_incremental_storage_matches_a_full_write(planner, sevilla, base_plan):
    """A moved plan stored by copying the parent's unchanged rows holds the same rows and read models
    as the same solution written in full (except the binding constraint and material-ready time of
    unchanged operations and the limiting constraint and cause of unchanged orders, kept from the
    version where they were decided)."""
    from sqlalchemy import func, select

    from monxuplan.models import Plan, PlanDocument, PlanOrder, PlanPeg, Scenario, ScheduledOperation
    from monxuplan.services import planning
    from monxuplan.services.context import system_ctx

    with _session(planner) as s:
        sc = s.get(Scenario, uuid.UUID(sevilla["live_scenario_id"]))
        plan = s.get(Plan, sc.head_plan_id)
        ctx = system_ctx(plan.tenant_id, "planner")
        rows = s.execute(select(ScheduledOperation.op_key, ScheduledOperation.resource_key, ScheduledOperation.start).where(ScheduledOperation.plan_id == plan.id, ScheduledOperation.is_locked.is_(False)).order_by(ScheduledOperation.setup_start)).all()
        op = rows[len(rows) // 2]
        start = (op.start if op.start.tzinfo else op.start.replace(tzinfo=UTC)) + timedelta(hours=2)
        out = planning._move(s, ctx, plan, op.op_key, op.resource_key, start, "DOWNSTREAM", False, explain="CHANGED")
        sol, base = out["_solution"], out["_base"]
        info = planning._info_from_plan(s, plan)
        reuse = planning._reuse_for(s, plan, sol, out["_replaced"] | {op.op_key})
        assert reuse is not None and op.op_key in reuse.ops and len(reuse.ops) < len(sol.schedule)
        kw = {"kind": "MANUAL_EDIT", "parent": plan, "explanations_from": plan, "snapshot_id": base.snapshot_id}
        a = planning.persist_solution(s, ctx, sc, base.problem, sol, info, reuse=reuse, **kw)
        b = planning.persist_solution(s, ctx, sc, base.problem, sol, info, **kw)
        s.flush()

        def table(model, key, skip):
            cols = [c for c in model.__table__.columns if c.name not in {"id", "plan_id", *skip}]
            out = {}
            for pid in (a.id, b.id):
                out[pid] = {tuple(getattr(r, k) for k in key): tuple(json.dumps(v, default=str, sort_keys=True) for v in r) for r in s.execute(select(*cols).where(model.plan_id == pid))}
            return out[a.id], out[b.id]

        x, y = table(ScheduledOperation, ("op_key",), ("binding", "material_ready"))
        assert x == y
        x, y = table(PlanOrder, ("order_key",), ("limiting", "cause"))
        assert x == y

        def pegged(pid):
            tot = {}
            for r in s.execute(select(PlanPeg.material_id, PlanPeg.consumer_op_id, PlanPeg.quantity).where(PlanPeg.plan_id == pid)):
                tot[(r.material_id, r.consumer_op_id)] = round(tot.get((r.material_id, r.consumer_op_id), 0) + float(r.quantity), 6)
            return tot

        assert pegged(a.id) == pegged(b.id)
        from monxuplan.services import plan_store

        for kind in (plan_store.DOC_CAPACITY, plan_store.DOC_CALENDAR, plan_store.DOC_MATERIAL, plan_store.DOC_CHAINS, plan_store.DOC_EXPLANATIONS):
            da, db = plan_store.documents(s, a, kind), plan_store.documents(s, b, kind)
            assert da.keys() == db.keys(), kind
            assert json.dumps(da, sort_keys=True, default=str) == json.dumps(db, sort_keys=True, default=str), kind
        assert s.scalar(select(func.count()).select_from(PlanDocument).where(PlanDocument.plan_id == a.id)) == s.scalar(select(func.count()).select_from(PlanDocument).where(PlanDocument.plan_id == b.id))
        s.rollback()
