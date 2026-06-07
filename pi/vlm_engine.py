"""VLM — Qwen2-VL-2B-Instruct on Hailo-10H via hailo_platform.genai.

Session lifecycle: open on every describe() call, release after inference.
Serialised with LLM via hailo_session.genai_session lock (HailoRT 5.2.0
firmware only allows one GenAI session at a time).

Cooldown: a minimum gap (vlm.cooldown_s, default 95s) is enforced between
HEF reloads. Rapid sequential loads cause HAILO_SHUTDOWN_EVENT_SIGNALED(57)
on HailoRT 5.2.0 (issue #28). describe() awaits asyncio.sleep() during the
cooldown — zero CPU, LLM path unaffected.
"""
from __future__ import annotations

import asyncio
import io
import logging
import time
import urllib.request
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_DEFAULT_HEF = "/opt/rover/models/Qwen2-VL-2B-Instruct.hef"
_CAMERA_SNAPSHOT_URL = "http://127.0.0.1:8081/snapshot"
_FETCH_TIMEOUT_S = 3.0
_DEFAULT_COOLDOWN_S = 95.0


class VLMEngine:
    """Qwen2-VL-2B-Instruct on Hailo-10H.

    Per-request GenAI session: the VDevice+VLM is opened, used, and released
    for every describe() call. No persistent session is held between calls so
    the Hailo LLM engine can use the chip between VLM requests.

    A 95s cooldown is enforced between calls to prevent the Hailo device crash
    caused by rapid sequential VLM HEF reloads (issue #28).
    """

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        cfg = (config or {}).get("vlm", {})
        self._hef_path = str(cfg.get("hef_path", _DEFAULT_HEF))
        self._group_id = str(cfg.get("hailo_group_id", "rover2"))
        self._camera_url = str(cfg.get("camera_url", _CAMERA_SNAPSHOT_URL))
        self._enabled = bool(cfg.get("enabled", True))
        self._max_tokens = int(cfg.get("max_tokens", 128))
        self._cooldown_s = float(cfg.get("cooldown_s", _DEFAULT_COOLDOWN_S))

        self._available = False
        self._load_error = ""
        self._last_session_release_ts: float = 0.0  # monotonic; 0 = never released
        self._last_describe_wait_s: float = 0.0

    # ── Public API ──────────────────────────────────────────────────────────

    def cooldown_remaining(self) -> float:
        """Seconds until the next describe() call can open a new VLM session. 0 if ready."""
        if self._last_session_release_ts == 0.0:
            return 0.0
        elapsed = time.monotonic() - self._last_session_release_ts
        remaining = self._cooldown_s - elapsed
        return max(0.0, remaining)

    async def describe(
        self,
        image_bytes: bytes,
        prompt: str = "Describe briefly what you see.",
    ) -> str | None:
        """Run VLM inference. Waits out any cooldown, then opens a GenAI session."""
        if not self._enabled:
            return None
        wait_start = time.monotonic()
        await self._wait_for_cooldown()
        self._last_describe_wait_s = time.monotonic() - wait_start

        from hailo_session import genai_session
        async with genai_session("vlm"):
            return await asyncio.get_event_loop().run_in_executor(
                None, self._open_and_generate, image_bytes, prompt
            )

    def fetch_camera_frame(self) -> bytes | None:
        """Fetch a JPEG snapshot from the camera service (blocking)."""
        try:
            with urllib.request.urlopen(self._camera_url, timeout=_FETCH_TIMEOUT_S) as resp:
                if resp.status == 200:
                    return resp.read()
                logger.warning("VLMEngine: snapshot HTTP %d", resp.status)
        except Exception as exc:
            logger.warning("VLMEngine: camera fetch: %s", exc)
        return None

    @property
    def available(self) -> bool:
        return self._available

    @property
    def loaded(self) -> bool:
        return self._available

    def get_status(self) -> dict[str, Any]:
        cooldown = self.cooldown_remaining()
        base: dict[str, Any] = {
            "cooldown_remaining_s": round(cooldown, 1),
            "last_describe_wait_s": round(self._last_describe_wait_s, 1),
        }
        if not self._enabled:
            return {"status": "skip", "detail": "disabled via config", **base}
        if not Path(self._hef_path).exists():
            return {"status": "error", "available": False,
                    "detail": f"HEF missing: {self._hef_path}", **base}
        if self._available:
            return {"status": "ok", "available": True, "hef": self._hef_path,
                    "group_id": self._group_id, "mode": "per-request", **base}
        if self._load_error:
            return {"status": "error", "available": False, "detail": self._load_error, **base}
        return {"status": "idle", "detail": "per-request — opens on demand",
                "available": False, **base}

    # ── Internal ────────────────────────────────────────────────────────────

    async def _wait_for_cooldown(self) -> None:
        remaining = self.cooldown_remaining()
        if remaining > 0:
            logger.info("vlm: cooldown active — waiting %.1fs before re-loading", remaining)
            await asyncio.sleep(remaining)

    def _open_and_generate(self, image_bytes: bytes, prompt: str) -> str | None:
        """Open VLM session, run inference, release. Called from executor under genai_session lock."""
        if not Path(self._hef_path).exists():
            self._load_error = f"HEF not found: {self._hef_path}"
            logger.warning("VLMEngine: %s", self._load_error)
            return None
        result = None
        try:
            from hailo_platform import HailoSchedulingAlgorithm, VDevice  # type: ignore[import]
            from hailo_platform.genai import VLM  # type: ignore[import]

            params = VDevice.create_params()
            params.group_id = self._group_id
            params.scheduling_algorithm = HailoSchedulingAlgorithm.ROUND_ROBIN
            vdevice = VDevice(params)
            vlm = VLM(vdevice, self._hef_path)
            self._available = True
            logger.info("VLMEngine: session opened (group=%s)", self._group_id)

            result = self._run_inference(vlm, image_bytes, prompt)

            vlm.release()
            logger.info("VLMEngine: session released")
        except Exception as exc:
            self._load_error = str(exc)[:120]
            self._available = False
            logger.warning("VLMEngine: open_and_generate failed: %s", self._load_error)
        finally:
            self._last_session_release_ts = time.monotonic()
        return result

    def _run_inference(self, vlm: Any, image_bytes: bytes, prompt: str) -> str | None:
        """Run VLM inference on open session."""
        try:
            import numpy as np  # type: ignore[import]
            from PIL import Image  # type: ignore[import]

            img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
            shape = vlm.input_frame_shape()
            h, w = shape[0], shape[1]
            img = img.resize((w, h))
            frame = np.array(img, dtype=np.uint8)

            messages = [{"role": "user", "content": [
                {"type": "text", "text": prompt},
                {"type": "image"},
            ]}]
            vlm.clear_context()
            response: str = vlm.generate_all(
                messages,
                frames=[frame],
                max_generated_tokens=self._max_tokens,
                temperature=0.1,
                timeout_ms=30_000,
            )
            logger.info("VLMEngine: %d chars generated", len(response))
            cleaned = response.replace("<|im_end|>", "").strip()
            return cleaned or None
        except Exception as exc:
            logger.warning("VLMEngine: inference failed: %s", exc)
            return None
