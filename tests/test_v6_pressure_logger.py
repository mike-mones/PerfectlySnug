"""Tests for appdaemon/v6_pressure_logger.py."""
from __future__ import annotations

import importlib.util
import sys
import time
import types
from pathlib import Path

import pytest


# ── Stub hassapi ────────────────────────────────────────────────────────
class _FakeHass:
    def get_state(self, *a, **kw): return None
    def call_service(self, *a, **kw): pass
    def log(self, *a, **kw): pass
    def listen_state(self, *a, **kw): pass
    def run_every(self, *a, **kw): pass


fake = types.ModuleType("hassapi")
fake.Hass = _FakeHass
sys.modules.setdefault("hassapi", fake)

_PATH = Path(__file__).parent.parent / "appdaemon" / "v6_pressure_logger.py"
_spec = importlib.util.spec_from_file_location("v6_pressure_logger", _PATH)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
V6PressureLogger = _mod.V6PressureLogger


class _FakeCursor:
    def __init__(self, parent):
        self.parent = parent
        self.last = None
    def execute(self, q, params=None):
        if "SELECT 1" in q:
            return
        self.last = (q, params)
        self.parent.inserts.append((q, params))
    def fetchone(self): return (1,)
    def close(self): pass


class _FakeConn:
    def __init__(self):
        self.inserts = []
        self.committed = 0
    def cursor(self): return _FakeCursor(self)
    def commit(self): self.committed += 1
    def close(self): pass


def _make_logger(state_map=None, conn=None):
    """Construct a V6PressureLogger without invoking initialize()."""
    obj = V6PressureLogger.__new__(V6PressureLogger)
    obj._pg_host = "x"
    obj._pg_conn = conn
    obj._readings = {"left": __import__("collections").deque(),
                     "right": __import__("collections").deque()}
    obj._state_map = state_map or {}
    # patch get_state to consult state_map
    obj.get_state = lambda eid, **kw: obj._state_map.get(eid)
    obj.call_service = lambda *a, **kw: None
    obj.log = lambda *a, **kw: None
    return obj


def test_aggregate_sliding_window_correct():
    log = _make_logger()
    base = time.monotonic()
    # readings: 10, 12, 11, 14, 14 → deltas 2, 1, 3, 0 → sum=6, max=3
    log._readings["left"].extend([
        (base, 10.0), (base + 5, 12.0), (base + 10, 11.0),
        (base + 15, 14.0), (base + 20, 14.0),
    ])
    s, m = log._aggregate("left")
    assert s == pytest.approx(6.0)
    assert m == pytest.approx(3.0)


def test_skip_when_sample_count_zero():
    conn = _FakeConn()
    log = _make_logger(state_map={
        _mod.SHADOW_FLAG: "on",
        _mod.OCCUPIED_ENTITIES["left"]: "on",
        _mod.OCCUPIED_ENTITIES["right"]: "on",
    }, conn=conn)
    log._tick_inner()
    assert conn.inserts == []


def test_skip_when_shadow_logging_off():
    conn = _FakeConn()
    log = _make_logger(state_map={_mod.SHADOW_FLAG: "off"}, conn=conn)
    base = time.monotonic()
    log._readings["left"].extend([(base, 10.0), (base + 5, 12.0)])
    log._tick_inner()
    assert conn.inserts == []


def test_insert_uses_correct_columns_and_values():
    conn = _FakeConn()
    log = _make_logger(state_map={
        _mod.SHADOW_FLAG: "on",
        _mod.OCCUPIED_ENTITIES["left"]: "on",
        _mod.OCCUPIED_ENTITIES["right"]: "off",
    }, conn=conn)
    base = time.monotonic()
    log._readings["left"].extend([(base, 10.0), (base + 1, 14.0)])  # delta 4
    log._readings["right"].extend([(base, 5.0), (base + 1, 6.0)])   # delta 1
    log._tick_inner()

    # Two inserts, one per zone.
    assert len(conn.inserts) == 2
    for q, params in conn.inserts:
        assert "INSERT INTO controller_pressure_movement" in q
        assert "abs_delta_sum_60s" in q
        assert "max_delta_60s" in q
        assert "sample_count" in q
        assert "occupied" in q
    by_zone = {p[0]: p for _, p in conn.inserts}
    assert by_zone["left"][1] == pytest.approx(4.0)
    assert by_zone["left"][2] == pytest.approx(4.0)
    assert by_zone["left"][3] == 2
    assert by_zone["left"][4] is True
    assert by_zone["right"][4] is False


def test_on_pressure_appends_and_trims():
    log = _make_logger()
    log._on_pressure("ent", "state", "0", "12.5", {"zone": "left"})
    log._on_pressure("ent", "state", "12.5", "13.5", {"zone": "left"})
    assert len(log._readings["left"]) == 2
    # Force window expiration
    log._readings["left"][0] = (
        log._readings["left"][0][0] - 999.0,
        log._readings["left"][0][1],
    )
    log._trim("left")
    assert len(log._readings["left"]) == 1


def test_on_pressure_handles_unavailable():
    log = _make_logger()
    log._on_pressure("ent", "state", "0", "unavailable", {"zone": "left"})
    log._on_pressure("ent", "state", "0", None, {"zone": "left"})
    log._on_pressure("ent", "state", "0", "not-a-number", {"zone": "left"})
    assert len(log._readings["left"]) == 0


def test_aggregate_single_reading_returns_zero():
    log = _make_logger()
    log._readings["left"].append((time.monotonic(), 10.0))
    s, m = log._aggregate("left")
    assert s == 0.0 and m == 0.0
