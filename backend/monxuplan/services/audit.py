"""Audit trail: who, when, what, before, after, why."""

from __future__ import annotations

import uuid
from datetime import date, datetime, time
from typing import Any

from sqlalchemy.orm import Session

from ..models import AuditLog
from .context import Ctx

SKIP = {"password_hash", "secret_encrypted", "key_hash", "data"}


def snapshot(obj: Any, fields: list[str] | None = None) -> dict[str, Any] | None:
    if obj is None:
        return None
    cols = fields or [c.key for c in obj.__table__.columns]
    out: dict[str, Any] = {}
    for k in cols:
        if k in SKIP:
            continue
        v = getattr(obj, k, None)
        out[k] = _json(v)
    return out


def _json(v: Any) -> Any:
    if isinstance(v, uuid.UUID):
        return str(v)
    if isinstance(v, datetime | date | time):
        return v.isoformat()
    if isinstance(v, dict):
        return {k: _json(x) for k, x in v.items()}
    if isinstance(v, list | tuple):
        return [_json(x) for x in v]
    return v


def diff(before: dict | None, after: dict | None) -> tuple[dict | None, dict | None]:
    """Keep only changed keys (smaller audit rows, easier to read)."""
    if before is None or after is None:
        return before, after
    keys = [k for k in set(before) | set(after) if before.get(k) != after.get(k) and k not in ("updated_at", "version")]
    return {k: before.get(k) for k in keys}, {k: after.get(k) for k in keys}


def record(
    s: Session,
    ctx: Ctx,
    action: str,
    entity_type: str,
    entity_id: Any = None,
    label: str | None = None,
    before: dict | None = None,
    after: dict | None = None,
    reason: str | None = None,
    compact: bool = True,
) -> AuditLog:
    if compact and before is not None and after is not None:
        before, after = diff(before, after)
    row = AuditLog(
        tenant_id=ctx.tenant_id,
        user=ctx.username,
        action=action,
        entity_type=entity_type,
        entity_id=str(entity_id) if entity_id is not None else None,
        entity_label=label,
        before=before,
        after=after,
        reason=reason,
        request_id=ctx.request_id,
        ip=ctx.ip,
    )
    s.add(row)
    return row
