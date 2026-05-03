"""Tests for appdaemon/sleep_controller_v6.py (shadow mode)."""
from __future__ import annotations

import importlib.util
import sys
import types
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

# Stub hassapi
fake = types.ModuleType("hassapi")
class _FakeHass:
    def get_state(self, *a, **kw): return None
    def call_service(self, *a, **kw): pass
    def log(self, *a, **kw): pass
    def listen_state(self, *a, **kw): pass
    def run_every(self, *a, **kw): pass
fake.Hass = _FakeHass
sys.modules["hassapi"] = fake

# Make ml.v6 importable from the repo root
_REPO = Path(__file__).parent.parent
sys.path.insert(0, str(_REPO))
sys.path.insert(0, str(_REPO / "appdaemon"))

_PATH = _REPO / "appdaemon" / "sleep_controller_v6.py"
_spec = importlib.util.spec_from_file_location("sleep_controller_v6", _PATH)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
SleepControllerV6 = _mod.SleepControllerV6


class FakeCursor:
    def __init__(self, parent):
        self.parent = parent
    def execute(self, q, params=None):
        if q.strip().startswith("SELECT 1"):
            return
        if "INSERT INTO controller_readings" in q:
            self.parent.inserted_rows.append((q, params))
        elif "SELECT AVG" in q:
            self.parent.last_query = q
            self.result = (None,)
        else:
            self.parent.misc_queries.append(q)
    def fetchone(self):
        return getattr(self, "result", (None,))
    def close(self): pass


class FakeConn:
    def __init__(self):
        self.inserted_rows = []
        self.misc_queries = []
        self.committed = 0
    def cursor(self): return FakeCursor(self)
    def commit(self): self.committed += 1
    def close(self): pass


def _make_ctrl(states=None, with_conn=True):
    """Build a controller without calling initialize() — set required attrs."""
    obj = SleepControllerV6.__new__(SleepControllerV6)
    obj.args = {}
    obj._enabled = True
    obj._pg_host = "x"
    obj._pg_conn = FakeConn() if with_conn else None
    obj._room_temp_entity = "sensor.room"
    obj._regime_config = _mod.v6_regime.DEFAULT_CONFIG
    obj._plant = _mod.v6_firmware.FirmwarePlant()
    obj._residual_heads = {
        "left": _mod.v6_residual.ResidualHead(zone="left"),
        "right": _mod.v6_residual.ResidualHead(zone="right"),
    }
    obj.safety_actuator = {
        "left": _mod.SafetyActuator(obj, "left", dry_run=True),
        "right": _mod.SafetyActuator(obj, "right", dry_run=True),
    }
    obj._bed_onset_ts = {"left": None, "right": None}
    obj._bedjet_off_ts = None
    obj._states = states or {}

    def _get_state(eid, **kw):
        if "attribute" in kw and kw["attribute"] == "unit_of_measurement":
            return obj._states.get(f"{eid}__unit")
        if "attribute" in kw and kw["attribute"] == "last_changed":
            return obj._states.get(f"{eid}__last_changed")
        return obj._states.get(eid)

    obj.get_state = MagicMock(side_effect=_get_state)
    obj.call_service = MagicMock()
    obj.log = MagicMock()
    obj.listen_state = MagicMock()
    obj.run_every = MagicMock()
    return obj


# ── initialize() ────────────────────────────────────────────────────────

def test_initialize_succeeds_with_mocks(monkeypatch):
    obj = SleepControllerV6.__new__(SleepControllerV6)
    obj.args = {}
    obj.get_state = MagicMock(return_value="off")
    obj.call_service = MagicMock()
    obj.log = MagicMock()
    obj.listen_state = MagicMock()
    obj.run_every = MagicMock()
    SleepControllerV6.initialize(obj)
    # banner contains version
    log_lines = [c.args[0] for c in obj.log.call_args_list if c.args]
    assert any(_mod.V6_PATCH_LEVEL in l for l in log_lines)
    assert any(_mod.V6_VERSION in l for l in log_lines)
    obj.run_every.assert_called()  # control loop scheduled


def test_banner_format():
    assert _mod.V6_VERSION == "v6_shadow_v1"
    assert "v6_regime+plant+proxy+residual+actuator" == _mod.V6_PATCH_LEVEL


# ── _control_loop ───────────────────────────────────────────────────────

_FULL_STATE = {
    "input_boolean.snug_v6_shadow_logging": "on",
    "input_boolean.snug_v6_residual_enabled": "off",
    "input_boolean.snug_v6_enabled": "off",
    "input_boolean.snug_right_rail_engaged": "off",
    "switch.smart_topper_left_side_3_level_mode": "off",
    "switch.smart_topper_right_side_3_level_mode": "off",
    "climate.bedjet_shar": "off",
    "input_text.apple_health_sleep_stage": "asleepCore",
    "sensor.room": "72.0",
    "binary_sensor.bed_presence_2bcab8_bed_occupied_left": "on",
    "binary_sensor.bed_presence_2bcab8_bed_occupied_right": "on",
    # Body sensors
    "sensor.smart_topper_left_side_body_sensor_left": "80.0",
    "sensor.smart_topper_left_side_body_sensor_center": "82.0",
    "sensor.smart_topper_left_side_body_sensor_right": "81.0",
    "sensor.smart_topper_left_side_ambient_temperature": "78.0",
    "sensor.smart_topper_left_side_temperature_setpoint": "85.0",
    "sensor.smart_topper_left_side_blower_output": "55.5",
    "number.smart_topper_left_side_bedtime_temperature": "-5",
    "sensor.smart_topper_right_side_body_sensor_left": "82.0",
    "sensor.smart_topper_right_side_body_sensor_center": "84.0",
    "sensor.smart_topper_right_side_body_sensor_right": "83.0",
    "sensor.smart_topper_right_side_ambient_temperature": "78.0",
    "sensor.smart_topper_right_side_temperature_setpoint": "86.0",
    "sensor.smart_topper_right_side_blower_output": "60.0",
    "number.smart_topper_right_side_bedtime_temperature": "-6",
    # last_changed for sleep mode + occupied
    "input_boolean.sleep_mode__last_changed":
        datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
}


def test_control_loop_writes_shadow_rows_for_both_zones():
    ctrl = _make_ctrl(_FULL_STATE)
    ctrl._control_loop_inner()
    rows = ctrl._pg_conn.inserted_rows
    zones = sorted(p[0] for _, p in rows)  # zone is the 1st bound param
    assert zones == ["left", "right"]
    # Each row: action='shadow', controller_version='v6_shadow'
    for q, params in rows:
        assert "v6_shadow" in q or any("v6_shadow" == p for p in params)
        assert "shadow" in params  # action column
        assert _mod.CONTROLLER_VERSION_TAG in params


def test_control_loop_skips_when_shadow_logging_off():
    states = dict(_FULL_STATE)
    states["input_boolean.snug_v6_shadow_logging"] = "off"
    ctrl = _make_ctrl(states)
    ctrl._control_loop_inner()
    assert ctrl._pg_conn.inserted_rows == []


def test_never_calls_safety_actuator_write_in_shadow_mode():
    ctrl = _make_ctrl(_FULL_STATE)
    for zone in ("left", "right"):
        ctrl.safety_actuator[zone].write = MagicMock(return_value={})
    ctrl._control_loop_inner()
    for zone in ("left", "right"):
        ctrl.safety_actuator[zone].write.assert_not_called()


def test_listener_master_enabled_logs_warning():
    ctrl = _make_ctrl(_FULL_STATE)
    ctrl._on_master_enabled_change("e", "s", "off", "on", {})
    levels = [c.kwargs.get("level") for c in ctrl.log.call_args_list]
    assert "WARNING" in levels


def test_listener_residual_enabled_off_path_forces_off_when_no_model(tmp_path):
    states = dict(_FULL_STATE)
    states["input_text.snug_v6_residual_model_path"] = str(tmp_path / "missing.json")
    ctrl = _make_ctrl(states)
    ctrl._on_residual_enabled_change("e", "s", "off", "on", {})
    # call_service should have been invoked to turn off residual flag
    args = [(c.args, c.kwargs) for c in ctrl.call_service.call_args_list]
    assert any(
        (a[0] and a[0][0] == "input_boolean/turn_off")
        or a[1].get("entity_id") == "input_boolean.snug_v6_residual_enabled"
        for a in args
    )


def test_listener_bed_onset_records_timestamp():
    ctrl = _make_ctrl(_FULL_STATE)
    ctrl._on_bed_onset("e", "s", "off", "on", {"zone": "left"})
    assert ctrl._bed_onset_ts["left"] is not None


def test_listener_bedjet_change_records_off_transition():
    ctrl = _make_ctrl(_FULL_STATE)
    ctrl._on_bedjet_change("e", "s", "heat", "off", {})
    assert ctrl._bedjet_off_ts is not None
    # heat→turbo should NOT update off_ts
    ctrl._bedjet_off_ts = None
    ctrl._on_bedjet_change("e", "s", "heat", "turbo", {})
    assert ctrl._bedjet_off_ts is None


def test_zone_snapshot_reads_all_sensors():
    ctrl = _make_ctrl(_FULL_STATE)
    snap = ctrl._read_zone_snapshot("left")
    assert snap["body_left"] == 80.0
    assert snap["body_center"] == 82.0
    assert snap["body_avg"] is not None
    assert snap["setting"] == -5


def test_post_bedjet_min_none_when_active():
    ctrl = _make_ctrl(_FULL_STATE)
    assert ctrl._post_bedjet_min(bedjet_active=True) is None


def test_post_bedjet_min_computes_after_off():
    ctrl = _make_ctrl(_FULL_STATE)
    import time as _t
    ctrl._bedjet_off_ts = _t.time() - 90.0  # 1.5 min ago
    val = ctrl._post_bedjet_min(bedjet_active=False)
    assert val is not None and 1.0 <= val <= 5.0


def test_disabled_when_ml_imports_fail(monkeypatch):
    monkeypatch.setattr(_mod, "_ML_IMPORT_ERROR", ImportError("boom"))
    obj = SleepControllerV6.__new__(SleepControllerV6)
    obj.args = {}
    obj.get_state = MagicMock(return_value=None)
    obj.call_service = MagicMock()
    obj.log = MagicMock()
    obj.listen_state = MagicMock()
    obj.run_every = MagicMock()
    SleepControllerV6.initialize(obj)
    assert obj._enabled is False
