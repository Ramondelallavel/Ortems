"""Outbound webhooks: transactional outbox, atomic claim, signed deliveries, retries with back-off.

``emit`` writes a ``webhook_delivery`` row in the transaction of the change it announces (outbox): if the
change rolls back, nothing is announced. Deliveries are sent by whichever process calls
``deliver_pending`` (worker loop or API thread); a row is *claimed* first — a lease (``locked_until``,
``locked_by``) taken with ``FOR UPDATE SKIP LOCKED`` and a conditional update — so two replicas never
send the same delivery at the same time. The HTTP call runs outside any database transaction; the result
is written back only by the lease holder. Failed attempts are retried with exponential back-off from the
last attempt (30 s · 2^n, at most 1 h) up to ``MAX_ATTEMPTS``, then the delivery is ``FAILED``.

Receivers must deduplicate on ``X-Monxu-Delivery`` (at-least-once: a worker that dies after the POST
but before recording it leaves the lease to expire and the delivery is sent again).

Headers: ``X-Monxu-Event``, ``X-Monxu-Delivery``, ``X-Monxu-Timestamp`` and
``X-Monxu-Signature: sha256=HMAC(secret, timestamp + "." + body)``. Destinations pass the outbound
network policy (``core.netpolicy``) and the connection is made to the checked address.
"""

from __future__ import annotations

import json
import logging
import os
import socket
import threading
import uuid
from datetime import timedelta
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx
from sqlalchemy import or_, select, update
from sqlalchemy.orm import Session

from ..core.clock import now
from ..core.config import get_settings
from ..core.db import new_session
from ..core.errors import DomainError
from ..core.netpolicy import check_url
from ..core.security import decrypt_secret, sign_payload
from ..models import WebhookDelivery, WebhookSubscription

log = logging.getLogger("monxuplan.webhooks")
EVENT_TYPES = (
    "plan.published",
    "plan.created",
    "planning.run.finished",
    "alert.created",
    "order.updated",
    "machine.down",
    "machine.available",
    "material.delayed",
    "import.completed",
)
MAX_ATTEMPTS = 5
LEASE_S = 120
WORKER_ID = f"{socket.gethostname()}:{os.getpid()}"


def emit(s: Session, tenant_id: uuid.UUID, event_type: str, payload: dict[str, Any]) -> int:
    subs = [w for w in s.scalars(select(WebhookSubscription).where(WebhookSubscription.is_active.is_(True))) if event_type in (w.events or []) or "*" in (w.events or [])]
    t = now()
    for w in subs:
        s.add(WebhookDelivery(tenant_id=tenant_id, subscription_id=w.id, event_type=event_type, payload={"event": event_type, "data": payload, "emitted_at": t.isoformat()}, next_attempt_at=t))
    return len(subs)


def backoff(attempts: int) -> timedelta:
    return timedelta(seconds=min(30 * (2 ** max(attempts - 1, 0)), 3600))


def claim(limit: int = 20, worker: str = WORKER_ID) -> list[uuid.UUID]:
    """Take a lease on due deliveries; returns the ids this worker now owns."""
    t = now()
    W = WebhookDelivery
    due = (W.status == "PENDING", or_(W.next_attempt_at.is_(None), W.next_attempt_at <= t), or_(W.locked_until.is_(None), W.locked_until < t))
    with new_session(None, "webhooks") as s:
        q = select(W.id).where(*due).order_by(W.next_attempt_at).limit(limit).execution_options(skip_tenant_filter=True)
        if not get_settings().is_sqlite:
            q = q.with_for_update(skip_locked=True)
        ids = list(s.scalars(q))
        owned = []
        for i in ids:
            # conditional update: only one claimer can move the lease (also without row locks, SQLite)
            res = s.execute(update(W).where(W.id == i, *due).values(locked_until=t + timedelta(seconds=LEASE_S), locked_by=worker).execution_options(synchronize_session=False))
            if res.rowcount == 1:
                owned.append(i)
        s.commit()
    return owned


def _send(url: str, body: bytes, headers: dict[str, str], timeout: float) -> int:
    dest, scheme = check_url(url, "Webhook")
    parts = urlsplit(url)
    ip = dest.address
    netloc = (f"[{ip}]" if ":" in ip else ip) + (f":{parts.port}" if parts.port else "")
    pinned = urlunsplit((scheme, netloc, parts.path or "/", parts.query, ""))
    headers = {**headers, "Host": parts.netloc.rsplit("@", 1)[-1]}
    ext = {"sni_hostname": parts.hostname} if scheme == "https" else {}
    with httpx.Client(timeout=timeout, follow_redirects=False) as c:
        return c.send(c.build_request("POST", pinned, content=body, headers=headers, extensions=ext)).status_code


def deliver(delivery_id: uuid.UUID, worker: str = WORKER_ID) -> str:
    """Send one claimed delivery; returns its new status."""
    W = WebhookDelivery
    with new_session(None, "webhooks") as s:
        d = s.execute(select(W).where(W.id == delivery_id).execution_options(skip_tenant_filter=True)).scalar_one_or_none()
        if d is None or d.locked_by != worker or d.status != "PENDING":
            return "NOT_OWNED"
        sub = s.execute(select(WebhookSubscription).where(WebhookSubscription.id == d.subscription_id).execution_options(skip_tenant_filter=True)).scalar_one_or_none()
        attempts = (d.attempts or 0) + 1
        job = None if sub is None or not sub.is_active else (sub.url, sub.secret_encrypted, json.dumps(d.payload, default=str).encode(), d.event_type)
    code: int | None = None
    error: str | None = None
    if job is None:
        status, error = "FAILED", "subscription removed or inactive"
    else:
        url, secret_enc, body, event_type = job
        ts = str(int(now().timestamp()))
        try:
            code = _send(
                url,
                body,
                {
                    "Content-Type": "application/json",
                    "X-Monxu-Event": event_type,
                    "X-Monxu-Delivery": str(delivery_id),
                    "X-Monxu-Timestamp": ts,
                    "X-Monxu-Signature": sign_payload(decrypt_secret(secret_enc), body, ts),
                },
                get_settings().webhook_timeout_s,
            )
            error = None if 200 <= code < 300 else f"HTTP {code}"
        except DomainError as exc:  # refused by the network policy: retrying will not help
            error = exc.message
            attempts = MAX_ATTEMPTS
        except Exception as exc:  # noqa: BLE001 - network errors are recorded and retried
            error = f"{exc.__class__.__name__}: {str(exc)[:300]}"
        status = "DELIVERED" if error is None else ("FAILED" if attempts >= MAX_ATTEMPTS else "PENDING")
    t = now()
    values: dict[str, Any] = {"status": status, "attempts": attempts, "last_attempt_at": t, "response_code": code, "error": error, "locked_until": None, "locked_by": None}
    if status == "DELIVERED":
        values["delivered_at"] = t
    elif status == "PENDING":
        values["next_attempt_at"] = t + backoff(attempts)
    with new_session(None, "webhooks") as s:
        # only the lease holder records the outcome (an expired lease may have been taken over)
        res = s.execute(update(W).where(W.id == delivery_id, W.locked_by == worker).values(**values).execution_options(synchronize_session=False))
        s.commit()
    if res.rowcount != 1:
        log.warning("webhook delivery outcome not recorded: lease lost", extra={"extra_data": {"delivery": str(delivery_id)}})
        return "LEASE_LOST"
    return status


def auto_delivery_enabled() -> bool:
    """Background delivery (API thread, worker loop); ``MONXU_WEBHOOK_DELIVERY=0`` turns it off."""
    return os.environ.get("MONXU_WEBHOOK_DELIVERY", "1") != "0"


def deliver_pending(limit: int = 20) -> int:
    """Claim and send due deliveries (worker loop / API thread). Returns the number delivered.
    Each round has its own lease identity, so two threads of one process never share a lease."""
    sent = 0
    me = f"{WORKER_ID}:{uuid.uuid4().hex[:12]}"
    for did in claim(limit, worker=me):
        if deliver(did, worker=me) == "DELIVERED":
            sent += 1
    return sent


_started = False


def start_delivery_thread(interval_s: float = 10.0) -> None:
    global _started
    if _started:
        return
    _started = True

    def loop() -> None:  # pragma: no cover - background thread
        while True:
            try:
                if auto_delivery_enabled():
                    deliver_pending()
            except Exception:  # noqa: BLE001
                log.exception("webhook delivery loop error")
            threading.Event().wait(interval_s)

    threading.Thread(target=loop, daemon=True, name="monxu-webhooks").start()
