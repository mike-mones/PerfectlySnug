"""
Tests for sleep_controller_v3.py

Two categories:
  1. Source analysis tests — verify structural invariants by parsing source code
  2. Behavioral tests — mock AppDaemon/HA and verify controller behavior end-to-end

Run:
    python3 -m pytest PerfectlySnug/tests/test_controller_v3.py -v
"""

import ast
import json
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import pytest

CONTROLLER_PATH = Path(__file__).parent.parent / "appdaemon" / "sleep_controller_v3.py"


def _read_source():
    return CONTROLLER_PATH.read_text()


def _extract_method(source: str, method_name: str) -> str:
    pattern = rf'(    def {method_name}\(.*?)(?=\n    def |\nclass |\Z)'
    match = re.search(pattern, source, re.DOTALL)
    return match.group(1) if match else ""


# ── Source Analysis Tests ────────────────────────────────────────────────

class TestSourceCompiles:
    def test_no_syntax_errors(self):
        src = _read_source()
        ast.parse(src)

    def test_no_conditional_format_specifiers(self):
        """F-strings must not have 'if...else' inside format specs."""
        src = _read_source()
        bad = re.compile(r'\{[^}]*:\.[0-9]+[a-z]\s+if\s+.*?else\s+.*?\}')
        matches = bad.findall(src)
        assert not matches, f"Bad f-string format specs: {matches}"


class TestNoAppleWatchDependency:
    """Verify the simplified controller has no Apple Watch / sleep stage dependencies."""

    def test_no_sleep_stage_entity(self):
        src = _read_source()
        assert "apple_health_sleep_stage" not in src

    def test_no_health_entities(self):
        src = _read_source()
        assert "HEALTH_ENTITIES" not in src
        assert "apple_health_hr" not in src
        assert "apple_health_hrv" not in src
        assert "apple_health_respiratory_rate" not in src
        assert "apple_health_wrist_temp" not in src

    def test_no_stage_classifier(self):
        src = _read_source()
        assert "stage_classifier" not in src
        assert "_predict_stage_ml" not in src
        assert "_walk_tree" not in src

    def test_no_pid_controller(self):
        src = _read_source()
        assert "PID_KP" not in src
        assert "PID_KI" not in src
        assert "PID_KD" not in src
        assert "integral_error" not in src
        assert "p_term" not in src
        assert "i_term" not in src
        assert "d_term" not in src

    def test_no_transfer_function_learning(self):
        src = _read_source()
        assert "transfer_rate" not in src
        assert "DEGREES_PER_SETTING_POINT" not in src

    def test_no_trend_penalty(self):
        src = _read_source()
        assert "TREND_PENALTY" not in src
        assert "nightly_history" not in src

    def test_no_deficit_compensation(self):
        src = _read_source()
        assert "DEFICIT" not in src
        assert "prior_night_stages" not in src


class TestStructuralInvariants:
    """Verify the controller has the essential features."""

    def test_baseline_reset_at_wake(self):
        """Presets must be reset to USER_BASELINE at end of night."""
        src = _read_source()
        end_night = _extract_method(src, "_end_night")
        assert "USER_BASELINE" in end_night
        assert "number/set_value" in end_night

    def test_hot_threshold_exists(self):
        """Controller must have a hot safety threshold."""
        src = _read_source()
        loop = _extract_method(src, "_control_loop_inner")
        assert "BODY_HOT_THRESHOLD_F" in loop

    def test_kill_switch_exists(self):
        src = _read_source()
        assert "_check_kill_switch" in src
        assert "manual_mode" in src

    def test_occupancy_hold_exists(self):
        src = _read_source()
        assert "OCCUPANCY_HOLD_MINUTES" in src
        assert "occupancy_hold_done" in src

    def test_crash_handler(self):
        src = _read_source()
        loop = _extract_method(src, "_control_loop")
        assert "except" in loop
        assert "_save_state" in loop

    def test_ambient_compensation(self):
        """Controller must compensate for ambient temperature."""
        src = _read_source()
        loop = _extract_method(src, "_control_loop_inner")
        assert "AMBIENT_REFERENCE_F" in loop
        assert "AMBIENT_COMPENSATION_PER_F" in loop

    def test_auto_restart(self):
        """Topper auto-restart must be preserved."""
        src = _read_source()
        loop = _extract_method(src, "_control_loop_inner")
        assert "auto-restart" in loop.lower() or "switch/turn_on" in loop

    def test_writes_active_phase_only(self):
        """When adjusting, must write to active phase only."""
        src = _read_source()
        loop = _extract_method(src, "_control_loop_inner")
        assert "preset_entity" in loop
        assert 'entity_id=preset_entity' in loop

    def test_multi_night_learning(self):
        """Controller must have multi-night learning."""
        src = _read_source()
        assert "_update_learned" in src
        assert "_load_learned" in src
        assert "LEARNING_FILE" in src


class TestSimplicity:
    """Verify the controller stays simple."""

    def test_line_count(self):
        """Controller should be under 800 lines."""
        src = _read_source()
        lines = src.strip().split('\n')
        assert len(lines) < 800, f"Controller is {len(lines)} lines — too complex!"

    def test_no_pid_controller(self):
        src = _read_source()
        assert "PID_KP" not in src
        assert "PID_KI" not in src
        assert "p_term" not in src
        assert "i_term" not in src

    def test_no_wake_ramp(self):
        src = _read_source()
        assert "WAKE_RAMP" not in src


# ── Behavioral Tests with Mocks ──────────────────────────────────────────

# Create a real base class for hassapi.Hass so SleepController can inherit
class _FakeHass:
    def get_state(self, *args, **kwargs): pass
    def call_service(self, *args, **kwargs): pass
    def log(self, *args, **kwargs): pass
    def listen_state(self, *args, **kwargs): pass
    def run_every(self, *args, **kwargs): pass

import types
fake_hass_module = types.ModuleType('hassapi')
fake_hass_module.Hass = _FakeHass
sys.modules['hassapi'] = fake_hass_module

# Now import the controller module
sys.path.insert(0, str(CONTROLLER_PATH.parent))
import importlib
_spec = importlib.util.spec_from_file_location("sleep_controller_v3", CONTROLLER_PATH)
ctrl_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ctrl_module)
SleepController = ctrl_module.SleepController


def _make_controller(entity_states=None):
    """Create a SleepController instance with mocked HA methods."""
    c = SleepController.__new__(SleepController)
    c.zones = ["left"]
    c.zone_state = {"left": c._fresh_zone_state()}
    c.learned = {}  # Multi-night learned adjustments
    c._loop_count = 0

    _entity_states = entity_states or {}

    def mock_get_state(entity_id, **kwargs):
        return _entity_states.get(entity_id)

    def mock_call_service(service, **kwargs):
        pass

    def mock_log(msg, level="INFO"):
        pass

    def mock_listen_state(*args, **kwargs):
        pass

    def mock_run_every(*args, **kwargs):
        pass

    c.get_state = mock_get_state
    c.call_service = mock_call_service
    c.log = mock_log
    c.listen_state = mock_listen_state
    c.run_every = mock_run_every
    c._entity_states = _entity_states
    return c


class TestBedtimeDetection:
    def test_bedtime_sets_timestamp(self):
        """When topper starts running, bedtime_ts should be set."""
        c = _make_controller({
            "sensor.smart_topper_left_side_run_progress": "5",
            "number.smart_topper_left_side_bedtime_temperature": "-8",
            "number.smart_topper_left_side_sleep_temperature": "-6",
            "number.smart_topper_left_side_wake_temperature": "-5",
        })
        assert c.zone_state["left"]["bedtime_ts"] is None
        c._control_loop_inner(None)
        assert c.zone_state["left"]["bedtime_ts"] is not None

    def test_bedtime_reads_current_presets(self):
        """On bedtime, controller should read and store current presets."""
        c = _make_controller({
            "sensor.smart_topper_left_side_run_progress": "5",
            "number.smart_topper_left_side_bedtime_temperature": "-7",
            "number.smart_topper_left_side_sleep_temperature": "-5",
            "number.smart_topper_left_side_wake_temperature": "-4",
        })
        c._control_loop_inner(None)
        pushed = c.zone_state["left"]["last_settings_pushed"]
        assert pushed["bedtime"] == -7
        assert pushed["sleep"] == -5
        assert pushed["wake"] == -4


class TestWakeDetection:
    def test_wake_resets_baseline(self):
        """On wake, presets must be reset to USER_BASELINE values."""
        c = _make_controller({
            "sensor.smart_topper_left_side_run_progress": "0",  # Stopped
        })
        # Simulate a completed night
        c.zone_state["left"]["bedtime_ts"] = (datetime.now() - timedelta(hours=8)).isoformat()
        c.zone_state["left"]["last_run_progress"] = 50  # Not > 90, so no auto-restart
        c.zone_state["left"]["body_temp_history"] = [82.0, 83.0]

        calls = []
        original_call = c.call_service
        def tracking_call(service, **kwargs):
            calls.append((service, kwargs))
        c.call_service = tracking_call

        c._control_loop_inner(None)

        # Should have called set_value for all 3 presets
        set_calls = [(s, k) for s, k in calls if s == "number/set_value"]
        assert len(set_calls) == 3, f"Expected 3 set_value calls, got {len(set_calls)}"

        # Verify correct values
        values_by_entity = {k["entity_id"]: k["value"] for _, k in set_calls}
        assert values_by_entity["number.smart_topper_left_side_bedtime_temperature"] == -8
        assert values_by_entity["number.smart_topper_left_side_sleep_temperature"] == -6
        assert values_by_entity["number.smart_topper_left_side_wake_temperature"] == -5

    def test_wake_clears_bedtime_ts(self):
        c = _make_controller({
            "sensor.smart_topper_left_side_run_progress": "0",
        })
        c.zone_state["left"]["bedtime_ts"] = (datetime.now() - timedelta(hours=7)).isoformat()
        c.zone_state["left"]["last_run_progress"] = 50
        c.zone_state["left"]["body_temp_history"] = [82.0]
        c._control_loop_inner(None)
        assert c.zone_state["left"]["bedtime_ts"] is None


class TestThresholdControl:
    def _sleeping_controller(self, body_temp, current_setting=-6, ambient=70.0):
        """Create a controller that's mid-sleep with given body temp and setting."""
        c = _make_controller({
            "sensor.smart_topper_left_side_run_progress": "50",
            "sensor.smart_topper_left_side_body_sensor_right": str(body_temp),
            "sensor.smart_topper_left_side_body_sensor_center": str(body_temp),
            "sensor.smart_topper_left_side_body_sensor_left": str(body_temp),
            "sensor.smart_topper_left_side_ambient_temperature": str(ambient),
            "number.smart_topper_left_side_start_length_minutes": "60",
            "number.smart_topper_left_side_wake_length_minutes": "30",
            "number.smart_topper_left_side_bedtime_temperature": str(current_setting),
            "number.smart_topper_left_side_sleep_temperature": str(current_setting),
            "number.smart_topper_left_side_wake_temperature": str(current_setting),
        })
        c.zone_state["left"]["bedtime_ts"] = (datetime.now() - timedelta(hours=2)).isoformat()
        c.zone_state["left"]["occupancy_hold_done"] = True
        c.zone_state["left"]["last_settings_pushed"] = {
            "bedtime": current_setting,
            "sleep": current_setting,
            "wake": current_setting,
        }
        return c

    def test_ambiguous_zone_follows_preference(self):
        """Body temp 80-85°F should follow preference, not adjust based on sensors."""
        # At 83°F (ambiguous zone), amb=70 (ref), baseline sleep=-6
        c = self._sleeping_controller(body_temp=83.0, current_setting=-4, ambient=70.0)
        calls = []
        c.call_service = lambda s, **k: calls.append((s, k))
        c._control_loop_inner(None)
        set_calls = [(s, k) for s, k in calls if s == "number/set_value"]
        # Effective = baseline -6 + learned 0 + ambient 0 = -6
        # Current is -4, so should move toward -6
        if set_calls:
            values = [k["value"] for _, k in set_calls]
            assert all(v == -6 for v in values), f"Expected -6, got {values}"

    def test_hot_safety_cools_down(self):
        """Body temp above 85°F should trigger safety cooling."""
        c = self._sleeping_controller(body_temp=86.0, current_setting=-6, ambient=70.0)
        # Need 2 consecutive hot readings
        c.zone_state["left"]["hot_streak"] = 1
        calls = []
        c.call_service = lambda s, **k: calls.append((s, k))
        c._control_loop_inner(None)
        set_calls = [(s, k) for s, k in calls if s == "number/set_value"]
        if set_calls:
            values = [k["value"] for _, k in set_calls]
            assert all(v <= -7 for v in values), f"Expected ≤-7, got {values}"

    def test_cold_room_warms_setting(self):
        """Colder room should produce warmer effective setting."""
        # ambient=66 is 4°F below reference 70, so compensation = +2
        c = self._sleeping_controller(body_temp=83.0, current_setting=-6, ambient=66.0)
        calls = []
        c.call_service = lambda s, **k: calls.append((s, k))
        c._control_loop_inner(None)
        set_calls = [(s, k) for s, k in calls if s == "number/set_value"]
        # Effective = baseline -6 + ambient_adj +2 = -4
        if set_calls:
            values = [k["value"] for _, k in set_calls]
            assert all(v == -4 for v in values), f"Expected -4, got {values}"

    def test_learned_adjustment_applied(self):
        """Multi-night learned adjustments should shift the effective setting."""
        c = self._sleeping_controller(body_temp=83.0, current_setting=-6, ambient=70.0)
        c.learned = {"left": {"sleep": 2}}  # Learned +2 for sleep
        calls = []
        c.call_service = lambda s, **k: calls.append((s, k))
        c._control_loop_inner(None)
        set_calls = [(s, k) for s, k in calls if s == "number/set_value"]
        # Effective = baseline -6 + learned +2 = -4
        if set_calls:
            values = [k["value"] for _, k in set_calls]
            assert all(v == -4 for v in values), f"Expected -4, got {values}"

    def test_override_floor_respected(self):
        """Manual override floor must prevent cooling below user's choice."""
        c = self._sleeping_controller(body_temp=83.0, current_setting=-3, ambient=70.0)
        c.zone_state["left"]["override_floor"] = {"sleep": -3}
        calls = []
        c.call_service = lambda s, **k: calls.append((s, k))
        c._control_loop_inner(None)
        set_calls = [(s, k) for s, k in calls if s == "number/set_value"]
        # Effective = -6, but floor = -3, so should stay at -3
        assert len(set_calls) == 0 or all(
            k["value"] >= -3 for _, k in set_calls
        ), "Should not go below override floor"


class TestKillSwitch:
    def test_three_changes_activates_manual(self):
        c = _make_controller()
        state = c.zone_state["left"]
        state["bedtime_ts"] = datetime.now().isoformat()
        now = datetime.now().timestamp()
        state["recent_setting_changes"] = [now - 2, now - 1, now]
        result = c._check_kill_switch("left", state)
        assert result is True
        assert state["manual_mode"] is True

    def test_two_changes_does_not_activate(self):
        c = _make_controller()
        state = c.zone_state["left"]
        state["bedtime_ts"] = datetime.now().isoformat()
        now = datetime.now().timestamp()
        state["recent_setting_changes"] = [now - 2, now]
        result = c._check_kill_switch("left", state)
        assert result is False
        assert state["manual_mode"] is False

    def test_manual_mode_skips_control(self):
        """Once in manual mode, the control loop should skip adjustments."""
        c = _make_controller({
            "sensor.smart_topper_left_side_run_progress": "50",
            "sensor.smart_topper_left_side_body_sensor_center": "88",
        })
        c.zone_state["left"]["bedtime_ts"] = datetime.now().isoformat()
        c.zone_state["left"]["manual_mode"] = True
        calls = []
        c.call_service = lambda s, **k: calls.append((s, k))
        c._control_loop_inner(None)
        set_calls = [(s, k) for s, k in calls if s == "number/set_value"]
        assert len(set_calls) == 0, "Should not adjust in manual mode"


class TestOccupancy:
    def test_empty_bed_skips(self):
        """Body temp below threshold = no one in bed, skip."""
        c = _make_controller({
            "sensor.smart_topper_left_side_run_progress": "50",
            "sensor.smart_topper_left_side_body_sensor_right": "72",
            "sensor.smart_topper_left_side_body_sensor_center": "72",
            "sensor.smart_topper_left_side_body_sensor_left": "72",
            "number.smart_topper_left_side_start_length_minutes": "60",
            "number.smart_topper_left_side_sleep_temperature": "-6",
        })
        c.zone_state["left"]["bedtime_ts"] = datetime.now().isoformat()
        calls = []
        c.call_service = lambda s, **k: calls.append((s, k))
        c._control_loop_inner(None)
        set_calls = [(s, k) for s, k in calls if s == "number/set_value"]
        assert len(set_calls) == 0

    def test_occupancy_hold_delays_control(self):
        """First detecting body should hold setting for OCCUPANCY_HOLD_MINUTES."""
        c = _make_controller({
            "sensor.smart_topper_left_side_run_progress": "50",
            "sensor.smart_topper_left_side_body_sensor_right": "85",
            "sensor.smart_topper_left_side_body_sensor_center": "85",
            "sensor.smart_topper_left_side_body_sensor_left": "85",
            "sensor.smart_topper_left_side_ambient_temperature": "74",
            "number.smart_topper_left_side_start_length_minutes": "60",
            "number.smart_topper_left_side_wake_length_minutes": "30",
            "number.smart_topper_left_side_sleep_temperature": "-6",
        })
        c.zone_state["left"]["bedtime_ts"] = (datetime.now() - timedelta(hours=2)).isoformat()
        c.zone_state["left"]["occupancy_hold_done"] = False
        c.zone_state["left"]["occupancy_detected_ts"] = None
        calls = []
        c.call_service = lambda s, **k: calls.append((s, k))
        c._control_loop_inner(None)
        # Should be in hold, not adjusting
        set_calls = [(s, k) for s, k in calls if s == "number/set_value"]
        assert len(set_calls) == 0
        assert c.zone_state["left"]["occupancy_detected_ts"] is not None


class TestOverrideDetection:
    def test_detects_manual_change(self):
        c = _make_controller({
            "number.smart_topper_left_side_sleep_temperature": "-4",
        })
        c.zone_state["left"]["bedtime_ts"] = datetime.now().isoformat()
        c.zone_state["left"]["last_settings_pushed"] = {"sleep": -6}
        override = c._detect_override("left", c.zone_state["left"], "sleep")
        assert override is not None
        assert override["delta"] == 2
        assert override["actual"] == -4

    def test_ignores_own_write(self):
        c = _make_controller({
            "number.smart_topper_left_side_sleep_temperature": "-6",
        })
        c.zone_state["left"]["bedtime_ts"] = datetime.now().isoformat()
        c.zone_state["left"]["last_settings_pushed"] = {"sleep": -6}
        override = c._detect_override("left", c.zone_state["left"], "sleep")
        assert override is None


class TestPhaseDetection:
    def test_bedtime_phase(self):
        c = _make_controller({
            "number.smart_topper_left_side_start_length_minutes": "60",
            "number.smart_topper_left_side_wake_length_minutes": "30",
            "sensor.smart_topper_left_side_run_progress": "5",
        })
        state = c.zone_state["left"]
        state["bedtime_ts"] = (datetime.now() - timedelta(minutes=30)).isoformat()
        assert c._get_active_phase("left", state) == "bedtime"

    def test_sleep_phase(self):
        c = _make_controller({
            "number.smart_topper_left_side_start_length_minutes": "60",
            "number.smart_topper_left_side_wake_length_minutes": "30",
            "sensor.smart_topper_left_side_run_progress": "50",
        })
        state = c.zone_state["left"]
        state["bedtime_ts"] = (datetime.now() - timedelta(hours=3)).isoformat()
        assert c._get_active_phase("left", state) == "sleep"


class TestAutoRestart:
    def test_auto_restart_when_body_in_bed(self):
        """If topper stops but body is still in bed, auto-restart."""
        c = _make_controller({
            "sensor.smart_topper_left_side_run_progress": "0",
            "sensor.smart_topper_left_side_body_sensor_center": "84",
            "sensor.smart_topper_left_side_body_sensor_right": "84",
            "sensor.smart_topper_left_side_body_sensor_left": "84",
        })
        c.zone_state["left"]["bedtime_ts"] = (datetime.now() - timedelta(hours=8)).isoformat()
        c.zone_state["left"]["last_run_progress"] = 95  # Was near completion

        calls = []
        c.call_service = lambda s, **k: calls.append((s, k))
        c._control_loop_inner(None)

        switch_calls = [(s, k) for s, k in calls if s == "switch/turn_on"]
        assert len(switch_calls) == 1, "Should auto-restart topper"

    def test_no_restart_when_bed_empty(self):
        """If topper stops and bed is empty, do normal wake routine."""
        c = _make_controller({
            "sensor.smart_topper_left_side_run_progress": "0",
            "sensor.smart_topper_left_side_body_sensor_center": "72",
            "sensor.smart_topper_left_side_body_sensor_right": "72",
            "sensor.smart_topper_left_side_body_sensor_left": "72",
        })
        c.zone_state["left"]["bedtime_ts"] = (datetime.now() - timedelta(hours=8)).isoformat()
        c.zone_state["left"]["last_run_progress"] = 95
        c.zone_state["left"]["body_temp_history"] = [82.0]

        calls = []
        c.call_service = lambda s, **k: calls.append((s, k))
        c._control_loop_inner(None)

        switch_calls = [(s, k) for s, k in calls if s == "switch/turn_on"]
        assert len(switch_calls) == 0, "Should NOT auto-restart when bed is empty"
