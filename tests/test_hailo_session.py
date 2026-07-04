"""Tests for pi/hailo_session.py — pure asyncio lock logic, no hardware."""
from __future__ import annotations

import asyncio

import pytest

import hailo_session as _hs


@pytest.fixture(autouse=True)
def _reset_session():
    """Reset module-level lock and holder between tests."""
    # Replace the module lock with a fresh one so tests don't interfere
    _hs._genai_lock = asyncio.Lock()
    _hs._current_holder = None
    yield
    _hs._current_holder = None
    # If lock is still held (e.g. test failed mid-way), release it
    if _hs._genai_lock.locked():
        try:
            _hs._genai_lock.release()
        except RuntimeError:
            pass


class TestGenAISession:
    async def test_acquire_sets_holder(self):
        async with _hs.genai_session("test-holder"):
            assert _hs.session_holder() == "test-holder"

    async def test_release_clears_holder(self):
        async with _hs.genai_session("holder"):
            pass
        assert _hs.session_holder() is None

    async def test_sequential_sessions(self):
        async with _hs.genai_session("first"):
            assert _hs.session_holder() == "first"
        async with _hs.genai_session("second"):
            assert _hs.session_holder() == "second"
        assert _hs.session_holder() is None

    async def test_session_holder_none_when_free(self):
        assert _hs.session_holder() is None

    async def test_lock_is_exclusive(self):
        """Second acquire must wait until first is released."""
        order: list[str] = []

        async def first():
            async with _hs.genai_session("A"):
                order.append("A-start")
                await asyncio.sleep(0.01)
                order.append("A-end")

        async def second():
            await asyncio.sleep(0.002)  # let A start first
            async with _hs.genai_session("B"):
                order.append("B-start")

        await asyncio.gather(first(), second())
        assert order == ["A-start", "A-end", "B-start"]

    async def test_exception_in_context_releases_lock(self):
        try:
            async with _hs.genai_session("erroring"):
                raise ValueError("oops")
        except ValueError:
            pass
        assert _hs.session_holder() is None
        assert not _hs._genai_lock.locked()
