"""Platform timings on the Scale Plant (``python -m monxuplan.seed.scale``): a whole planning run
through the worker path and the main screens' API calls.

    MONXU_DATABASE_URL=postgresql+psycopg://… python benchmarks/platform_scale.py [--run] [--views]
"""

from __future__ import annotations

import argparse
import json
import os
import resource
import sys
import time
from contextlib import contextmanager

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from sqlalchemy import select  # noqa: E402

TIMES: dict[str, float] = {}


@contextmanager
def timed(name: str):
    t = time.monotonic()
    try:
        yield
    finally:
        TIMES[name] = round(TIMES.get(name, 0.0) + time.monotonic() - t, 2)


def _wrap(module, name: str, label: str) -> None:
    fn = getattr(module, name)

    def inner(*a, **k):
        with timed(label):
            return fn(*a, **k)

    setattr(module, name, inner)


def run_plan(time_limit: float) -> str:
    from monxuplan import models as M
    from monxuplan.core.db import new_session
    from monxuplan.services import alerts, planning
    from monxuplan.services.context import system_ctx

    _wrap(planning, "build_problem", "build_problem")
    _wrap(planning, "solve", "solve")
    _wrap(planning, "persist_solution", "persist_solution")
    _wrap(planning, "solution_from_plan", "load_previous_plan")
    _wrap(planning, "compare_solutions", "compare_with_previous")
    _wrap(alerts, "generate_alerts", "generate_alerts")
    with new_session(None, "bench") as s:
        plant = s.scalar(select(M.Plant).where(M.Plant.code == "SCL"))
        tid, scid = plant.tenant_id, plant.live_scenario_id
    ctx = system_ctx(tid, "planner")
    from monxuplan.core.errors import Conflict

    with new_session(tid, "planner") as s:
        try:
            run = planning.enqueue_run(s, ctx, scid, "OPTIMIZE", {"solver": {"provider": "heuristic", "profile": "QUICK", "time_limit_s": time_limit}})
        except Conflict as exc:
            raise SystemExit(f"cannot start the benchmark: {exc.message} (wait for it or cancel it first)") from None
        s.commit()
        rid = run.id
    from monxuplan.worker import claim_next

    with timed("execute_run_total"):
        job = claim_next(rid)  # the same claim as the worker, for this run (a run is only promoted by its owner)
        if job is None:
            with new_session(tid, "planner") as s:  # do not leave the benchmark's run queued behind
                planning.cancel_run(s, ctx, rid)
                s.commit()
            raise SystemExit("the benchmark run was claimed by a running worker: stop the workers of this database first")
        planning.execute_run(*job)
    with new_session(tid, "planner") as s:
        run = s.get(M.PlanningRun, rid)
        print(json.dumps({"status": run.status, "error": run.error_message, "result": {k: v for k, v in (run.result or {}).items() if k != "kpis"}}, default=str)[:1500])
        return str(run.plan_id)


def views() -> None:
    """The calls the screens make, timed as the API serves them: service call, then JSON encoding
    (``pydantic_core.to_json``, as the API's fast responses do)."""
    from datetime import timedelta

    from pydantic_core import to_json

    from monxuplan import models as M
    from monxuplan.core.db import new_session
    from monxuplan.services import analytics, materials, orders, overview, plan_store
    from monxuplan.services import views as V
    from monxuplan.services.context import system_ctx

    with new_session(None, "bench") as s:
        plant = s.scalar(select(M.Plant).where(M.Plant.code == "SCL"))
        tid, pid = plant.tenant_id, plant.id
        sc = s.get(M.Scenario, plant.live_scenario_id)
        plan_id = sc.head_plan_id
        plan = s.get(M.Plan, plan_id)
        hs, he = V._aware(plan.horizon_start), V._aware(plan.horizon_end)
        some = s.execute(select(M.ScheduledOperation.op_key, M.ScheduledOperation.order_key, M.ScheduledOperation.resource_key).where(M.ScheduledOperation.plan_id == plan_id).limit(1)).one()
        lanes = [r for (r,) in s.execute(select(M.ScheduledOperation.resource_key).where(M.ScheduledOperation.plan_id == plan_id).distinct().order_by(M.ScheduledOperation.resource_key).limit(50))]
        material = s.scalar(select(M.PlanPeg.material_id).where(M.PlanPeg.plan_id == plan_id).limit(1))
        number = s.scalar(select(M.ProductionOrder.number).where(M.ProductionOrder.plant_id == pid).order_by(M.ProductionOrder.number).offset(12_345).limit(1))
    ctx = system_ctx(tid, "planner")
    t0 = hs + timedelta(hours=8)
    calls = [
        ("dashboard", lambda s: overview.command_center(s, ctx, pid)),
        ("plan_header", lambda s: V.plan_header(s, ctx, plan_id)),
        ("orders_page_1", lambda s: orders.list_orders(s, ctx, pid, None, {}, 0, 200)),
        ("orders_page_last", lambda s: orders.list_orders(s, ctx, pid, None, {}, 99_800, 200)),
        ("orders_search", lambda s: orders.list_orders(s, ctx, pid, number, {}, 0, 200)),
        ("orders_late_by_lateness", lambda s: orders.list_orders(s, ctx, pid, None, {"plan_status": "LATE", "sort": "lateness_minutes", "dir": "desc"}, 0, 200)),
        ("plan_orders_late_page", lambda s: plan_store.order_page(s, s.get(M.Plan, plan_id), ["LATE"], 0, 200)),
        ("unscheduled_page", lambda s: plan_store.unscheduled(s, s.get(M.Plan, plan_id), offset=0, limit=200)),
        # Gantt as the planning board loads it: rows once, then the visible rows and time span
        ("gantt_rows", lambda s: V.gantt(s, ctx, plan_id, hs - timedelta(days=1), he + timedelta(days=2), include_operations=False)),
        ("gantt_viewport_ops_50_rows_8h", lambda s: V.gantt(s, ctx, plan_id, t0 - timedelta(hours=4), t0 + timedelta(hours=12), lanes, include_resources=False)),
        ("gantt_viewport_blocks_50_rows_week", lambda s: V.gantt_blocks(s, ctx, plan_id, t0 - timedelta(days=3.5), t0 + timedelta(days=10.5), lanes, 29)),
        ("gantt_find", lambda s: V.find_operations(s, ctx, plan_id, number, 20)),
        ("gantt_default_window", lambda s: V.gantt(s, ctx, plan_id)),
        ("operation_detail", lambda s: V.operation_detail(s, ctx, plan_id, some.op_key)),
        ("order_detail", lambda s: V.order_detail(s, ctx, plan_id, some.order_key)),
        ("order_chain", lambda s: V.order_chain(s, ctx, plan_id, some.order_key)),
        ("capacity_day", lambda s: analytics.capacity(s, ctx, plan_id, "day")),
        ("capacity_hour_page", lambda s: analytics.capacity(s, ctx, plan_id, "hour", limit=100)),
        ("materials_availability", lambda s: materials.availability(s, ctx, plan_id)),
        ("material_projection", lambda s: materials.projection(s, ctx, plan_id, material) if material else None),
        ("dispatch_one_machine", lambda s: V.dispatch_list(s, ctx, pid, some.resource_key)),
        ("supervisor", lambda s: V.supervisor_view(s, ctx, pid)),
    ]
    for name, fn in calls:
        with new_session(tid, "planner") as s:
            t = time.monotonic()
            try:
                out = fn(s)
                t1 = time.monotonic()
                body = to_json(out, fallback=str) if out is not None else b""
                TIMES[name] = round(time.monotonic() - t, 2)
                TIMES[name + "_json_s"] = round(time.monotonic() - t1, 2)
                TIMES[name + "_kb"] = round(len(body) / 1024)
            except Exception as exc:  # noqa: BLE001
                TIMES[name] = f"ERROR {exc.__class__.__name__}: {str(exc)[:120]}"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", action="store_true")
    ap.add_argument("--views", action="store_true")
    ap.add_argument("--time-limit", type=float, default=120)
    args = ap.parse_args()
    if args.run:
        run_plan(args.time_limit)
    if args.views:
        views()
    TIMES["peak_rss_mb"] = round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024)
    print(json.dumps(TIMES))


if __name__ == "__main__":
    main()
