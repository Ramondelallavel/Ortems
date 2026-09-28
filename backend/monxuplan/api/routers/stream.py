"""Server-Sent Events stream (plan progress, plans, alerts, machine events)."""

from __future__ import annotations

import asyncio
import json
import queue

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse

from ...core.events import bus
from ...core.observability import ACTIVE_SSE
from ...services.context import Ctx
from ..deps import get_ctx

router = APIRouter(tags=["events"])


@router.get("/events/stream")
async def stream(request: Request, plant_id: str | None = None, ctx: Ctx = Depends(get_ctx)):
    sid, q = bus().subscribe(str(ctx.tenant_id))
    ACTIVE_SSE.inc()

    async def gen():
        try:
            yield "retry: 3000\n\n"
            idle = 0
            while True:
                if await request.is_disconnected():
                    break
                try:
                    msg = q.get_nowait()
                except queue.Empty:
                    await asyncio.sleep(0.25)
                    idle += 1
                    if idle >= 60:  # keep-alive every ~15 s
                        idle = 0
                        yield ": keep-alive\n\n"
                    continue
                if plant_id and msg.get("plant_id") not in (None, plant_id):
                    continue
                yield f"event: {msg['type']}\ndata: {json.dumps(msg, default=str)}\n\n"
        finally:
            bus().unsubscribe(sid)
            ACTIVE_SSE.dec()

    return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
