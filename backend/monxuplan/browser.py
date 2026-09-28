"""In-browser host: runs the MonxuPlan API inside a WebAssembly Python runtime (Pyodide).

The browser edition has no network server, no threads and no OpenSSL. This module adapts the same
FastAPI application to that environment:

* requests arrive from JavaScript as plain values and are dispatched straight into the ASGI app;
* sync endpoints run inline (there is no thread pool in WebAssembly);
* planning runs are queued exactly as on the server and executed by :func:`pump_jobs`, which the
  JavaScript host calls between requests;
* events published on the bus (run progress, plan changes, alerts) are forwarded to a JavaScript
  callback, which feeds the page's live-update stream.

Nothing here changes planning or business logic; the engine, services and API are the server code.
"""

from __future__ import annotations

import json
import os
from typing import Any

_app = None
_event_sink = None


def configure(db_path: str, now_override: str | None = None) -> None:
    os.environ.setdefault("MONXU_ENV", "browser")
    os.environ["MONXU_DATABASE_URL"] = f"sqlite:///{db_path}"
    os.environ["MONXU_WORKER_IN_PROCESS"] = "0"
    os.environ["MONXU_LOG_JSON"] = "0"
    os.environ["MONXU_LOG_LEVEL"] = "WARNING"
    os.environ["MONXU_RATE_LIMIT_PER_MINUTE"] = "100000"
    os.environ["MONXU_LOGIN_RATE_LIMIT_PER_MINUTE"] = "1000"
    os.environ.setdefault("MONXU_SECRET_KEY", "browser-edition-local-only-" + os.urandom(16).hex())
    if now_override:
        os.environ["MONXU_NOW"] = now_override
    _inline_threadpool()


def _inline_threadpool() -> None:
    """Run ``anyio.to_thread.run_sync`` calls inline: WebAssembly has no threads."""
    import anyio.to_thread

    async def run_sync(func, *args, abandon_on_cancel=False, cancellable=None, limiter=None):  # noqa: ARG001
        return func(*args)

    anyio.to_thread.run_sync = run_sync


def set_event_sink(fn) -> None:
    """``fn(json_text)`` is called for every bus message (all tenants of this local database)."""
    global _event_sink
    _event_sink = fn
    from .core import events

    bus = events.bus()
    original = bus._deliver

    def deliver(msg: dict[str, Any]) -> None:
        original(msg)
        if _event_sink is not None:
            try:
                _event_sink(json.dumps(msg, default=str))
            except Exception:  # noqa: BLE001 - a UI listener must never break the API
                pass

    bus._deliver = deliver


def init_database(seed: bool = True) -> dict[str, Any]:
    from .core.db import create_all

    create_all()
    if not seed:
        return {"status": "empty"}
    from .seed.demo import seed_demo

    return seed_demo()


def app():
    global _app
    if _app is None:
        from .api.app import create_app

        _app = create_app()
    return _app


async def handle(method: str, path: str, query: str, headers_json: str, body: bytes | None) -> dict[str, Any]:
    """Dispatch one HTTP request into the ASGI app and return ``{status, headers, body}``."""
    headers = [(k.lower().encode("latin-1"), v.encode("latin-1")) for k, v in json.loads(headers_json)]
    body = bytes(body or b"")
    if body and not any(k == b"content-length" for k, _ in headers):
        headers.append((b"content-length", str(len(body)).encode()))
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method.upper(),
        "scheme": "https",
        "path": path,
        "raw_path": path.encode(),
        "root_path": "",
        "query_string": (query or "").encode("latin-1"),
        "headers": headers,
        "client": ("127.0.0.1", 0),
        "server": ("monxuplan.local", 443),
    }
    sent = False

    async def receive():
        nonlocal sent
        if not sent:
            sent = True
            return {"type": "http.request", "body": body, "more_body": False}
        return {"type": "http.disconnect"}

    out: dict[str, Any] = {"status": 500, "headers": [], "body": bytearray()}

    async def send(message):
        if message["type"] == "http.response.start":
            out["status"] = message["status"]
            out["headers"] = [(k.decode("latin-1"), v.decode("latin-1")) for k, v in message.get("headers", [])]
        elif message["type"] == "http.response.body":
            out["body"] += message.get("body", b"")

    await app()(scope, receive, send)
    out["body"] = bytes(out["body"])
    return out


def queued_runs() -> int:
    from sqlalchemy import func, select

    from .core.db import new_session
    from .models import PlanningRun

    with new_session(None, "browser") as s:
        return s.scalar(select(func.count()).select_from(PlanningRun).where(PlanningRun.status == "QUEUED")) or 0


def pump_jobs(max_jobs: int = 1) -> int:
    """Execute queued planning runs (same claim/execute path as the server worker)."""
    from .services.planning import execute_run
    from .worker import claim_next

    done = 0
    while done < max_jobs:
        job = claim_next()
        if job is None:
            break
        execute_run(*job)
        done += 1
    return done
