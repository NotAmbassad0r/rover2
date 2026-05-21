"""VLM — Qwen2-VL-2B-Instruct on Hailo-10H via hailo_platform.genai.

Lazy-loaded on first describe() call. Shares the Hailo chip with
body_tracker via the same group_id + ROUND_ROBIN scheduler.
"""
from __future__ import annotations

import io
import logging
import threading
import urllib.request
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_DEFAULT_HEF = "/opt/rover/models/Qwen2-VL-2B-Instruct.hef"
_CAMERA_SNAPSHOT_URL = "http://127.0.0.1:8081/snapshot"
_FETCH_TIMEOUT_S = 3.0


class VLMEngine:
    """Qwen2-VL-2B-Instruct on Hailo-10H.

    Not loaded at startup. First call to describe() triggers _load(),
    which opens a VDevice with the same group_id as body_tracker so
    Hailo's round-robin scheduler time-multiplexes both models.
    """

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        cfg = (config or {}).get("vlm", {})
        self._hef_path = str(cfg.get("hef_path", _DEFAULT_HEF))
        self._group_id = str(cfg.get("hailo_group_id", "rover2"))
        self._camera_url = str(cfg.get("camera_url", _CAMERA_SNAPSHOT_URL))
        self._enabled = bool(cfg.get("enabled", True))
        self._max_tokens = int(cfg.get("max_tokens", 128))

        self._lock = threading.Lock()
        self._vdevice: Any = None
        self._vlm: Any = None
        self._available = False
        self._loaded = False
        self._load_error = ""

    # ── Public API ──────────────────────────────────────────────────────────

    async def describe(
        self,
        image_bytes: bytes,
        prompt: str = "Describe briefly what you see.",
    ) -> str | None:
        """Run VLM inference on image_bytes. Lazy-loads on first call."""
        import asyncio

        return await asyncio.get_event_loop().run_in_executor(
            None, self._ensure_and_generate, image_bytes, prompt
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
        with self._lock:
            return self._available

    @property
    def loaded(self) -> bool:
        with self._lock:
            return self._loaded

    def get_status(self) -> dict[str, Any]:
        with self._lock:
            if not self._enabled:
                return {"status": "skip", "detail": "disabled via config"}
            if not self._loaded:
                return {"status": "idle", "detail": "lazy — not yet loaded", "available": False}
            if self._available:
                return {"status": "ok", "available": True, "hef": self._hef_path,
                        "group_id": self._group_id}
            return {"status": "error", "available": False, "detail": self._load_error}

    # ── Internal ────────────────────────────────────────────────────────────

    def _ensure_and_generate(self, image_bytes: bytes, prompt: str) -> str | None:
        with self._lock:
            if not self._loaded:
                self._load()
            avail = self._available
        if not avail:
            return None
        return self._generate_sync(image_bytes, prompt)

    def _load(self) -> None:
        """Load VDevice + VLM model. Must be called with self._lock held."""
        self._loaded = True
        if not self._enabled:
            self._load_error = "disabled via config"
            return
        if not Path(self._hef_path).exists():
            self._load_error = f"HEF not found: {self._hef_path}"
            logger.warning("VLMEngine: %s", self._load_error)
            return
        try:
            from hailo_platform import HailoSchedulingAlgorithm, VDevice  # type: ignore[import]
            from hailo_platform.genai import VLM  # type: ignore[import]

            params = VDevice.create_params()
            params.group_id = self._group_id
            params.scheduling_algorithm = HailoSchedulingAlgorithm.ROUND_ROBIN
            self._vdevice = VDevice(params)
            self._vlm = VLM(self._vdevice, self._hef_path)
            self._available = True
            logger.info("VLMEngine: loaded %s (group=%s)", self._hef_path, self._group_id)
        except Exception as exc:
            self._load_error = str(exc)[:120]
            logger.warning("VLMEngine: load failed: %s", self._load_error)

    def _generate_sync(self, image_bytes: bytes, prompt: str) -> str | None:
        """Blocking inference — called from a thread executor."""
        try:
            import numpy as np  # type: ignore[import]
            from PIL import Image  # type: ignore[import]

            img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
            expected = getattr(self._vlm, "input_frame_shape", None)
            h, w = (expected[0], expected[1]) if expected and len(expected) >= 2 else (336, 336)
            img = img.resize((w, h))
            frame = np.array(img, dtype=np.uint8)

            messages = [{"role": "user", "content": [
                {"type": "text", "text": prompt},
                {"type": "image"},
            ]}]
            # Serialize VLM calls — one inference at a time.
            with self._lock:
                self._vlm.clear_context()
                response: str = self._vlm.generate_all(
                    messages,
                    frames=[frame],
                    max_generated_tokens=self._max_tokens,
                    temperature=0.1,
                    timeout_ms=30_000,
                )
            logger.info("VLMEngine: %d chars generated", len(response))
            return response.strip() or None
        except Exception as exc:
            logger.warning("VLMEngine: inference failed: %s", exc)
            return None
