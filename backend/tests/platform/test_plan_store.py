"""Stored plan results and read models (plan_order, plan_peg, plan_unscheduled, plan_document).

The read models written with a plan version must say exactly what an engine replay of the stored
plan says — they replace that replay on every screen — and the paged / windowed endpoints built on
them must return consistent slices."""

import json
import uuid
from datetime import datetime, timedelta


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
