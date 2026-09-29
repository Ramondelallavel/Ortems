"""Webhook outbox: deliveries are claimed by one worker at a time, retried with back-off, recorded only
by the lease holder, and never sent to a destination refused by the egress policy."""

import uuid
from datetime import timedelta

import pytest


@pytest.fixture()
def deliveries(client):
    from conftest import Api
    from sqlalchemy import update

    from monxuplan.core.db import new_session
    from monxuplan.core.security import encrypt_secret
    from monxuplan.models import WebhookDelivery, WebhookSubscription

    admin = Api(client, "admin")
    tid = uuid.UUID(admin.ok(admin.get("/auth/me"))["tenant_id"])
    with new_session(tid, "tests") as s:
        # park every other pending delivery so the test only sees its own rows
        s.execute(update(WebhookDelivery).where(WebhookDelivery.status == "PENDING").values(status="FAILED", error="parked by test"))
        sub = WebhookSubscription(name="t", url="https://hooks.example.com/in", events=["plan.published"], secret_encrypted=encrypt_secret("s" * 32))
        s.add(sub)
        s.flush()
        ids = []
        for k in range(6):
            d = WebhookDelivery(subscription_id=sub.id, event_type="plan.published", payload={"n": k})
            s.add(d)
            s.flush()
            ids.append(d.id)
        s.commit()
    return tid, ids


def test_two_workers_never_claim_the_same_delivery(deliveries):
    from monxuplan.services import webhooks

    _tid, ids = deliveries
    a = webhooks.claim(limit=4, worker="worker-a")
    b = webhooks.claim(limit=10, worker="worker-b")
    assert not set(a) & set(b)
    assert set(a) | set(b) == set(ids)
    assert webhooks.claim(limit=10, worker="worker-c") == []  # all leased


def test_retry_backoff_final_state_and_lease(deliveries, monkeypatch):
    from monxuplan.core.db import new_session
    from monxuplan.models import WebhookDelivery
    from monxuplan.services import webhooks

    tid, ids = deliveries
    calls = []
    monkeypatch.setattr(webhooks, "_send", lambda url, body, headers, timeout: calls.append(headers["X-Monxu-Delivery"]) or 503)
    [first, *_] = webhooks.claim(limit=1, worker="w")
    assert webhooks.deliver(first, worker="w") == "PENDING"
    with new_session(tid, "tests") as s:
        d = s.get(WebhookDelivery, first)
        assert d.attempts == 1 and d.locked_by is None and d.next_attempt_at > d.last_attempt_at
        assert d.next_attempt_at - d.last_attempt_at == timedelta(seconds=30)
    # not due again yet: not claimable
    assert first not in webhooks.claim(limit=10, worker="w2")
    # a worker that does not hold the lease cannot record an outcome
    assert webhooks.deliver(ids[-1], worker="someone-else") in ("NOT_OWNED",)
    # after MAX_ATTEMPTS the delivery is FAILED
    with new_session(tid, "tests") as s:
        s.get(WebhookDelivery, first).attempts = webhooks.MAX_ATTEMPTS - 1
        s.get(WebhookDelivery, first).next_attempt_at = None
        s.commit()
    assert first in webhooks.claim(limit=20, worker="w3")
    assert webhooks.deliver(first, worker="w3") == "FAILED"
    assert calls.count(str(first)) == 2


def test_delivery_to_a_refused_destination_fails_without_retry(deliveries, monkeypatch):
    from monxuplan.core.db import new_session
    from monxuplan.models import WebhookDelivery, WebhookSubscription
    from monxuplan.services import webhooks

    tid, ids = deliveries
    with new_session(tid, "tests") as s:
        d = s.get(WebhookDelivery, ids[0])
        s.get(WebhookSubscription, d.subscription_id).url = "http://169.254.169.254/latest"  # changed behind the API
        s.commit()
    assert ids[0] in webhooks.claim(limit=20, worker="w")
    assert webhooks.deliver(ids[0], worker="w") == "FAILED"
    with new_session(tid, "tests") as s:
        assert "metadata" in s.get(WebhookDelivery, ids[0]).error
