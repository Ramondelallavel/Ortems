"""Single source of 'now' (UTC). ``MONXU_NOW`` fixes the clock for demos and tests."""

from __future__ import annotations

from datetime import UTC, datetime

from .config import get_settings

_override: datetime | None = None


def set_now(dt: datetime | None) -> None:
    global _override
    _override = dt.astimezone(UTC) if dt is not None else None


def now() -> datetime:
    if _override is not None:
        return _override
    s = get_settings().now_override
    if s:
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt.astimezone(UTC)
    return datetime.now(UTC)
