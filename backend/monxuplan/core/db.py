"""Database engine, sessions and tenant isolation.

Every ORM model that inherits :class:`TenantMixin` is automatically filtered by the tenant bound to
the session (``session.info["tenant_id"]``) on every SELECT, and new rows get the tenant id on flush.
Cross-tenant access therefore requires an explicit, visible opt-out
(``execution_options(skip_tenant_filter=True)``), used only by authentication and the worker bootstrap.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from typing import Any

from sqlalchemy import JSON, DateTime, Integer, MetaData, String, Uuid, create_engine, event
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, declared_attr, mapped_column, sessionmaker, with_loader_criteria

from .clock import now
from .config import get_settings

NAMING = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

JSONType = JSON().with_variant(JSONB(), "postgresql")


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING)
    type_annotation_map = {dict[str, Any]: JSONType, list[Any]: JSONType}


class IdMixin:
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now, nullable=False)
    created_by: Mapped[str | None] = mapped_column(String(120))
    updated_by: Mapped[str | None] = mapped_column(String(120))


class VersionMixin:
    """Optimistic locking: SQLAlchemy increments ``version`` on every UPDATE and raises
    ``StaleDataError`` (→ HTTP 409) when the row was changed by someone else meanwhile."""

    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    @declared_attr.directive
    def __mapper_args__(cls) -> dict:
        return {"version_id_col": cls.__table__.c.version}


class TenantMixin:
    tenant_id: Mapped[uuid.UUID] = mapped_column(Uuid, index=True, nullable=False)


_engine: Engine | None = None
_SessionFactory: sessionmaker | None = None


def get_engine() -> Engine:
    global _engine, _SessionFactory
    if _engine is None:
        s = get_settings()
        kwargs: dict[str, Any] = {"future": True, "pool_pre_ping": True}
        if s.is_sqlite:
            from pathlib import Path

            path = s.database_url.replace("sqlite:///", "")
            if path and path != ":memory:":
                Path(path).parent.mkdir(parents=True, exist_ok=True)
            kwargs["connect_args"] = {"check_same_thread": False, "timeout": 30}
        else:
            kwargs.update(pool_size=10, max_overflow=20)
        _engine = create_engine(s.database_url, **kwargs)
        if s.is_sqlite:

            @event.listens_for(_engine, "connect")
            def _sqlite_pragmas(dbapi_conn, _rec):  # pragma: no cover - driver hook
                cur = dbapi_conn.cursor()
                cur.execute("PRAGMA foreign_keys=ON")
                cur.execute("PRAGMA journal_mode=WAL")
                cur.execute("PRAGMA busy_timeout=30000")
                cur.close()

        _SessionFactory = sessionmaker(bind=_engine, expire_on_commit=False, future=True)
    return _engine


def reset_engine() -> None:
    global _engine, _SessionFactory
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _SessionFactory = None


def new_session(tenant_id: uuid.UUID | None = None, user: str | None = None) -> Session:
    get_engine()
    assert _SessionFactory is not None
    s = _SessionFactory()
    s.info["tenant_id"] = tenant_id
    s.info["user"] = user
    return s


@contextmanager
def session_scope(tenant_id: uuid.UUID | None = None, user: str | None = None) -> Iterator[Session]:
    s = new_session(tenant_id, user)
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


@event.listens_for(Session, "do_orm_execute")
def _tenant_filter(state) -> None:
    tid = state.session.info.get("tenant_id")
    if tid is None or not state.is_select or state.execution_options.get("skip_tenant_filter"):
        return
    state.statement = state.statement.options(
        with_loader_criteria(TenantMixin, lambda cls: cls.tenant_id == tid, include_aliases=True)
    )


@event.listens_for(Session, "before_flush")
def _stamp(session: Session, _ctx, _instances) -> None:
    tid = session.info.get("tenant_id")
    user = session.info.get("user")
    for obj in session.new:
        if isinstance(obj, TenantMixin) and getattr(obj, "tenant_id", None) is None:
            if tid is None:
                raise RuntimeError(f"{type(obj).__name__} created without tenant context")
            obj.tenant_id = tid
        if isinstance(obj, TimestampMixin):
            obj.created_by = obj.created_by or user
            obj.updated_by = obj.updated_by or user
    for obj in session.dirty:
        if isinstance(obj, TenantMixin) and tid is not None and obj.tenant_id != tid:
            raise PermissionError("cross-tenant update rejected")
        if isinstance(obj, TimestampMixin) and user:
            obj.updated_by = user


def create_all() -> None:
    from .. import models  # noqa: F401  (register mappers)

    Base.metadata.create_all(get_engine())
