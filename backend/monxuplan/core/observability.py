"""Structured logging, request context, Prometheus metrics and rate limiting."""

from __future__ import annotations

import contextvars
import json
import logging
import sys
import threading
import time
from collections import defaultdict, deque

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram

from .config import get_settings

request_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar("request_id", default=None)
tenant_var: contextvars.ContextVar[str | None] = contextvars.ContextVar("tenant", default=None)
user_var: contextvars.ContextVar[str | None] = contextvars.ContextVar("user", default=None)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        data = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            "request_id": request_id_var.get(),
            "tenant": tenant_var.get(),
            "user": user_var.get(),
        }
        for k in ("planning_run", "scenario", "plan", "error_id", "duration_s", "status", "extra_data"):
            v = getattr(record, k, None)
            if v is not None:
                data[k] = v
        if record.exc_info:
            data["exc"] = self.formatException(record.exc_info)
        return json.dumps(data, default=str)


def configure_logging() -> None:
    s = get_settings()
    root = logging.getLogger()
    if getattr(root, "_monxu_configured", False):
        return
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter() if s.log_json else logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    root.handlers = [handler]
    root.setLevel(s.log_level)
    root._monxu_configured = True  # type: ignore[attr-defined]


REGISTRY = CollectorRegistry()
HTTP_REQUESTS = Counter("monxu_http_requests_total", "HTTP requests", ["method", "route", "status"], registry=REGISTRY)
HTTP_LATENCY = Histogram("monxu_http_request_seconds", "HTTP latency", ["method", "route"], registry=REGISTRY, buckets=(0.01, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10))
PLANNING_RUNS = Counter("monxu_planning_runs_total", "Planning runs", ["kind", "status", "provider"], registry=REGISTRY)
SOLVER_SECONDS = Histogram("monxu_solver_seconds", "Planning run duration", ["provider"], registry=REGISTRY, buckets=(0.5, 1, 2, 5, 10, 30, 60, 120, 300, 600))
QUEUE_DEPTH = Gauge("monxu_planning_queue_depth", "Queued planning runs", registry=REGISTRY)
ACTIVE_SSE = Gauge("monxu_sse_clients", "Connected SSE clients", registry=REGISTRY)


class RateLimiter:
    """Sliding-window limiter per key (in-process; put a shared limiter at the ingress for clusters)."""

    def __init__(self) -> None:
        self._hits: dict[str, deque] = defaultdict(deque)
        self._lock = threading.Lock()

    def allow(self, key: str, limit: int, window_s: float = 60.0) -> bool:
        t = time.monotonic()
        with self._lock:
            q = self._hits[key]
            while q and t - q[0] > window_s:
                q.popleft()
            if len(q) >= limit:
                return False
            q.append(t)
            return True


limiter = RateLimiter()
