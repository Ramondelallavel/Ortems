"""Planning job worker.

Run as a separate process (``python -m monxuplan.worker``) or as threads inside the API process
(``MONXU_WORKER_IN_PROCESS=1``, default for development). Jobs are rows of ``planning_run`` claimed
atomically (``FOR UPDATE SKIP LOCKED`` on PostgreSQL), so any number of workers can run in parallel.
Runs whose worker died (no heartbeat for 10 minutes) are re-queued.
"""

from __future__ import annotations

import logging
import os
import socket
import threading
import time
from datetime import timedelta

from sqlalchemy import select, update

from .core.clock import now
from .core.config import get_settings
from .core.db import new_session
from .models import PlanningRun

log = logging.getLogger("monxuplan.worker")
WORKER_ID = f"{socket.gethostname()}:{os.getpid()}"
_stop = threading.Event()
_started = False
_claim_lock = threading.Lock()


def claim_next() -> tuple | None:
    """Atomically claim the oldest queued run → (run_id, tenant_id)."""
    st = get_settings()
    with _claim_lock, new_session(None, "worker") as s:
        q = select(PlanningRun).where(PlanningRun.status == "QUEUED").order_by(PlanningRun.created_at).limit(1)
        if not st.is_sqlite:
            q = q.with_for_update(skip_locked=True)
        run = s.scalar(q)
        if run is None:
            return None
        res = s.execute(
            update(PlanningRun)
            .where(PlanningRun.id == run.id, PlanningRun.status == "QUEUED")
            .values(status="RUNNING", started_at=now(), heartbeat_at=now(), worker=WORKER_ID)
        )
        s.commit()
        if res.rowcount != 1:
            return None
        return run.id, run.tenant_id


def requeue_stale(minutes: int = 10) -> int:
    with new_session(None, "worker") as s:
        limit = now() - timedelta(minutes=minutes)
        res = s.execute(update(PlanningRun).where(PlanningRun.status == "RUNNING", PlanningRun.heartbeat_at < limit).values(status="QUEUED", worker=None))
        s.commit()
        return res.rowcount or 0


def loop(poll_s: float = 1.0) -> None:
    from .services.planning import execute_run

    last_stale = 0.0
    while not _stop.is_set():
        try:
            if time.monotonic() - last_stale > 60:
                n = requeue_stale()
                if n:
                    log.warning("re-queued %d stale planning runs", n)
                last_stale = time.monotonic()
            job = claim_next()
            if job is None:
                _stop.wait(poll_s)
                continue
            run_id, tenant_id = job
            log.info("claimed planning run", extra={"planning_run": str(run_id)})
            execute_run(run_id, tenant_id)
        except Exception:  # noqa: BLE001 - never let the worker die
            log.exception("worker loop error")
            _stop.wait(2)


def start_in_process(threads: int = 2) -> None:
    global _started
    if _started:
        return
    _started = True
    for k in range(max(1, threads)):
        threading.Thread(target=loop, daemon=True, name=f"monxu-worker-{k}").start()


def stop() -> None:
    _stop.set()


def main() -> None:  # pragma: no cover
    from .core.observability import configure_logging

    configure_logging()
    log.info("worker started", extra={"extra_data": {"worker": WORKER_ID}})
    try:
        loop()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":  # pragma: no cover
    main()
