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
    last_hooks = 0.0
    while not _stop.is_set():
        try:
            if time.monotonic() - last_hooks > 10:
                last_hooks = time.monotonic()
                try:
                    from .services.webhooks import auto_delivery_enabled, deliver_pending

                    if auto_delivery_enabled():
                        deliver_pending()  # claimed with a lease: safe with any number of workers and API replicas
                except Exception:  # noqa: BLE001 - deliveries never stop planning
                    log.exception("webhook delivery failed")
            if time.monotonic() - last_stale > 60:
                n = requeue_stale()
                if n:
                    log.warning("re-queued %d stale planning runs", n)
                last_stale = time.monotonic()
                try:
                    from .services.dbconnect import run_due

                    run_due()
                except Exception:  # noqa: BLE001 - scheduled syncs never stop planning
                    log.exception("scheduled database sync failed")
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


def requeue_own(worker: str = WORKER_ID) -> int:
    """Puts the runs this worker is executing back in the queue (shutdown): another worker takes them
    over at once instead of after the 10-minute heartbeat timeout. A result this worker had not
    promoted yet is discarded with its transaction; promotion checks the owning worker."""
    with new_session(None, "worker") as s:
        res = s.execute(update(PlanningRun).where(PlanningRun.status == "RUNNING", PlanningRun.worker == worker).values(status="QUEUED", worker=None))
        s.commit()
        return res.rowcount or 0


def main() -> None:  # pragma: no cover
    import signal

    from .core.observability import configure_logging

    configure_logging()

    def _terminate(signum, _frame):
        # docker stop / systemd send SIGTERM: leave the loop (also from inside a run) and hand the run back
        raise SystemExit(128 + signum)

    signal.signal(signal.SIGTERM, _terminate)
    log.info("worker started", extra={"extra_data": {"worker": WORKER_ID}})
    try:
        loop()
    except (KeyboardInterrupt, SystemExit):
        pass
    finally:
        _stop.set()
        n = requeue_own()
        if n:
            log.warning("re-queued %d planning run(s) interrupted by the shutdown", n)
        log.info("worker stopped", extra={"extra_data": {"worker": WORKER_ID}})


if __name__ == "__main__":  # pragma: no cover
    main()
