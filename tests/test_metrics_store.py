"""Tests for pi/metrics_store.py — SQLite time-series, no hardware.

Each test gets a fresh in-memory-ish DB by redirecting _DB_PATH to a temp file.
"""
from __future__ import annotations

import time
from pathlib import Path

import pytest

import metrics_store as _ms


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Redirect metrics DB to a temp file and reset the singleton connection."""
    db_path = tmp_path / "metrics.db"
    monkeypatch.setattr(_ms, "_DB_PATH", db_path)
    monkeypatch.setattr(_ms, "_db", None)
    monkeypatch.setattr(_ms, "_writes_enabled", True)
    yield
    # Close and reset the connection so it doesn't leak across tests
    conn = _ms._db
    if conn is not None:
        try:
            conn.close()
        except Exception:
            pass
    _ms._db = None


class TestWriteAndQuery:
    def test_write_then_query_returns_point(self):
        ts = int(time.time())
        _ms.write({"cpu_percent": 42.0}, ts=ts)
        rows = _ms.query("cpu_percent", since_ts=ts - 1)
        assert len(rows) == 1
        assert abs(rows[0]["v"] - 42.0) < 1e-6
        assert rows[0]["ts"] == ts

    def test_write_multiple_points_ordered(self):
        base = int(time.time())
        _ms.write({"temp": 50.0}, ts=base)
        _ms.write({"temp": 55.0}, ts=base + 1)
        _ms.write({"temp": 60.0}, ts=base + 2)
        rows = _ms.query("temp", since_ts=base)
        assert len(rows) == 3
        assert rows[0]["v"] == 50.0
        assert rows[2]["v"] == 60.0

    def test_query_excludes_before_since_ts(self):
        base = int(time.time())
        _ms.write({"x": 1.0}, ts=base - 100)
        _ms.write({"x": 2.0}, ts=base)
        rows = _ms.query("x", since_ts=base)
        assert len(rows) == 1
        assert rows[0]["v"] == 2.0

    def test_query_empty_name_returns_empty(self):
        rows = _ms.query("nonexistent", since_ts=0)
        assert rows == []

    def test_query_latest_returns_latest(self):
        base = int(time.time())
        _ms.write({"a": 10.0}, ts=base)
        _ms.write({"a": 20.0}, ts=base + 1)
        result = _ms.query_latest(["a"], since_ts=base)
        assert result["a"] == 20.0

    def test_query_latest_absent_returns_none(self):
        result = _ms.query_latest(["nonexistent"], since_ts=0)
        assert result["nonexistent"] is None

    def test_none_values_not_written(self):
        ts = int(time.time())
        _ms.write({"good": 1.0, "bad": None}, ts=ts)
        rows = _ms.query("bad", since_ts=ts - 1)
        assert rows == []
        rows_good = _ms.query("good", since_ts=ts - 1)
        assert len(rows_good) == 1


class TestWritesEnabledGate:
    def test_writes_disabled_is_noop(self):
        _ms.disable_writes()
        ts = int(time.time())
        _ms.write({"cpu": 99.0}, ts=ts)
        rows = _ms.query("cpu", since_ts=ts - 1)
        assert rows == []

    def test_enable_restores_writes(self):
        _ms.disable_writes()
        _ms.enable_writes()
        ts = int(time.time())
        _ms.write({"cpu": 50.0}, ts=ts)
        rows = _ms.query("cpu", since_ts=ts - 1)
        assert len(rows) == 1

    def test_writes_enabled_returns_true_by_default(self):
        assert _ms.writes_enabled() is True

    def test_writes_enabled_false_after_disable(self):
        _ms.disable_writes()
        assert _ms.writes_enabled() is False


class TestAvailableAndPurge:
    def test_available_lists_metric_names(self):
        ts = int(time.time())
        _ms.write({"alpha": 1.0, "beta": 2.0}, ts=ts)
        names = _ms.available()
        assert "alpha" in names
        assert "beta" in names

    def test_purge_removes_old_entries(self):
        now = int(time.time())
        old_ts = now - 100 * 86400  # 100 days ago
        _ms.write({"old_metric": 7.0}, ts=old_ts)
        _ms.write({"old_metric": 8.0}, ts=now)
        deleted = _ms.purge(retention_days=7)
        assert deleted >= 1
        rows = _ms.query("old_metric", since_ts=old_ts)
        assert all(r["ts"] >= now - 7 * 86400 for r in rows)

    def test_db_size_mb_returns_float(self):
        ts = int(time.time())
        _ms.write({"x": 1.0}, ts=ts)
        size = _ms.db_size_mb()
        assert isinstance(size, float)
        assert size >= 0.0
