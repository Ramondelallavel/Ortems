"""Helpers to build small engine problems in tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from monxuplan_engine.contract import Problem

T0 = datetime(2026, 9, 28, 6, 0, tzinfo=UTC)  # Monday


def at(hours: float = 0, days: float = 0) -> str:
    return (T0 + timedelta(hours=hours, days=days)).isoformat()


def two_shift_calendar(cid: str = "CAL-2S", tz: str = "UTC", weekdays=range(5)) -> dict[str, Any]:
    return {
        "id": cid,
        "timezone": tz,
        "shifts": [{"weekday": d, "start": "06:00", "end": "22:00"} for d in weekdays],
    }


def machine(rid: str, cal: str | None = None, **kw) -> dict[str, Any]:
    d = {"id": rid, "code": rid, "kind": "MACHINE", "calendar_id": cal}
    d.update(kw)
    return d


def order(oid: str, due_h: float, item: str = "A", qty: float = 1, **kw) -> dict[str, Any]:
    d = {"id": oid, "number": oid, "item_id": item, "item_code": item, "quantity": qty, "due": at(due_h)}
    d.update(kw)
    return d


def operation(oid: str, order_id: str, seq: int, resources: list[str], run: float, setup: float = 0, qty: float = 1, **kw) -> dict[str, Any]:
    modes = [{"resource_id": r, "preference": k} for k, r in enumerate(resources)]
    d = {
        "id": oid,
        "order_id": order_id,
        "seq": seq,
        "quantity": qty,
        "duration": {"setup_minutes": setup, "run_minutes_per_unit": run / qty},
        "modes": modes,
    }
    d.update(kw)
    return d


def problem(**kw) -> Problem:
    data: dict[str, Any] = {
        "horizon": {"start": T0.isoformat(), "end": (T0 + timedelta(days=14)).isoformat(), "timezone": "UTC", "overflow_days": 30},
        "solver": {"provider": "heuristic", "time_limit_s": 5, "local_search": False, "multi_start": False},
    }
    for k, v in kw.items():
        if k in data and isinstance(data[k], dict) and isinstance(v, dict):
            data[k] = {**data[k], **v}
        else:
            data[k] = v
    return Problem.model_validate(data)
