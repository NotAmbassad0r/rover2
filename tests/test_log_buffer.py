"""Tests for pi/log_buffer.py — ring log handler, no hardware."""
from __future__ import annotations

import logging

import pytest

import log_buffer as _lb


@pytest.fixture(autouse=True)
def _fresh_handler():
    """Give each test a clean RingLogHandler so tests don't share state."""
    handler = _lb.RingLogHandler(maxlen=5)
    yield handler
    handler.clear()


class TestRingLogHandler:
    def test_emit_adds_record(self, _fresh_handler):
        h = _fresh_handler
        logger = logging.getLogger("test.emit")
        logger.addHandler(h)
        logger.setLevel(logging.DEBUG)
        logger.info("hello world")
        logger.removeHandler(h)
        records = h.get_records()
        assert any("hello world" in r["message"] for r in records)

    def test_record_has_required_fields(self, _fresh_handler):
        h = _fresh_handler
        logger = logging.getLogger("test.fields")
        logger.addHandler(h)
        logger.setLevel(logging.DEBUG)
        logger.warning("field-check")
        logger.removeHandler(h)
        rec = h.get_records()[-1]
        assert "timestamp" in rec
        assert "level" in rec
        assert "logger" in rec
        assert "message" in rec

    def test_clear_empties_buffer(self, _fresh_handler):
        h = _fresh_handler
        logger = logging.getLogger("test.clear")
        logger.addHandler(h)
        logger.setLevel(logging.DEBUG)
        logger.info("a")
        logger.info("b")
        logger.removeHandler(h)
        h.clear()
        assert h.get_records() == []

    def test_maxlen_enforced(self):
        h = _lb.RingLogHandler(maxlen=3)
        logger = logging.getLogger("test.maxlen")
        logger.addHandler(h)
        logger.setLevel(logging.DEBUG)
        for i in range(5):
            logger.info(f"msg {i}")
        logger.removeHandler(h)
        records = h.get_records()
        assert len(records) == 3
        assert "msg 4" in records[-1]["message"]

    def test_get_records_returns_copy(self, _fresh_handler):
        h = _fresh_handler
        r1 = h.get_records()
        r1.append({"timestamp": "x", "level": "X", "logger": "x", "message": "x"})
        assert len(h.get_records()) == 0  # internal buffer unchanged


class TestInstallAndGetHandler:
    def test_get_log_handler_returns_singleton(self):
        h1 = _lb.get_log_handler()
        h2 = _lb.get_log_handler()
        assert h1 is h2

    def test_install_is_idempotent(self):
        root = logging.getLogger()
        before = root.handlers.count(_lb.get_log_handler())
        _lb.install_log_ring_buffer()
        _lb.install_log_ring_buffer()
        after = root.handlers.count(_lb.get_log_handler())
        assert after == max(before, 1)
