"""ROVER2 FastAPI server — motors, gripper, ultrasonic, static D-pad UI."""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any

import yaml
from fastapi import FastAPI, HTTPException, Request, WebSocket
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

from arm_control import arm_pwm
from body_tracker import BodyTracker
from megapi import MegaPiBridge

try:
    from camera_proxy import check_camera_up, snapshot_response, stream_response

    _CAMERA_PROXY = True
except ImportError:
    _CAMERA_PROXY = False
from safety import SafetyMonitor
from ws_control import ControlHub, build_telemetry

logger = logging.getLogger(__name__)
_START = time.monotonic()
_STATIC = Path(__file__).parent / "web" / "static"

# (left_sign, right_sign) before firmware negation — tuned for Ultimate 2.0 wiring.
# Override in config.yaml → drive.direction_map if your chassis differs.
_DIRECTIONS: dict[str, tuple[int, int]] = {
    "forward": (1, -1),
    "back": (-1, 1),
    "left": (1, 1),
    "right": (-1, -1),
    "stop": (0, 0),
}


def load_config(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    if not isinstance(data, dict):
        raise ValueError("config.yaml must be a mapping")
    return data


def _scale(speed_fraction: float, max_speed: int) -> int:
    return int(max(-max_speed, min(max_speed, round(speed_fraction * max_speed))))


def _public_ultrasonic_cm(raw: int | None, max_age_s: float = 2.0, age_s: float | None = None) -> int | None:
    """Map bridge cache to API: null if stale, no echo, or never read."""
    if raw is None or age_s is None or age_s > max_age_s:
        return None
    if raw < 0:
        return None
    return raw


def _directions_from_config(config: dict[str, Any]) -> dict[str, tuple[int, int]]:
    custom = config.get("drive", {}).get("direction_map")
    if not isinstance(custom, dict):
        return dict(_DIRECTIONS)
    merged = dict(_DIRECTIONS)
    for key, pair in custom.items():
        if not isinstance(key, str) or not isinstance(pair, (list, tuple)) or len(pair) != 2:
            continue
        merged[key.lower()] = (int(pair[0]), int(pair[1]))
    return merged


def create_app(
    megapi: MegaPiBridge,
    config: dict[str, Any],
    safety_monitor: SafetyMonitor | None = None,
    body_tracker: BodyTracker | None = None,
) -> FastAPI:
    drive_cfg = config.get("drive", {})
    default_speed = int(drive_cfg.get("default_speed", 120))
    max_speed = int(drive_cfg.get("max_speed", 255))
    directions = _directions_from_config(config)
    ws_cfg = config.get("websocket", {})
    telemetry_interval_s = float(ws_cfg.get("telemetry_interval_s", 0.4))
    cam_cfg = config.get("camera", {})
    camera_stream_url = str(cam_cfg.get("upstream_url", "http://127.0.0.1:8081/stream"))
    camera_snapshot_url = str(cam_cfg.get("snapshot_url", "http://127.0.0.1:8081/snapshot"))
    camera_health_url = str(cam_cfg.get("health_url", "http://127.0.0.1:8081/health"))

    hub = ControlHub(
        megapi=megapi,
        directions=directions,
        max_speed=max_speed,
        safety_monitor=safety_monitor,
        start_monotonic=_START,
        public_ultrasonic_cm=_public_ultrasonic_cm,
        telemetry_interval_s=telemetry_interval_s,
        body_tracker=body_tracker,
        config=config,
    )

    app = FastAPI(title="ROVER2", version="2.0.0")
    app.mount("/static", StaticFiles(directory=_STATIC), name="static")

    @app.websocket("/ws")
    async def websocket_control(websocket: WebSocket) -> None:
        await websocket.accept()
        await hub.serve(websocket)

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(
            _STATIC / "index.html",
            headers={"Cache-Control": "no-cache"},
        )

    if _CAMERA_PROXY:

        @app.get("/stream")
        async def camera_stream() -> StreamingResponse:
            """MJPEG proxy — same feed as rover-camera on port 8081."""
            await check_camera_up(camera_health_url)
            return await stream_response(camera_stream_url)

        @app.get("/snapshot")
        async def camera_snapshot() -> Response:
            return await snapshot_response(camera_snapshot_url)

        @app.get("/api/camera/health")
        async def camera_health() -> JSONResponse:
            import httpx

            try:
                async with httpx.AsyncClient(timeout=3.0) as client:
                    upstream = await client.get(camera_health_url)
            except httpx.HTTPError as exc:
                return JSONResponse({"status": "error", "detail": str(exc)[:120]})
            ok = upstream.status_code == 200 and upstream.text.strip() == "ok"
            return JSONResponse({
                "status": "ok" if ok else "no_frame",
                "upstream": camera_health_url,
                "upstream_code": upstream.status_code,
            })
    else:

        @app.get("/stream")
        async def camera_stream_unavailable() -> JSONResponse:
            return JSONResponse(
                {"detail": "camera proxy requires httpx (pip install httpx)"},
                status_code=503,
            )

        @app.get("/api/camera/health")
        async def camera_health_unavailable() -> JSONResponse:
            return JSONResponse({"status": "error", "detail": "httpx not installed"})

    @app.get("/api/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/status")
    async def status() -> JSONResponse:
        payload = build_telemetry(
            megapi, safety_monitor, _START, _public_ultrasonic_cm, body_tracker
        )
        payload.pop("type", None)
        return JSONResponse(payload)

    @app.post("/api/tracking")
    async def tracking(request: Request) -> JSONResponse:
        if body_tracker is None:
            raise HTTPException(status_code=503, detail="tracking not configured")
        try:
            body = await request.json()
        except Exception as exc:
            raise HTTPException(status_code=400, detail="Invalid JSON") from exc
        enabled = bool(body.get("enabled", False))
        if enabled and not body_tracker.available:
            raise HTTPException(status_code=503, detail="Hailo not available")
        body_tracker.set_enabled(enabled)
        if not enabled:
            try:
                megapi.stop_motors()
            except RuntimeError as exc:
                raise HTTPException(status_code=503, detail=str(exc)) from exc
        return JSONResponse({"status": "ok", "enabled": body_tracker.enabled})

    @app.post("/api/drive")
    async def drive(request: Request) -> JSONResponse:
        try:
            body = await request.json()
        except Exception as exc:
            raise HTTPException(status_code=400, detail="Invalid JSON") from exc
        direction = str(body.get("direction", "stop")).lower()
        speed_frac = float(body.get("speed", 1.0))
        if direction not in directions:
            raise HTTPException(status_code=400, detail=f"Unknown direction: {direction}")
        mult_l, mult_r = directions[direction]
        speed = _scale(speed_frac, max_speed if direction != "stop" else 0)
        left = mult_l * speed
        right = mult_r * speed
        try:
            if direction == "stop":
                megapi.stop_motors()
            else:
                megapi.drive(left, right)
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        return JSONResponse({"status": "ok", "direction": direction, "l": left, "r": right})

    @app.post("/api/stop")
    async def stop() -> JSONResponse:
        try:
            megapi.stop_motors()
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        return JSONResponse({"status": "ok"})

    @app.post("/api/grip")
    async def grip(request: Request) -> JSONResponse:
        try:
            body = await request.json()
        except Exception as exc:
            raise HTTPException(status_code=400, detail="Invalid JSON") from exc
        action = str(body.get("action", "")).lower()
        if action not in ("open", "close"):
            raise HTTPException(status_code=400, detail="action must be open or close")
        try:
            megapi.grip(action)
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        return JSONResponse({"status": "ok", "action": action})

    @app.post("/api/arm")
    async def arm(request: Request) -> JSONResponse:
        try:
            body = await request.json()
        except Exception as exc:
            raise HTTPException(status_code=400, detail="Invalid JSON") from exc
        pulse_action = str(body.get("action", "")).lower()
        if pulse_action in ("up", "down") and "direction" not in body:
            try:
                megapi.arm(action=pulse_action)
            except RuntimeError as exc:
                raise HTTPException(status_code=503, detail=str(exc)) from exc
            return JSONResponse({"status": "ok", "mode": "pulse", "action": pulse_action})
        direction = str(body.get("direction", "stop")).lower()
        speed_frac = float(body.get("speed", 1.0))
        try:
            pwm = arm_pwm(direction, speed_frac, config)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        try:
            megapi.arm(pwm)
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        return JSONResponse({"status": "ok", "mode": "speed", "direction": direction, "pwm": pwm})

    @app.get("/api/ultrasonic")
    async def ultrasonic() -> JSONResponse:
        try:
            megapi.request_ultrasonic(wait_s=0.6)
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        return JSONResponse({
            "status": "ok",
            "ultrasonic_cm": _public_ultrasonic_cm(
                megapi.last_ultrasonic_cm, age_s=megapi.last_ultrasonic_age_s
            ),
            "ultrasonic_age_s": megapi.last_ultrasonic_age_s,
        })

    @app.get("/api/firmware/tools")
    async def firmware_tools() -> JSONResponse:
        from firmware_flash import tools_available

        ok, detail = tools_available()
        return JSONResponse({"available": ok, "detail": detail})

    @app.post("/api/firmware/upload")
    async def firmware_upload(request: Request) -> JSONResponse:
        """Save compiled .hex to /tmp/rover2_firmware.hex (multipart or raw body)."""
        from firmware_flash import DEFAULT_HEX

        content_type = request.headers.get("content-type", "")
        if "multipart/form-data" in content_type:
            form = await request.form()
            upload = form.get("firmware") or form.get("file")
            if upload is None:
                raise HTTPException(status_code=400, detail="Missing firmware file")
            data = await upload.read()  # type: ignore[union-attr]
        else:
            data = await request.body()
        if not data:
            raise HTTPException(status_code=400, detail="Empty firmware payload")
        DEFAULT_HEX.write_bytes(data)
        return JSONResponse({"status": "ok", "path": str(DEFAULT_HEX), "bytes": len(data)})

    @app.post("/api/firmware/flash")
    async def firmware_flash() -> JSONResponse:
        """Flash /tmp/rover2_firmware.hex via avrdude (background job restarts rover2-api)."""
        from firmware_flash import flash_firmware_async

        try:
            result = flash_firmware_async()
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        return JSONResponse(result)

    return app
