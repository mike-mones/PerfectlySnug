"""Tests for appdaemon/safety_actuator.py."""
from __future__ import annotations

import importlib.util
import sys
import time
import types
from pathlib import Path

# Provide a dummy hassapi (safety_actuator does NOT import it but the
# package layout might cause indirect imports in CI).
sys.modules.setdefault("hassapi", types.ModuleType("hassapi"))

_PATH = Path(__file__).parent.parent / "appdaemon" / "safety_actuator.py"
_spec = importlib.util.spec_from_file_location("safety_actuator", _PATH)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
SafetyActuator = _mod.SafetyActuator
DummySafetyActuator = _mod.DummySafetyActuator


class FakeHass:
    def __init__(self, states=None):
        self.states = states or {}
        self.calls = []
        self.logs = []

    def get_state(self, entity_id, **kw):
        return self.states.get(entity_id)

    def call_service(self, service, **kw):
        self.calls.append((service, kw))
        # Mirror lease writes back into state map for CAS verification.
        if service == "input_text/set_value":
            self.states[kw["entity_id"]] = kw["value"]

    def log(self, msg, **kw):
        self.logs.append((kw.get("level", "INFO"), msg))


def _live_state(zone, *, master=True, live=True, lease="v6", rail=False):
    return {
        "input_boolean.snug_v6_enabled": "on" if master else "off",
        f"input_boolean.snug_v6_{zone}_live": "on" if live else "off",
        f"input_text.snug_writer_owner_{zone}": lease,
        "input_boolean.snug_right_rail_engaged": "on" if rail else "off",
    }


# ── Tests ────────────────────────────────────────────────────────────────

def test_dry_run_blocks_every_write():
    sa = SafetyActuator(hass_app=None, zone="left", dry_run=True)
    r = sa.write(-5, regime="NORMAL_COOL", reason="t")
    assert r == {"written": None, "blocked": True, "reason": "dry_run"}


def test_dummy_actuator_blocks():
    sa = DummySafetyActuator(zone="right")
    assert sa.write(-3, regime="x", reason="x")["reason"] == "dry_run"


def test_cooling_only_clip_positive_input():
    h = FakeHass(_live_state("left"))
    sa = SafetyActuator(h, "left")
    r = sa.write(+2, regime="NORMAL_COOL", reason="t")
    # Clipped to 0 then written.
    assert not r["blocked"]
    assert r["written"] == 0


def test_master_arm_off_blocks():
    h = FakeHass(_live_state("left", master=False))
    sa = SafetyActuator(h, "left")
    r = sa.write(-5, regime="x", reason="x")
    assert r["blocked"] and r["reason"] == "master_arm_off"


def test_live_off_returns_shadow_only():
    h = FakeHass(_live_state("left", live=False))
    sa = SafetyActuator(h, "left")
    r = sa.write(-5, regime="x", reason="x")
    assert r["blocked"] and r["reason"] == "shadow_only"


def test_lease_held_by_v5_blocks():
    h = FakeHass(_live_state("right", lease="v5"))
    sa = SafetyActuator(h, "right")
    r = sa.write(-5, regime="x", reason="x")
    assert r["blocked"] and r["reason"] == "lease_held_by_v5"


def test_rail_engaged_right_blocks_when_not_force_cool():
    h = FakeHass(_live_state("right", rail=True))
    sa = SafetyActuator(h, "right")
    r = sa.write(-5, regime="x", reason="x")
    assert r["blocked"] and r["reason"] == "rail_engaged_right"


def test_rail_engaged_right_allows_force_cool_minus_10():
    h = FakeHass(_live_state("right", rail=True))
    sa = SafetyActuator(h, "right")
    r = sa.write(-10, regime="SAFETY_YIELD", reason="x")
    assert not r["blocked"]
    assert r["written"] == -10


def test_rail_engaged_left_does_not_apply_right_mutex():
    h = FakeHass(_live_state("left"))
    h.states["input_boolean.snug_right_rail_engaged"] = "on"
    sa = SafetyActuator(h, "left")
    r = sa.write(-5, regime="x", reason="x")
    assert not r["blocked"]


def test_rate_limit_blocks_large_step():
    h = FakeHass(_live_state("left"))
    sa = SafetyActuator(h, "left", max_step_per_tick=2)
    r1 = sa.write(-3, regime="x", reason="x")
    assert not r1["blocked"]
    r2 = sa.write(-7, regime="x", reason="x")
    assert r2["blocked"] and r2["reason"] == "rate_limit"


def test_dead_man_trips_after_threshold():
    h = FakeHass(_live_state("left"))
    sa = SafetyActuator(h, "left", dead_man_sec=0.0001)
    r1 = sa.write(-3, regime="x", reason="x")
    assert not r1["blocked"]
    time.sleep(0.001)
    r2 = sa.write(-4, regime="x", reason="x")
    assert r2["blocked"] and r2["reason"] == "dead_man"


def test_successful_write_updates_lease_and_ts():
    h = FakeHass(_live_state("left"))
    sa = SafetyActuator(h, "left")
    r = sa.write(-5, regime="x", reason="x")
    assert not r["blocked"]
    assert sa.last_v6_write == -5
    assert sa.last_v6_write_ts is not None
    # number/set_value + input_text/set_value (lease re-assert)
    services = [c[0] for c in h.calls]
    assert "number/set_value" in services
    assert "input_text/set_value" in services
    # entity verified
    write_call = next(c for c in h.calls if c[0] == "number/set_value")
    assert write_call[1]["entity_id"] == "number.smart_topper_left_side_bedtime_temperature"
    assert write_call[1]["value"] == -5


def test_take_lease_acquires():
    h = FakeHass(_live_state("left", lease="v5"))
    sa = SafetyActuator(h, "left")
    ok = sa.take_lease()
    assert ok is True
    assert h.states["input_text.snug_writer_owner_left"] == "v6"


def test_release_lease_back_to_v5():
    h = FakeHass(_live_state("left"))
    sa = SafetyActuator(h, "left")
    sa.release_lease()
    assert h.states["input_text.snug_writer_owner_left"] == "v5"


def test_fallback_to_v5_releases_lease_and_clears_state():
    h = FakeHass(_live_state("right"))
    sa = SafetyActuator(h, "right")
    sa.last_v6_write = -3
    sa.last_v6_write_ts = time.monotonic()
    sa.fallback_to_v5(reason="test")
    assert sa.last_v6_write is None
    assert sa.last_v6_write_ts is None
    assert h.states["input_text.snug_writer_owner_right"] == "v5"


def test_invalid_target_type_blocks():
    h = FakeHass(_live_state("left"))
    sa = SafetyActuator(h, "left")
    r = sa.write("not-an-int", regime="x", reason="x")
    assert r["blocked"] and r["reason"] == "invalid_target_type"


def test_zone_must_be_left_or_right():
    import pytest
    with pytest.raises(ValueError):
        SafetyActuator(FakeHass(), "middle")
