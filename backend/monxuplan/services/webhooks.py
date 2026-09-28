"""Outbound webhooks: signed JSON deliveries with retries.

Headers: ``X-Monxu-Event``, ``X-Monxu-Delivery``, ``X-Monxu-Timestamp`` and
``X-Monxu-Signature: sha256=HMAC(secret, timestamp + "." + body)``.
"""

from __future__ import annotations

import json
import logging
import threading
import uuid
from datetime import timedelta
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..core.clock import now
from ..core.config import get_settings
from ..core.db import new_session
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


def emit(s: Session, tenant_id: uuid.UUID, event_type: str, payload: dict[str, Any]) -> int:
    subs = [w for w in s.scalars(select(WebhookSubscription).where(WebhookSubscription.is_active.is_(True))) if event_type in (w.events or []) or "*" in (w.events or [])]
    for w in subs:
        s.add(WebhookDelivery(tenant_id=tenant_id, subscription_id=w.id, event_type=event_type, payload={"event": event_type, "data": payload, "emitted_at": now().isoformat()}))
    return len(subs)


def deliver_pending(limit: int = 50) -> int:
    """Called periodically by the worker."""
    sent = 0
    with new_session(None, "webhooks") as s:
        rows = list(
            s.scalars(
                select(WebhookDelivery).where(WebhookDelivery.status == "PENDING").order_by(WebhookDelivery.created_at).limit(limit).execution_options(skip_tenant_filter=True)
            )
        )
        for d in rows:
            sub = s.get(WebhookSubscription, d.subscription_id)
            if sub is None or not sub.is_active:
                d.status = "FAILED"
                d.error = "subscription removed"
                continue
            if d.attempts and d.delivered_at is None and d.created_at and now() - _aw(d.created_at) < timedelta(seconds=30 * (2 ** min(d.attempts, 6))):
                continue
            d.attempts += 1
            body = json.dumps(d.payload, default=str).encode()
            ts = str(int(now().timestamp()))
            try:
                secret = decrypt_secret(sub.secret_encrypted)
                r = httpx.post(
                    sub.url,
                    content=body,
                    headers={
                        "Content-Type": "application/json",
                        "X-Monxu-Event": d.event_type,
                        "X-Monxu-Delivery": str(d.id),
                        "X-Monxu-Timestamp": ts,
                        "X-Monxu-Signature": sign_payload(secret, body, ts),
                    },
                    timeout=get_settings().webhook_timeout_s,
                )
                d.response_code = r.status_code
                if 200 <= r.status_code < 300:
                    d.status = "DELIVERED"
                    d.delivered_at = now()
                    sent += 1
                elif d.attempts >= MAX_ATTEMPTS:
                    d.status = "FAILED"
                    d.error = f"HTTP {r.status_code}"
            except Exception as exc:  # noqa: BLE001
                d.error = str(exc)[:500]
                if d.attempts >= MAX_ATTEMPTS:
                    d.status = "FAILED"
        s.commit()
    return sent


def _aw(dt):
    from datetime import UTC

    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


_started = False


def start_delivery_thread(interval_s: float = 10.0) -> None:
    global _started
    if _started:
        return
    _started = True

    def loop() -> None:  # pragma: no cover - background thread
        while True:
            try:
                deliver_pending()
            except Exception:  # noqa: BLE001
                log.exception("webhook delivery loop error")
            threading.Event().wait(interval_s)

    threading.Thread(target=loop, daemon=True, name="monxu-webhooks").start()
