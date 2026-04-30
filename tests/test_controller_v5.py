"""Tests for sleep_controller_v5.py."""

import ast
import importlib.util
import sys
import types
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock


CONTROLLER_PATH = Path(__file__).parent.parent / "appdaemon" / "sleep_controller_v5.py"


class _FakeHass:
    def get_state(self, *args, **kwargs):
        pass

    def call_service(self, *args, **kwargs):
        pass

    def log(self, *args, **kwargs):
        pass

    def listen_state(self, *args, **kwargs):
        pass

    def run_every(self, *args, **kwargs):
        pass


fake_hass_module = types.ModuleType("hassapi")
fake_hass_module.Hass = _FakeHass
sys.modules["hassapi"] = fake_hass_module

_spec = importlib.util.spec_from_file_location("sleep_controller_v5", CONTROLLER_PATH)
ctrl_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ctrl_module)
SleepControllerV5 = ctrl_module.SleepControllerV5


class _FakeCursor:
    def __init__(self, rows=None):
        self.rows = rows or []
        self.query = None
        self.params = None
        self.closed = False

    def execute(self, query, params=None):
        self.query = query
        self.params = params

    def fetchall(self):
        return self.rows

    def close(self):
        self.closed = True


class _FakeConn:
    def __init__(self, rows=None):
        self.cursor_obj = _FakeCursor(rows=rows)
        self.commit = MagicMock()

    def cursor(self):
        return self.cursor_obj


def _snapshot(*, setting=-7, blower_pct=65):
    return {
        "body_center": 84.0,
        "body_left": 83.5,
        "body_right": 84.5,
        "body_avg": 84.0,
        "ambient": 76.0,
        "setpoint": 72.0,
        "blower_pct": blower_pct,
        "setting": setting,
    }


def _bed_presence_snapshot():
    return {
        "left_pressure": 80.5,
        "right_pressure": 15.0,
        "left_calibrated_pressure": 0.0,
        "right_calibrated_pressure": 0.0,
        "left_unoccupied_pressure": 80.5,
        "right_unoccupied_pressure": 15.0,
        "left_occupied_pressure": 95.0,
        "right_occupied_pressure": 98.0,
        "left_trigger_pressure": 88.0,
        "right_trigger_pressure": 77.0,
        "occupied_left": False,
        "occupied_right": False,
        "occupied_either": False,
        "occupied_both": False,
    }


def _make_controller(*, current_setting=-7, current_blower=65):
    controller = SleepControllerV5.__new__(SleepControllerV5)
    controller.args = {}
    controller._room_temp_entity = ctrl_module.DEFAULT_ROOM_TEMP_ENTITY
    controller._state = {
        "sleep_start": datetime.now().isoformat(),
        "sleep_start_epoch": datetime.now().timestamp() - 3600,
        "last_setting": current_setting,
        "last_change_ts": None,
        "last_restart_ts": None,
        "last_target_blower_pct": ctrl_module.L1_TO_BLOWER_PCT[current_setting],
        "override_freeze_until": None,
        "override_floor": None,
        "override_floor_blower_pct": None,
        "manual_mode": False,
        "recent_changes": [],
        "override_count": 0,
        "body_below_since": None,
        "hot_streak": 0,
        "current_cycle_num": None,
    }
    controller._learned = {}
    controller._pg_conn = None
    controller.call_service = MagicMock()
    controller.get_state = MagicMock(return_value=None)
    controller.log = MagicMock()
    controller._save_state = MagicMock()
    controller._log_to_postgres = MagicMock()
    controller._log_passive_zone_snapshot = MagicMock()
    controller._read_zone_snapshot = MagicMock(
        return_value=_snapshot(setting=current_setting, blower_pct=current_blower)
    )
    controller._read_float = MagicMock(
        side_effect=lambda entity_id: {
            ctrl_module.DEFAULT_ROOM_TEMP_ENTITY: 74.0,
            ctrl_module.E_BEDTIME_TEMP: float(current_setting),
        }.get(entity_id)
    )
    controller._read_str = MagicMock(return_value="unknown")
    controller._check_occupancy = MagicMock(return_value=True)
    controller._elapsed_min = MagicMock(return_value=60.0)
    controller._compute_setting = MagicMock(
        return_value={
            "setting": current_setting,
            "target_blower_pct": ctrl_module.L1_TO_BLOWER_PCT[current_setting],
            "base_setting": -10,
            "base_blower_pct": 100,
            "cycle_num": 1,
            "room_temp_comp": 0,
            "learned_adj_pct": 0,
            "data_source": "time_cycle",
            "hot_safety": False,
        }
    )
    return controller


def _set_states(controller, *, running="on", sleep_mode="on", responsive="off"):
    def _get_state(entity_id, **kwargs):
        if kwargs.get("attribute") == "last_changed":
            return None
        return {
            ctrl_module.E_RUNNING: running,
            ctrl_module.E_SLEEP_MODE: sleep_mode,
            ctrl_module.E_RESPONSIVE_COOLING: responsive,
        }.get(entity_id)

    controller.get_state = MagicMock(side_effect=_get_state)


class TestSourceCompiles:
    def test_no_syntax_errors(self):
        ast.parse(CONTROLLER_PATH.read_text())


class TestLadderMapping:
    def test_exact_ladder_and_inverse(self):
        controller = _make_controller()

        for l1, pct in ctrl_module.L1_TO_BLOWER_PCT.items():
            assert controller._l1_to_blower_pct(l1) == pct
            assert controller._blower_pct_to_l1(pct) == l1

        assert controller._blower_pct_to_l1(72) == -8
        assert controller._blower_pct_to_l1(44) == -5


class TestTemperatureUnits:
    def test_read_temperature_converts_celsius_to_fahrenheit(self):
        controller = SleepControllerV5.__new__(SleepControllerV5)

        def _get_state(entity_id, **kwargs):
            if kwargs.get("attribute") == "unit_of_measurement":
                return "°C"
            return "20.0"

        controller.get_state = MagicMock(side_effect=_get_state)

        assert controller._read_temperature("sensor.example") == 68.0


class TestBedPresenceSnapshot:
    def test_reads_and_derives_bed_presence_metrics(self):
        controller = SleepControllerV5.__new__(SleepControllerV5)

        values = {
            ctrl_module.BED_PRESENCE_ENTITIES["left_pressure"]: "80.5",
            ctrl_module.BED_PRESENCE_ENTITIES["right_pressure"]: "15.0",
            ctrl_module.BED_PRESENCE_ENTITIES["left_unoccupied_pressure"]: "80.5",
            ctrl_module.BED_PRESENCE_ENTITIES["right_unoccupied_pressure"]: "15.0",
            ctrl_module.BED_PRESENCE_ENTITIES["left_occupied_pressure"]: "95.0",
            ctrl_module.BED_PRESENCE_ENTITIES["right_occupied_pressure"]: "98.0",
            ctrl_module.BED_PRESENCE_ENTITIES["left_trigger_pressure"]: "88.0",
            ctrl_module.BED_PRESENCE_ENTITIES["right_trigger_pressure"]: "77.0",
            ctrl_module.BED_PRESENCE_ENTITIES["occupied_left"]: "off",
            ctrl_module.BED_PRESENCE_ENTITIES["occupied_right"]: "off",
            ctrl_module.BED_PRESENCE_ENTITIES["occupied_either"]: "off",
            ctrl_module.BED_PRESENCE_ENTITIES["occupied_both"]: "off",
        }
        controller.get_state = MagicMock(side_effect=lambda entity_id, **kwargs: values.get(entity_id))

        snapshot = SleepControllerV5._read_bed_presence_snapshot(controller)

        assert snapshot["left_pressure"] == 80.5
        assert snapshot["right_pressure"] == 15.0
        assert snapshot["left_calibrated_pressure"] == 0.0
        assert snapshot["right_calibrated_pressure"] == 0.0
        assert snapshot["left_trigger_pressure"] == 88.0
        assert snapshot["occupied_either"] is False


class TestResponsiveCooling:
    def test_initialize_turns_responsive_cooling_off(self):
        controller = SleepControllerV5.__new__(SleepControllerV5)
        controller.args = {}
        controller.log = MagicMock()
        controller.call_service = MagicMock()
        controller.run_every = MagicMock()
        controller.listen_state = MagicMock()
        controller._load_state = MagicMock()
        controller._load_learned = MagicMock(return_value={})
        controller._check_midnight_restart = MagicMock()

        controller.initialize()

        controller.call_service.assert_called_once_with(
            "switch/turn_off",
            entity_id=ctrl_module.E_RESPONSIVE_COOLING,
        )
        assert controller._room_temp_entity == ctrl_module.DEFAULT_ROOM_TEMP_ENTITY

    def test_control_loop_turns_responsive_cooling_back_off(self):
        controller = _make_controller()
        _set_states(controller, running="on", responsive="on")

        controller._control_loop({})

        controller.call_service.assert_called_once_with(
            "switch/turn_off",
            entity_id=ctrl_module.E_RESPONSIVE_COOLING,
        )
        controller._log_to_postgres.assert_called_once()


class TestAutoRestart:
    def test_restarts_when_running_off_and_still_occupied(self):
        controller = _make_controller()
        _set_states(controller, running="off", responsive="off")

        controller._control_loop({})

        controller.call_service.assert_called_once_with(
            "switch/turn_on",
            entity_id=ctrl_module.E_RUNNING,
        )
        assert controller._state["last_restart_ts"] is not None
        controller._save_state.assert_called_once()
        controller._log_to_postgres.assert_called_once()

    def test_debounces_recent_restart_attempt(self):
        controller = _make_controller()
        _set_states(controller, running="off", responsive="off")
        controller._state["last_restart_ts"] = (
            datetime.now() - timedelta(seconds=ctrl_module.AUTO_RESTART_DEBOUNCE_SEC - 30)
        ).isoformat()

        controller._control_loop({})

        controller.call_service.assert_not_called()
        controller._save_state.assert_not_called()
        controller._log_to_postgres.assert_called_once()

    def test_does_not_restart_when_bed_is_empty(self):
        controller = _make_controller()
        _set_states(controller, running="off", responsive="off")
        controller._check_occupancy.return_value = False

        controller._control_loop({})

        controller.call_service.assert_not_called()
        controller._log_to_postgres.assert_called_once()
        assert controller._log_to_postgres.call_args.kwargs["action"] == "empty_bed"


class TestOverrideFloor:
    def test_override_floor_persists_after_freeze(self):
        controller = _make_controller(current_setting=-6, current_blower=50)
        _set_states(controller, running="on", responsive="off")
        controller._state["override_floor"] = -5
        controller._state["override_freeze_until"] = (
            datetime.now() - timedelta(minutes=1)
        ).isoformat()
        controller._compute_setting.return_value = {
            "setting": -10,
            "target_blower_pct": 100,
            "base_setting": -10,
            "base_blower_pct": 100,
            "cycle_num": 1,
            "room_temp_comp": 0,
            "learned_adj_pct": 0,
            "data_source": "time_cycle",
            "hot_safety": False,
        }

        controller._control_loop({})

        controller.call_service.assert_called_with(
            "number/set_value",
            entity_id=ctrl_module.E_BEDTIME_TEMP,
            value=-5,
        )
        assert controller._state["override_floor"] == -5
        assert controller._log_to_postgres.call_args.kwargs["override_floor"] == -5


class TestControlLoopLogging:
    def test_set_rows_force_fresh_blower_read(self):
        controller = _make_controller(current_setting=-7, current_blower=65)
        _set_states(controller, running="on", responsive="off")
        controller._compute_setting.return_value = {
            "setting": -8,
            "target_blower_pct": 75,
            "base_setting": -10,
            "base_blower_pct": 100,
            "cycle_num": 1,
            "room_temp_comp": 0,
            "learned_adj_pct": 0,
            "data_source": "time_cycle",
            "hot_safety": False,
        }

        controller._control_loop({})

        kwargs = controller._log_to_postgres.call_args.kwargs
        assert kwargs["action"] == "set"
        assert kwargs["blower_pct"] is None


class TestLearning:
    def test_learns_blower_residuals_and_filters_to_v5(self):
        controller = _make_controller()
        fake_conn = _FakeConn(
            rows=[
                (30.0, -6, -8, "2026-04-10"),
                (120.0, -5, -7, "2026-04-11"),
            ]
        )
        controller._get_pg = MagicMock(return_value=fake_conn)

        adjustments = SleepControllerV5._learn_from_history(controller)

        assert adjustments == {"1": -25, "2": -24}
        assert "controller_version = %s" in fake_conn.cursor_obj.query
        assert fake_conn.cursor_obj.params[0] == ctrl_module.CONTROLLER_VERSION


class TestBodySafety:
    def test_ambiguous_body_temp_does_not_trigger_hot_safety(self):
        controller = _make_controller()
        controller._compute_setting = SleepControllerV5._compute_setting.__get__(
            controller, SleepControllerV5
        )

        plan = controller._compute_setting(
            elapsed_min=180.0,
            room_temp=68.0,
            sleep_stage=None,
            body_avg=84.5,
            current_setting=-7,
        )

        assert plan["hot_safety"] is False
        assert controller._state["hot_streak"] == 0


class TestRightSideLogging:
    def test_right_side_override_uses_temperature_reader(self):
        controller = _make_controller()
        controller._is_sleeping = MagicMock(return_value=True)
        controller._log_override = MagicMock()
        controller._read_temperature = MagicMock(return_value=68.0)
        controller._read_str = MagicMock(return_value="core")
        controller._read_zone_snapshot = MagicMock(return_value=_snapshot(setting=-6, blower_pct=50))

        SleepControllerV5._on_right_setting_change(
            controller,
            ctrl_module.ZONE_ENTITY_IDS["right"]["bedtime"],
            None,
            "-6",
            "-5",
            {},
        )

        controller._read_temperature.assert_called_once_with(ctrl_module.DEFAULT_ROOM_TEMP_ENTITY)
        assert controller._log_override.call_args.kwargs["room_temp"] == 68.0


class TestPostgresLogging:
    def test_notes_label_proxy_and_actual_blower(self):
        controller = _make_controller()
        fake_conn = _FakeConn()
        controller._get_pg = MagicMock(return_value=fake_conn)

        SleepControllerV5._log_to_postgres(
            controller,
            elapsed_min=60.0,
            room_temp=72.0,
            sleep_stage="unknown",
            body_center=84.0,
            setting=-7,
            cycle_num=1,
            room_temp_comp=8,
            data_source="time_cycle+room",
            body_avg=84.0,
            body_left=83.5,
            body_right=84.5,
            action="set",
            ambient=76.0,
            setpoint=72.0,
            effective=-7,
            baseline=-10,
            learned_adj=0,
            blower_pct=65,
            target_blower_pct=75,
            base_blower_pct=100,
            responsive_cooling_on=False,
            bed_presence=_bed_presence_snapshot(),
        )

        notes = fake_conn.cursor_obj.params[15]
        assert "base_proxy_blower=100" in notes
        assert "proxy_blower=75" in notes
        assert "actual_blower=65" in notes
        assert "rc=off" in notes
        assert fake_conn.cursor_obj.params[16] == ctrl_module.CONTROLLER_VERSION
        assert fake_conn.cursor_obj.params[17] == 80.5
        assert fake_conn.cursor_obj.params[30] is False
        assert fake_conn.cursor_obj.closed is True

    def test_override_notes_label_proxy_and_actual_blower(self):
        controller = _make_controller()
        fake_conn = _FakeConn()
        controller._get_pg = MagicMock(return_value=fake_conn)
        controller._get_cycle_num = MagicMock(return_value=1)

        SleepControllerV5._log_override(
            controller,
            zone="left",
            value=-8,
            controller_value=-10,
            delta=2,
            room_temp=72.0,
            sleep_stage="unknown",
            snapshot=_snapshot(setting=-10, blower_pct=33),
        )

        notes = fake_conn.cursor_obj.params[15]
        assert "controller_proxy_blower=100" in notes
        assert "override_proxy_blower=75" in notes
        assert "actual_blower=33" in notes


class TestBodyFeedback:
    """v5.2: closed-loop body-temperature feedback on cycle baselines."""

    def _fresh_controller(self):
        c = SleepControllerV5()
        c._learned = {}
        c._state = {"current_cycle_num": 0}
        return c

    def test_skip_in_cycles_1_2(self):
        """Bedtime aggressive cooling — skip feedback regardless of body."""
        c = self._fresh_controller()
        # cycle 1 (elapsed_min < 90)
        plan = c._compute_setting(elapsed_min=10, room_temp=68.0, sleep_stage=None,
                                  body_avg=72.0)
        assert plan["base_setting"] == ctrl_module.CYCLE_SETTINGS[1]
        # cycle 2 (90 <= elapsed_min < 180)
        plan = c._compute_setting(elapsed_min=120, room_temp=68.0, sleep_stage=None,
                                  body_avg=72.0)
        assert plan["base_setting"] == ctrl_module.CYCLE_SETTINGS[2]

    def test_no_correction_when_body_at_or_above_target(self):
        c = self._fresh_controller()
        for body in (86.0, 88.0, 90.0):
            plan = c._compute_setting(elapsed_min=200, room_temp=68.0,
                                      sleep_stage=None, body_avg=body)
            assert plan["base_setting"] == ctrl_module.CYCLE_SETTINGS[3], \
                f"body={body} should not trigger correction"

    def test_correction_when_body_below_target(self):
        """body 6°F below target → +3.3 → +3 settings warmer."""
        c = self._fresh_controller()
        plan = c._compute_setting(elapsed_min=200, room_temp=68.0,
                                  sleep_stage=None, body_avg=80.0)
        # cycle 3 baseline -7 + 3 = -4
        assert plan["base_setting"] == ctrl_module.CYCLE_SETTINGS[3] + 3

    def test_correction_capped(self):
        """body 12°F below target → +6.6 → cap at +5."""
        c = self._fresh_controller()
        plan = c._compute_setting(elapsed_min=200, room_temp=68.0,
                                  sleep_stage=None, body_avg=74.0)
        cap = ctrl_module.BODY_FB_MAX_DELTA
        expected = max(-10, ctrl_module.CYCLE_SETTINGS[3] + cap)
        assert plan["base_setting"] == expected

    def test_no_correction_when_body_missing(self):
        c = self._fresh_controller()
        plan = c._compute_setting(elapsed_min=200, room_temp=68.0,
                                  sleep_stage=None, body_avg=None)
        assert plan["base_setting"] == ctrl_module.CYCLE_SETTINGS[3]

    def test_correction_does_not_exceed_zero(self):
        """If correction would push setting above 0 (heating), clamp at 0."""
        c = self._fresh_controller()
        # Synthetic cycle baseline -1, body very low → +5 cap → -1+5=+4 → clamp 0
        original = dict(ctrl_module.CYCLE_SETTINGS)
        try:
            ctrl_module.CYCLE_SETTINGS[3] = -1
            plan = c._compute_setting(elapsed_min=200, room_temp=68.0,
                                      sleep_stage=None, body_avg=72.0)
            assert plan["base_setting"] == 0  # MAX_SETTING
        finally:
            ctrl_module.CYCLE_SETTINGS.clear()
            ctrl_module.CYCLE_SETTINGS.update(original)

    def test_data_source_logs_correction(self):
        c = self._fresh_controller()
        plan = c._compute_setting(elapsed_min=200, room_temp=68.0,
                                  sleep_stage=None, body_avg=80.0)
        assert "body_fb" in plan["data_source"]

    def test_constants_are_locked(self):
        """v5.2 constants pinned by parse-time AST so a refactor can't drift."""
        import ast as _ast
        src = CONTROLLER_PATH.read_text()
        consts = {}
        for node in _ast.walk(_ast.parse(src)):
            if isinstance(node, _ast.Assign) and len(node.targets) == 1:
                t = node.targets[0]
                if not isinstance(t, _ast.Name):
                    continue
                v = node.value
                if isinstance(v, _ast.Constant):
                    consts[t.id] = v.value
                elif (isinstance(v, _ast.UnaryOp) and isinstance(v.op, _ast.USub)
                      and isinstance(v.operand, _ast.Constant)):
                    consts[t.id] = -v.operand.value
        assert consts.get("CONTROLLER_VERSION") == "v5_2_rc_off"
        assert consts.get("BODY_FB_ENABLED") is True
        assert consts.get("BODY_FB_TARGET_F") == 86.0
        assert consts.get("BODY_FB_KP_COLD") == 0.55
        assert consts.get("BODY_FB_MAX_DELTA") == 5
        assert consts.get("BODY_FB_MIN_CYCLE") == 3
