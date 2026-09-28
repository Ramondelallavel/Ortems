"""JSON responses for large read models.

FastAPI re-encodes a returned dict value by value before serialising it — seconds for a Gantt
window of tens of thousands of operations. Endpoints that return big documents build them from
plain JSON-able data already and return :func:`fast_json`, serialised in one pass by pydantic-core.
"""

from __future__ import annotations

from typing import Any

from fastapi.responses import Response
from pydantic_core import to_json


class FastJSONResponse(Response):
    media_type = "application/json"

    def render(self, content: Any) -> bytes:
        return to_json(content)


def fast_json(content: Any) -> FastJSONResponse:
    return FastJSONResponse(content)
