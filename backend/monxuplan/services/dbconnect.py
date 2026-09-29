"""Database connectors: read data from the customer's own databases into MonxuPlan.

A connector (an :class:`Integration` with ``system = "DATABASE"``) holds the connection settings — the
password encrypted at rest — and a list of *sources*. A source says which MonxuPlan table it feeds
(any import template: the friendly ones such as ``production-orders`` or any ``table:<entity>``),
where the rows come from (a table/view name or a ``SELECT`` query) and how its columns map to fields.

A sync runs each source's query and hands the rows to the normal import pipeline (mapping,
validation dry run, commit), so data from a database gets exactly the same checks as an Excel file,
is listed in the import history and can be reviewed when it has errors.

Safety: only ``SELECT``/``WITH`` statements are accepted, the session is set read-only where the
database supports it, every transaction is rolled back, row counts are capped, and connections time
out. Give the connector a database user with read-only grants all the same.
"""

from __future__ import annotations

import importlib.util
import re
import sys
import uuid
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import create_engine, func, inspect, select, text, update
from sqlalchemy.engine import URL
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

from .. import models as M
from ..core.clock import now
from ..core.config import get_settings
from ..core.errors import DomainError, NotFound, ValidationFailed
from ..core.netpolicy import check_host
from ..core.security import decrypt_secret, encrypt_secret
from . import audit, imports
from .context import Ctx

MAX_ROWS = imports.MAX_ROWS
PREVIEW_ROWS = 50

# dialect -> (SQLAlchemy driver name, Python package, pip name, default port, label)
DIALECTS: dict[str, tuple[str, str | None, str | None, int | None, str]] = {
    "postgresql": ("postgresql+psycopg", "psycopg", "psycopg[binary]", 5432, "PostgreSQL"),
    "mysql": ("mysql+pymysql", "pymysql", "pymysql", 3306, "MySQL / MariaDB"),
    "mssql": ("mssql+pymssql", "pymssql", "pymssql", 1433, "Microsoft SQL Server"),
    "oracle": ("oracle+oracledb", "oracledb", "oracledb", 1521, "Oracle Database"),
    "sqlite": ("sqlite", None, None, None, "SQLite file"),
}
FORBIDDEN = re.compile(
    r"\b(insert|update|delete|merge|drop|alter|create|truncate|grant|revoke|exec|execute|call|copy|attach|detach|pragma|vacuum|replace|lock"
    # SELECT … INTO creates a table (PostgreSQL, SQL Server) or writes a file (MySQL INTO OUTFILE/DUMPFILE)
    # (statement keywords such as SET or DECLARE cannot start a second statement: the query must begin
    # with SELECT/WITH and contain no ';')
    r"|into|outfile|dumpfile|load_file"
    # server-side functions that read files, sleep, signal or reach the network
    r"|pg_read_file|pg_read_binary_file|pg_ls_dir|pg_stat_file|pg_sleep\w*|pg_terminate_backend|pg_cancel_backend|pg_reload_conf|lo_import|lo_export|dblink\w*"
    r"|sleep|benchmark|waitfor|openrowset|opendatasource|openquery"
    r"|xp_\w+|sp_\w+|utl_\w+|dbms_\w+|sys_eval|sys_exec)\b",
    re.I,
)


def runtime_supported() -> tuple[bool, str | None]:
    if sys.platform == "emscripten":
        return False, "This edition runs inside the browser, which cannot open database connections. Use the server edition of MonxuPlan to connect databases; Excel/CSV import works here."
    return True, None


def drivers() -> list[dict[str, Any]]:
    ok, why = runtime_supported()
    out = []
    for code, (_drv, pkg, pip, port, label) in DIALECTS.items():
        installed = ok and (pkg is None or importlib.util.find_spec(pkg) is not None)
        allowed = code != "sqlite" or sqlite_allowed()
        out.append(
            {
                "dialect": code,
                "label": label,
                "default_port": port,
                "installed": installed and allowed,
                "reason": why if not ok else (None if installed and allowed else ("SQLite files can only be read when MONXU_ALLOW_SQLITE_SOURCES=1" if not allowed else f"Install the driver on the server: pip install {pip}")),
            }
        )
    return out


def sqlite_allowed() -> bool:
    import os

    v = os.environ.get("MONXU_ALLOW_SQLITE_SOURCES")
    if v is not None:
        return v.strip().lower() in ("1", "true", "yes")
    return not get_settings().is_production


# ---------------------------------------------------------------------------------------------
# connections
# ---------------------------------------------------------------------------------------------


def _url(cfg: dict[str, Any], password: str | None) -> URL:
    dialect = cfg.get("dialect")
    if dialect not in DIALECTS:
        raise ValidationFailed(f"Unknown database type '{dialect}'. Use one of: {', '.join(DIALECTS)}", code="UNKNOWN_DIALECT", context={"field": "dialect"})
    drv, pkg, pip, port, label = DIALECTS[dialect]
    ok, why = runtime_supported()
    if not ok:
        raise ValidationFailed(why, code="NOT_SUPPORTED_HERE")
    if pkg and importlib.util.find_spec(pkg) is None:
        raise ValidationFailed(f"The {label} driver is not installed on the server (pip install {pip}).", code="DRIVER_MISSING", context={"field": "dialect"})
    if dialect == "sqlite":
        if not sqlite_allowed():
            raise ValidationFailed("Reading SQLite files is disabled on this server (MONXU_ALLOW_SQLITE_SOURCES).", code="SQLITE_DISABLED")
        if not cfg.get("database"):
            raise ValidationFailed("Give the path of the SQLite file.", code="REQUIRED", context={"field": "database"})
        return URL.create("sqlite", database=str(cfg["database"]))
    if not cfg.get("host"):
        raise ValidationFailed("Give the database server host.", code="REQUIRED", context={"field": "host"})
    query = {str(k): str(v) for k, v in (cfg.get("options") or {}).items()}
    if any(k.lower() in ("host", "hostaddr", "server", "dsn") for k in query):
        raise ValidationFailed("The server address is given in 'host', not in driver options.", code="EGRESS_DENIED", context={"field": "options"})
    if dialect == "oracle" and cfg.get("database") and "service_name" not in query:
        query["service_name"] = str(cfg["database"])
    port_n = int(cfg.get("port") or port) if (cfg.get("port") or port) else None
    # outbound policy on every resolved address; the driver then connects to the checked address
    # (for PostgreSQL through hostaddr, which keeps the host name for TLS verification)
    dest = check_host(str(cfg["host"]), port_n or 0, "Database connector")
    host = str(cfg["host"])
    if dialect == "postgresql":
        query["hostaddr"] = dest.address
    else:
        host = dest.address
    return URL.create(
        drv,
        username=cfg.get("username") or None,
        password=password or None,
        host=host,
        port=port_n,
        database=None if dialect == "oracle" else (cfg.get("database") or None),
        query=query,
    )


def _connect_args(dialect: str, timeout: int) -> dict[str, Any]:
    if dialect == "postgresql":
        return {"connect_timeout": timeout, "options": f"-c statement_timeout={timeout * 12 * 1000} -c default_transaction_read_only=on"}
    if dialect == "mysql":
        return {"connect_timeout": timeout, "read_timeout": timeout * 12}
    if dialect == "mssql":
        return {"login_timeout": timeout, "timeout": timeout * 12}
    if dialect == "oracle":
        return {"tcp_connect_timeout": timeout}
    if dialect == "sqlite":
        return {"uri": False}
    return {}


def _engine(cfg: dict[str, Any], password: str | None):
    url = _url(cfg, password)
    dialect = cfg["dialect"]
    if dialect == "sqlite":
        import os

        if not os.path.exists(str(cfg["database"])):
            raise ValidationFailed(f"SQLite file not found: {cfg['database']}", code="NOT_FOUND", context={"field": "database"})
        return create_engine(f"sqlite:///file:{cfg['database']}?mode=ro&uri=true", poolclass=NullPool)
    return create_engine(url, poolclass=NullPool, connect_args=_connect_args(dialect, int(cfg.get("timeout_s") or 10)))


def _read_only(conn, dialect: str) -> None:
    if dialect in ("postgresql", "oracle"):
        conn.execute(text("SET TRANSACTION READ ONLY"))
    elif dialect == "mysql":
        conn.execute(text("SET SESSION TRANSACTION READ ONLY"))


def _clean_sql(sql: str) -> str:
    stripped = re.sub(r"--[^\n]*|/\*.*?\*/", " ", sql, flags=re.S).strip().rstrip(";").strip()
    if not re.match(r"^(select|with)\b", stripped, re.I):
        raise ValidationFailed("Only SELECT queries (or WITH … SELECT) can be used to read data.", code="NOT_A_SELECT", context={"field": "query"})
    if ";" in stripped:
        raise ValidationFailed("Use a single SELECT statement (no ';').", code="MULTIPLE_STATEMENTS", context={"field": "query"})
    no_strings = re.sub(r"'(?:[^']|'')*'", "''", stripped)
    m = FORBIDDEN.search(no_strings)
    if m:
        raise ValidationFailed(f"The query contains '{m.group(1)}'; only reading is allowed.", code="NOT_READ_ONLY", context={"field": "query"})
    return stripped


def _quote_table(engine, name: str) -> str:
    parts = [p for p in re.split(r"\.", name.strip()) if p]
    if not parts or not all(re.match(r"^[A-Za-z_][A-Za-z0-9_$ #-]*$", p) for p in parts):
        raise ValidationFailed(f"'{name}' is not a valid table or view name.", code="BAD_TABLE", context={"field": "table"})
    prep = engine.dialect.identifier_preparer
    return ".".join(prep.quote(p) for p in parts)


def _value(v: Any) -> Any:
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, bytes | bytearray | memoryview):
        return None
    if isinstance(v, datetime):
        return v.replace(tzinfo=None).isoformat(sep=" ", timespec="minutes") if v.tzinfo is None else v.isoformat()
    if hasattr(v, "isoformat"):
        return v.isoformat()
    if isinstance(v, str):
        v = v.strip()
        return v or None
    return v


def _read(cfg: dict[str, Any], password: str | None, source: dict[str, Any], limit: int) -> tuple[list[str], list[list[Any]]]:
    engine = _engine(cfg, password)
    try:
        with engine.connect() as conn:
            try:
                _read_only(conn, cfg["dialect"])
            except Exception as exc:
                if cfg["dialect"] in ("postgresql", "mysql"):  # both support it: a failure is not normal
                    raise ValidationFailed(f"The database refused a read-only transaction: {str(getattr(exc, 'orig', exc))[:200]}", code="READ_ONLY_REFUSED") from exc
                conn.rollback()  # Oracle/SQL Server variants without it: the statement check still applies
            if source.get("query"):
                sql = _clean_sql(str(source["query"]))
            elif source.get("table"):
                sql = f"SELECT * FROM {_quote_table(engine, str(source['table']))}"
            else:
                raise ValidationFailed("Choose a table or write a SELECT query.", code="REQUIRED", context={"field": "table"})
            res = conn.execution_options(stream_results=True).execute(text(sql))
            cols = [str(c) for c in res.keys()]  # noqa: SIM118 - Result.keys() is not a dict
            rows: list[list[Any]] = []
            for r in res:
                rows.append([_value(v) for v in r])
                if len(rows) > limit:
                    break
            conn.rollback()
    except DomainError:
        raise
    except Exception as exc:  # noqa: BLE001 - driver errors become a readable message
        raise ValidationFailed(f"The database answered: {str(getattr(exc, 'orig', exc)).strip()[:400]}", code="DATABASE_ERROR") from exc
    finally:
        engine.dispose()
    if len(rows) > limit and limit >= MAX_ROWS:
        raise ValidationFailed(f"The source returns more than {MAX_ROWS} rows. Add a WHERE clause to the query.", code="TOO_MANY_ROWS", context={"field": "query"})
    return cols, rows[:limit]


def test_connection(cfg: dict[str, Any], password: str | None) -> dict[str, Any]:
    engine = _engine(cfg, password)
    try:
        with engine.connect() as conn:
            version = conn.dialect.server_version_info
            names = _objects(engine)
            conn.rollback()
    except DomainError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise ValidationFailed(f"Could not connect: {str(getattr(exc, 'orig', exc)).strip()[:400]}", code="CONNECTION_FAILED") from exc
    finally:
        engine.dispose()
    return {"ok": True, "server_version": ".".join(str(x) for x in version) if version else None, "tables": names[:500], "table_count": len(names)}


def _objects(engine, schema: str | None = None) -> list[str]:
    insp = inspect(engine)
    out = []
    for name in insp.get_table_names(schema=schema):
        out.append(f"{schema}.{name}" if schema else name)
    try:
        for name in insp.get_view_names(schema=schema):
            out.append(f"{schema}.{name}" if schema else name)
    except NotImplementedError:
        pass
    return sorted(out)


# ---------------------------------------------------------------------------------------------
# stored connectors
# ---------------------------------------------------------------------------------------------


def validate_settings(settings: dict[str, Any]) -> dict[str, Any]:
    cfg = dict(settings or {})
    if cfg.get("dialect") not in DIALECTS:
        raise ValidationFailed("Choose the database type.", code="UNKNOWN_DIALECT", context={"field": "dialect"})
    srcs = []
    for i, src in enumerate(cfg.get("sources") or []):
        src = dict(src)
        src.setdefault("id", uuid.uuid4().hex[:8])
        if not src.get("entity") or imports.get_template(str(src["entity"])) is None:
            raise ValidationFailed(f"Source {i + 1}: choose the MonxuPlan table it feeds.", code="UNKNOWN_ENTITY", context={"field": f"sources[{i}].entity"})
        if not src.get("query") and not src.get("table"):
            raise ValidationFailed(f"Source {i + 1}: choose a table or write a SELECT query.", code="REQUIRED", context={"field": f"sources[{i}].table"})
        if src.get("query"):
            _clean_sql(str(src["query"]))
        if src.get("mode", "UPSERT") not in ("UPSERT", "CREATE_ONLY", "UPDATE_ONLY"):
            raise ValidationFailed(f"Source {i + 1}: unknown mode.", code="BAD_MODE")
        srcs.append(src)
    cfg["sources"] = srcs
    every = cfg.get("sync_every_minutes")
    if every not in (None, "", 0):
        if int(every) < 5:
            raise ValidationFailed("Automatic sync can run every 5 minutes at most.", code="TOO_FREQUENT", context={"field": "sync_every_minutes"})
        cfg["sync_every_minutes"] = int(every)
    else:
        cfg["sync_every_minutes"] = None
    return cfg


def _connector(s: Session, ctx: Ctx, cid: uuid.UUID) -> M.Integration:
    i = s.get(M.Integration, cid)
    if i is None or i.system != "DATABASE":
        raise NotFound("Database connector not found", code="NOT_FOUND")
    return i


def _password(i: M.Integration) -> str | None:
    return decrypt_secret(i.secret_encrypted) if i.secret_encrypted else None


def public(i: M.Integration) -> dict[str, Any]:
    from .masterdata import row_dict

    out = row_dict(i)
    out["has_password"] = bool(i.secret_encrypted)
    return out


def save(s: Session, ctx: Ctx, data: dict[str, Any], cid: uuid.UUID | None = None) -> dict[str, Any]:
    ctx.require("integration:manage")
    cfg = validate_settings(data.get("settings") or {})
    code = str(data.get("code") or "").strip()[:60]
    if cid is not None and not code:
        code = _connector(s, ctx, cid).code
    if not code:
        raise ValidationFailed("Give the connector a code.", code="REQUIRED", context={"field": "code"})
    q = select(M.Integration.id).where(func.lower(M.Integration.code) == code.lower())
    if cid is not None:
        q = q.where(M.Integration.id != cid)
    if s.scalar(q) is not None:
        raise ValidationFailed(f"There is already a connector with code {code}.", code="DUPLICATE_CODE", context={"field": "code"})
    if cid is None:
        i = M.Integration(tenant_id=ctx.tenant_id, system="DATABASE", direction="IN")
        s.add(i)
        before = None
    else:
        i = _connector(s, ctx, cid)
        before = audit.snapshot(i)
    i.code = code
    i.name = str(data.get("name") or i.name or code).strip()[:200]
    i.settings = cfg
    if "is_active" in data:
        i.is_active = bool(data["is_active"])
    if data.get("password"):
        i.secret_encrypted = encrypt_secret(str(data["password"]))
    elif data.get("clear_password"):
        i.secret_encrypted = None
    s.flush()
    audit.record(s, ctx, "CREATE" if before is None else "UPDATE", "integration", i.id, i.code, before=before, after=audit.snapshot(i))
    return public(i)


def delete(s: Session, ctx: Ctx, cid: uuid.UUID) -> None:
    ctx.require("integration:manage")
    i = _connector(s, ctx, cid)
    audit.record(s, ctx, "DELETE", "integration", i.id, i.code, before=audit.snapshot(i))
    s.delete(i)
    s.flush()


def objects(s: Session, ctx: Ctx, cid: uuid.UUID) -> dict[str, Any]:
    ctx.require("integration:import")
    i = _connector(s, ctx, cid)
    return test_connection(i.settings or {}, _password(i))


def preview(s: Session, ctx: Ctx, cid: uuid.UUID, source: dict[str, Any], entity: str | None = None) -> dict[str, Any]:
    """First rows of a table/query, plus the suggested field mapping for ``entity``."""
    ctx.require("integration:import")
    i = _connector(s, ctx, cid)
    cols, rows = _read(i.settings or {}, _password(i), source, PREVIEW_ROWS)
    out: dict[str, Any] = {"columns": cols, "rows": rows}
    tpl = imports.get_template(entity) if entity else None
    if tpl is not None:
        out["mapping"] = imports.suggest_mapping(tpl, cols)
        out["fields"] = [{"name": f.name, "type": f.type, "required": f.required, "description": f.description} for f in tpl.fields]
    return out


def sync(s: Session, ctx: Ctx, cid: uuid.UUID, source_ids: list[str] | None = None, plant_id: uuid.UUID | None = None, commit: bool | None = None) -> dict[str, Any]:
    """Read every (or the chosen) source and run it through the import pipeline.

    A source whose validation finds errors is left VALIDATED/INVALID in the import history for review;
    nothing of it is written unless the source says ``skip_invalid_rows``."""
    ctx.require("integration:import")
    i = _connector(s, ctx, cid)
    cfg = i.settings or {}
    pwd = _password(i)
    results = []
    status = "OK"
    plant = s.get(M.Plant, plant_id) if plant_id else None
    if plant is None and cfg.get("plant_id"):
        plant = s.get(M.Plant, uuid.UUID(str(cfg["plant_id"])))
    if plant is None:
        plants = list(s.scalars(select(M.Plant).limit(2)))
        if len(plants) != 1:
            raise ValidationFailed("Choose the plant the connector's data belongs to.", code="NO_PLANT", context={"field": "plant_id"})
        plant = plants[0]
    ctx.require_plant(plant.id)
    for src in cfg.get("sources") or []:
        if source_ids and src.get("id") not in source_ids:
            continue
        if not source_ids and src.get("enabled") is False:
            continue
        label = src.get("table") or "query"
        try:
            cols, rows = _read(cfg, pwd, src, MAX_ROWS)
            if not rows:
                results.append({"source": src["id"], "entity": src["entity"], "status": "EMPTY", "message": f"{label}: no rows"})
                continue
            tpl = imports.get_template(src["entity"])
            opts = {"plant_id": str(plant.id) if plant else None, "mode": src.get("mode", "UPSERT"), "date_format": src.get("date_format", "DMY"), "skip_invalid_rows": bool(src.get("skip_invalid_rows")), "connector": i.code, "source_id": src["id"]}
            job = M.ImportJob(tenant_id=ctx.tenant_id, entity=src["entity"], filename=f"{i.code}: {label}"[:300], file_format="db", status="UPLOADED", columns=cols, rows=rows, options=opts, source="DATABASE")
            mapping = imports.suggest_mapping(tpl, cols)
            for k, v in (src.get("mapping") or {}).items():
                if k in mapping or any(f.name == k for f in tpl.fields):
                    mapping[k] = v if v in cols else None
            job.mapping = mapping
            s.add(job)
            s.flush()
            v = imports.validate(s, ctx, job.id)
            auto = src.get("auto_commit", True) if commit is None else commit
            if v["stats"].get("can_import") and auto:
                v = imports.commit(s, ctx, job.id)
            if v["status"] != "IMPORTED":
                status = "REVIEW" if status == "OK" else status
            results.append({"source": src["id"], "entity": src["entity"], "status": v["status"], "job_id": v["id"], "stats": v["stats"], "errors": len(v.get("errors") or [])})
        except DomainError as exc:
            status = "ERROR"
            results.append({"source": src.get("id"), "entity": src.get("entity"), "status": "ERROR", "message": exc.message})
    i.last_run_at = now()
    i.last_status = status
    audit.record(s, ctx, "SYNC", "integration", i.id, i.code, after={"status": status, "sources": len(results)})
    return {"connector": i.code, "status": status, "results": results, "at": i.last_run_at.isoformat()}


def run_due(limit: int = 5) -> int:
    """Scheduled syncs (called by the worker loop): connectors whose interval has elapsed."""
    from ..core.db import new_session
    from .context import system_ctx

    done = 0
    with new_session(None, "scheduler") as s:
        cands = [(i.id, i.tenant_id, (i.settings or {}).get("sync_every_minutes"), i.last_run_at) for i in s.scalars(select(M.Integration).where(M.Integration.system == "DATABASE", M.Integration.is_active.is_(True)))]
    for cid, tid, every, last in cands:
        if not every or done >= limit:
            continue
        t = now()
        if last is not None and (last if last.tzinfo else last.replace(tzinfo=t.tzinfo)) > t - timedelta(minutes=int(every)):
            continue
        with new_session(tid, "scheduler") as s:
            # claim: only one worker runs a given connector for this interval
            q = update(M.Integration).where(M.Integration.id == cid)
            q = q.where((M.Integration.last_run_at.is_(None)) | (M.Integration.last_run_at == last)) if last is not None else q.where(M.Integration.last_run_at.is_(None))
            if (s.execute(q.values(last_run_at=t, last_status="RUNNING")).rowcount or 0) != 1:
                s.rollback()
                continue
            s.commit()
            try:
                sync(s, system_ctx(tid, "scheduler"), cid)
                s.commit()
            except Exception:  # noqa: BLE001 - one connector must not stop the others
                s.rollback()
                s.execute(update(M.Integration).where(M.Integration.id == cid).values(last_status="ERROR"))
                s.commit()
            done += 1
    return done
