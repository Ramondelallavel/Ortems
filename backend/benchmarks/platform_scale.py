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
    with new_session(tid, "planner") as s:
        run = planning.enqueue_run(s, ctx, scid, "OPTIMIZE", {"solver": {"provider": "heuristic", "profile": "QUICK", "time_limit_s": time_limit}})
        s.commit()
        rid = run.id
    with timed("execute_run_total"):
        planning.execute_run(rid, tid)
    with new_session(tid, "planner") as s:
        run = s.get(M.PlanningRun, rid)
        print(json.dumps({"status": run.status, "error": run.error_message, "result": {k: v for k, v in (run.result or {}).items() if k != "kpis"}}, default=str)[:1500])
        return str(run.plan_id)


def views() -> None:
    from monxuplan import models as M
    from monxuplan.core.db import new_session
    from monxuplan.services import analytics, materials, orders, overview, views as V
    from monxuplan.services.context import system_ctx

    with new_session(None, "bench") as s:
        plant = s.scalar(select(M.Plant).where(M.Plant.code == "SCL"))
        tid, pid = plant.tenant_id, plant.id
        sc = s.get(M.Scenario, plant.live_scenario_id)
        plan_id = sc.head_plan_id
    ctx = system_ctx(tid, "planner")
    calls = [
        ("dashboard", lambda s: overview.command_center(s, ctx, pid)),
        ("plan_header", lambda s: V.plan_header(s, ctx, plan_id)),
        ("orders_page_1", lambda s: orders.list_orders(s, ctx, pid, None, {}, 0, 200)),
        ("orders_search", lambda s: orders.list_orders(s, ctx, pid, "SCL-00123", {}, 0, 200)),
        ("gantt_default", lambda s: V.gantt(s, ctx, plan_id)),
        ("capacity_day", lambda s: analytics.capacity(s, ctx, plan_id, "day")),
        ("materials", lambda s: materials.overview(s, ctx, plan_id) if hasattr(materials, "overview") else None),
    ]
    for name, fn in calls:
        with new_session(tid, "planner") as s:
            t = time.monotonic()
            try:
                out = fn(s)
                size = len(json.dumps(out, default=str)) if out is not None else 0
                TIMES[name] = round(time.monotonic() - t, 2)
                TIMES[name + "_kb"] = round(size / 1024)
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
