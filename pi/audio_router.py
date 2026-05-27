"""UDP audio routing service — streams 16kHz PCM frames to configured endpoint."""

from __future__ import annotations

import asyncio
import logging
import socket
from typing import Any

logger = logging.getLogger(__name__)

_VALID_MODES = frozenset({"ROVER_A32", "BUDS"})
_FRAME_BYTES = 640  # 320 samples × 2 bytes — 20 ms at 16kHz 16-bit mono


class _UDPProtocol(asyncio.DatagramProtocol):
    def __init__(self) -> None:
        self.transport: asyncio.DatagramTransport | None = None

    def connection_made(self, transport: asyncio.DatagramTransport) -> None:  # type: ignore[override]
        self.transport = transport

    def error_received(self, exc: Exception) -> None:
        logger.debug("audio_router: UDP error: %s", exc)

    def connection_lost(self, exc: Exception | None) -> None:
        self.transport = None


class AudioRouter:
    """Routes 16kHz PCM audio frames via UDP to ROVER_A32 or BUDS endpoint.

    Also broadcasts to connected /ws/audio WebSocket clients so the browser
    PWA can receive TTS audio (browsers cannot open raw UDP sockets).
    """

    def __init__(self, config: dict[str, Any]) -> None:
        self._a32_ip = str(config.get("a32_ip", "192.168.250.2"))
        self._flip6_ip = str(config.get("flip6_ip", "192.168.250.1"))
        self._port = int(config.get("udp_audio_port", 8085))
        self._mode = str(config.get("default_mode", "ROVER_A32"))
        self._audio_queue: asyncio.Queue[bytes] = asyncio.Queue()
        self._protocol: _UDPProtocol | None = None
        self._send_task: asyncio.Task[None] | None = None
        self._ws_audio_clients: set[Any] = set()
        logger.info("audio_router: init mode=%s target=%s:%d", self._mode, *self._target())

    @property
    def audio_queue(self) -> asyncio.Queue[bytes]:
        return self._audio_queue

    def _target(self) -> tuple[str, int]:
        ip = self._flip6_ip if self._mode == "BUDS" else self._a32_ip
        return (ip, self._port)

    async def start(self) -> None:
        loop = asyncio.get_running_loop()
        _, protocol = await loop.create_datagram_endpoint(
            _UDPProtocol,
            local_addr=("0.0.0.0", 0),
            family=socket.AF_INET,
        )
        self._protocol = protocol  # type: ignore[assignment]
        self._send_task = asyncio.create_task(self._send_loop(), name="audio-router-send")
        logger.info("audio_router: started, mode=%s target=%s:%d", self._mode, *self._target())

    async def stop(self) -> None:
        if self._send_task and not self._send_task.done():
            self._send_task.cancel()
            try:
                await self._send_task
            except asyncio.CancelledError:
                pass
        if self._protocol and self._protocol.transport:
            self._protocol.transport.close()
        self._protocol = None
        logger.info("audio_router: stopped")

    async def set_mode(self, mode: str) -> None:
        if mode not in _VALID_MODES:
            logger.warning("audio_router: unknown mode %r — ignored", mode)
            return
        self._mode = mode
        ip, port = self._target()
        logger.info("audio_router: mode changed to %s → target %s:%d", mode, ip, port)

    def get_mode(self) -> str:
        return self._mode

    def add_ws_audio_client(self, ws: Any) -> None:
        self._ws_audio_clients.add(ws)

    def remove_ws_audio_client(self, ws: Any) -> None:
        self._ws_audio_clients.discard(ws)

    async def _send_loop(self) -> None:
        while True:
            frame = await self._audio_queue.get()
            # UDP send to configured target
            if self._protocol and self._protocol.transport:
                try:
                    self._protocol.transport.sendto(frame, self._target())
                except Exception as exc:
                    logger.debug("audio_router: UDP send failed: %s", exc)
            # Broadcast to WebSocket audio clients (browser PWA)
            dead: set[Any] = set()
            for ws in self._ws_audio_clients:
                try:
                    await ws.send_bytes(frame)
                except Exception:
                    dead.add(ws)
            self._ws_audio_clients -= dead
            # Pace at 20 ms per frame to maintain real-time audio stream
            await asyncio.sleep(0.020)
