"""Shared Hailo GenAI session lock.

HailoRT 5.2.0 firmware permits only one GenAI session open at a time on the
Hailo-10H. hailo_platform.genai.{LLM,VLM} sessions must be serialised.

Usage:
    from hailo_session import genai_session
    async with genai_session("llm"):
        vd = VDevice(params)
        llm = LLM(vd, hef_path)
        result = llm.generate_all(messages)
        llm.release()
"""
from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger(__name__)

_genai_lock = asyncio.Lock()
_current_holder: str | None = None


class _GenAISession:
    __slots__ = ("_holder",)

    def __init__(self, holder: str) -> None:
        self._holder = holder

    async def __aenter__(self) -> "_GenAISession":
        global _current_holder
        if _genai_lock.locked():
            logger.debug("hailo_session: %s waiting (held by %s)", self._holder, _current_holder)
        await _genai_lock.acquire()
        _current_holder = self._holder
        logger.info("hailo_session: %s acquired GenAI session", self._holder)
        return self

    async def __aexit__(self, *_: object) -> None:
        global _current_holder
        prev = _current_holder
        _current_holder = None
        _genai_lock.release()
        logger.info("hailo_session: %s released GenAI session", prev)


def genai_session(holder: str) -> _GenAISession:
    """Exclusive Hailo GenAI session context manager."""
    return _GenAISession(holder)


def session_holder() -> str | None:
    """Return the current GenAI session holder, or None if the lock is free."""
    return _current_holder


