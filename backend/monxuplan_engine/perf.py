"""Performance helpers for large problems.

A planning run allocates millions of small, long-lived objects (operations, placements, calendar
pieces). CPython's cyclic garbage collector would rescan all of them again and again while they are
being created — on a 200 000-operation problem that alone multiplies parsing and construction time
several times. The engine creates almost no reference cycles, so the collector is paused for the
duration of a run and a single collection runs at the end.
"""

from __future__ import annotations

import gc
import threading
from collections.abc import Iterator
from contextlib import contextmanager

_lock = threading.Lock()
_depth = 0


@contextmanager
def paused_gc() -> Iterator[None]:
    """Pause cyclic garbage collection (nesting- and thread-safe); collect once when the outermost
    block ends."""
    global _depth
    with _lock:
        _depth += 1
        if _depth == 1:
            was_enabled = gc.isenabled()
            gc.disable()
        else:
            was_enabled = None
    try:
        yield
    finally:
        with _lock:
            _depth -= 1
            last = _depth == 0
        if last:
            gc.collect()
            if was_enabled is not False:
                gc.enable()
