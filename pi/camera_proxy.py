"""Proxy MJPEG camera stream from rover-camera (port 8081) through ROVER2 API."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator

import httpx
from fastapi import HTTPException
from fastapi.responses import Response, StreamingResponse

logger = logging.getLogger(__name__)

_CHUNK_SIZE = 65536


async def stream_response(stream_url: str) -> StreamingResponse:
    """Proxy the upstream MJPEG feed at ``stream_url``."""

    async def relay() -> AsyncIterator[bytes]:
        try:
            async with httpx.AsyncClient(timeout=None) as client:
                async with client.stream("GET", stream_url) as upstream:
                    if upstream.status_code != 200:
                        body = await upstream.aread()
                        detail = body.decode("utf-8", errors="replace")[:200]
                        logger.warning(
                            "Camera stream upstream %s: %s", upstream.status_code, detail
                        )
                        return
                    async for chunk in upstream.aiter_bytes(chunk_size=_CHUNK_SIZE):
                        yield chunk
        except httpx.HTTPError as exc:
            logger.warning("Camera stream relay failed: %s", exc)

    return StreamingResponse(
        relay(),
        media_type="multipart/x-mixed-replace; boundary=frame",
    )


async def snapshot_response(snapshot_url: str) -> Response:
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            upstream = await client.get(snapshot_url)
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=503, detail=f"camera unreachable: {exc}") from exc
    if upstream.status_code != 200:
        raise HTTPException(status_code=503, detail="camera snapshot unavailable")
    return Response(
        content=upstream.content,
        media_type=upstream.headers.get("content-type", "image/jpeg"),
    )


async def check_camera_up(health_url: str) -> None:
    """Raise HTTP 503 if rover-camera is not reachable."""
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            upstream = await client.get(health_url)
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=503, detail=f"camera unreachable: {exc}") from exc
    # 200 = frames ready; 503 no_frame = server up but warming up — stream may still work.
    if upstream.status_code not in (200, 503):
        raise HTTPException(
            status_code=503,
            detail=f"camera not available ({upstream.status_code})",
        )
