"""Event bus for real-time updates (SSE) and internal reactions.

* ``InMemoryBus`` — single process (development, tests, the in-process worker).
* ``RedisBus`` — multi-process fan-out via Redis pub/sub when ``MONXU_REDIS_URL`` is set.

Messages are small JSON dicts ``{type, tenant_id, plant_id?, data, at}``; subscribers receive only
their tenant's messages.
"""

from __future__ import annotations

import json
import logging
import queue
import threading
from typing import Any

from .clock import now
from .config import get_settings

log = logging.getLogger("monxuplan.events")


class InMemoryBus:
    def __init__(self) -> None:
        self._subs: dict[int, tuple[str, queue.Queue]] = {}
        self._lock = threading.Lock()
        self._next = 0

    def publish(self, type_: str, tenant_id: str, data: dict[str, Any] | None = None, plant_id: str | None = None) -> None:
        msg = {"type": type_, "tenant_id": str(tenant_id), "plant_id": plant_id, "data": data or {}, "at": now().isoformat()}
        self._deliver(msg)

    def _deliver(self, msg: dict[str, Any]) -> None:
        with self._lock:
            subs = list(self._subs.values())
        for tid, q in subs:
            if tid == msg["tenant_id"]:
                try:
                    q.put_nowait(msg)
                except queue.Full:  # slow consumer: drop (clients re-sync by polling)
                    pass

    def subscribe(self, tenant_id: str) -> tuple[int, queue.Queue]:
        q: queue.Queue = queue.Queue(maxsize=1000)
        with self._lock:
            self._next += 1
            sid = self._next
            self._subs[sid] = (str(tenant_id), q)
        return sid, q

    def unsubscribe(self, sid: int) -> None:
        with self._lock:
            self._subs.pop(sid, None)


class RedisBus(InMemoryBus):
    CHANNEL = "monxuplan:events"

    def __init__(self, url: str) -> None:
        super().__init__()
        import redis

        self._r = redis.Redis.from_url(url)
        self._thread = threading.Thread(target=self._listen, daemon=True, name="monxu-redis-bus")
        self._thread.start()

    def publish(self, type_: str, tenant_id: str, data: dict[str, Any] | None = None, plant_id: str | None = None) -> None:
        msg = {"type": type_, "tenant_id": str(tenant_id), "plant_id": plant_id, "data": data or {}, "at": now().isoformat()}
        try:
            self._r.publish(self.CHANNEL, json.dumps(msg, default=str))
        except Exception as exc:  # noqa: BLE001
            log.warning("redis publish failed, delivering locally: %s", exc)
            self._deliver(msg)

    def _listen(self) -> None:  # pragma: no cover - needs a Redis server
        while True:
            try:
                ps = self._r.pubsub()
                ps.subscribe(self.CHANNEL)
                for item in ps.listen():
                    if item.get("type") == "message":
                        self._deliver(json.loads(item["data"]))
            except Exception as exc:  # noqa: BLE001
                log.warning("redis subscription lost: %s", exc)
                threading.Event().wait(2)


_bus: InMemoryBus | None = None
_bus_lock = threading.Lock()


def bus() -> InMemoryBus:
    global _bus
    with _bus_lock:
        if _bus is None:
            url = get_settings().redis_url
            if url:
                try:
                    _bus = RedisBus(url)
                except Exception as exc:  # noqa: BLE001
                    log.warning("Redis unavailable (%s); using in-memory event bus", exc)
                    _bus = InMemoryBus()
            else:
                _bus = InMemoryBus()
        return _bus
