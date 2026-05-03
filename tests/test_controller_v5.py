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

    def run_in(self, *args, **kwargs):
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


class _FilteringCursor(_FakeCursor):
    def __init__(self, rows_with_notes):
        super().__init__(rows=[])
        self.rows_with_notes = rows_with_notes

    def execute(self, query, params=None):
        super().execute(query, params)
        excluded = ("initial_bed_cooling", "bedjet_window", "pre_sleep")
        if all(f"NOT LIKE '%{tag}%'" in query for tag in excluded):
            self.rows = [row[:4] for row in self.rows_with_notes
                         if not any(tag in (row[4] or "") for tag in excluded)]
        else:
            self.rows = [row[:4] for row in self.rows_with_notes]


class _FilteringConn(_FakeConn):
    def __init__(self, rows_with_notes):
        self.cursor_obj = _FilteringCursor(rows_with_notes)
        self.commit = MagicMock()


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
    occupied_since = (datetime.now() - timedelta(minutes=60)).isoformat()
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
        "left_last_data_source": None,
        "right_last_data_source": None,
        "override_freeze_until": None,
        "manual_mode": False,
        "recent_changes": [],
        "override_count": 0,
        "body_below_since": None,
        "hot_streak": 0,
        "right_hot_streak": 0,
        "right_rail_force_seen": False,
        "right_rail_force_seen_at": None,
        "right_rail_helper_seen_on_at": None,
        "current_cycle_num": None,
        "left_zone_last_occupied": True,
        "left_zone_occupied_since": occupied_since,
        "left_bed_onset_ts": occupied_since,
        "left_bed_vacated_since": None,
        "right_zone_last_occupied": True,
        "right_zone_occupied_since": occupied_since,
        "right_bed_onset_ts": occupied_since,
        "right_bed_vacated_since": None,
    }
    controller._learned = {}
    controller._pg_conn = None
    controller.call_service = MagicMock()
    controller.get_state = MagicMock(return_value=None)
    controller.log = MagicMock()
    controller.run_in = MagicMock()
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
            "overheat_hard": False,
        }
    )
    return controller


def _set_states(controller, *, running="on", sleep_mode="on", responsive="off", responsive_right="off"):
    def _get_state(entity_id, **kwargs):
        if kwargs.get("attribute") == "last_changed":
            return None
        return {
            ctrl_module.E_RUNNING: running,
            ctrl_module.E_SLEEP_MODE: sleep_mode,
            ctrl_module.E_RESPONSIVE_COOLING: responsive,
            ctrl_module.E_RESPONSIVE_COOLING_RIGHT: responsive_right,
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

        controller.call_service.assert_any_call(
            "switch/turn_off",
            entity_id=ctrl_module.E_RESPONSIVE_COOLING,
        )
        controller.call_service.assert_any_call(
            "switch/turn_off",
            entity_id=ctrl_module.E_RESPONSIVE_COOLING_RIGHT,
        )
        assert controller.call_service.call_count == 2
        assert controller._room_temp_entity == ctrl_module.DEFAULT_ROOM_TEMP_ENTITY

    def test_control_loop_turns_responsive_cooling_back_off(self):
        controller = _make_controller()
        _set_states(controller, running="on", responsive="on", responsive_right="on")

        controller._control_loop({})

        controller.call_service.assert_any_call(
            "switch/turn_off",
            entity_id=ctrl_module.E_RESPONSIVE_COOLING,
        )
        controller.call_service.assert_any_call(
            "switch/turn_off",
            entity_id=ctrl_module.E_RESPONSIVE_COOLING_RIGHT,
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
    def test_no_floor_after_freeze_returns_to_algorithmic(self):
        """2026-05-01: Override floor was removed. After the 60-min freeze
        elapses, the controller resumes algorithmic decisions (does NOT clamp
        to the user's last manual value). User overrides become learning data
        points consumed cross-night via _learn_from_history, not night-long
        floors."""
        controller = _make_controller(current_setting=-6, current_blower=50)
        _set_states(controller, running="on", responsive="off")
        # Freeze has elapsed (1 minute ago). Algorithm wants -10.
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

        # Algorithm should drive to -10, NOT clamp to a user-supplied floor.
        controller.call_service.assert_called_with(
            "number/set_value",
            entity_id=ctrl_module.E_BEDTIME_TEMP,
            value=-10,
        )
        # No floor state remains in the controller.
        assert "override_floor" not in controller._state
        # Logging path passes None for legacy override_floor kwarg.
        assert controller._log_to_postgres.call_args.kwargs["override_floor"] is None


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


class TestSafetyBypass:
    def _safety_plan(self, setting=-10, *, overheat_hard=False, hot_safety=False):
        return {
            "setting": setting,
            "target_blower_pct": ctrl_module.L1_TO_BLOWER_PCT[setting],
            "base_setting": -5,
            "base_blower_pct": ctrl_module.L1_TO_BLOWER_PCT[-5],
            "cycle_num": 1,
            "room_temp_comp": 0,
            "learned_adj_pct": 0,
            "data_source": "time_cycle+safety",
            "hot_safety": hot_safety,
            "overheat_hard": overheat_hard,
        }

    def test_overheat_hard_bypasses_freeze(self):
        controller = _make_controller(current_setting=-5, current_blower=41)
        _set_states(controller, running="on", responsive="off")
        controller._state["override_freeze_until"] = (
            datetime.now() + timedelta(minutes=30)
        ).isoformat()
        controller._compute_setting.return_value = self._safety_plan(overheat_hard=True)

        controller._control_loop({})

        controller.call_service.assert_any_call(
            "number/set_value", entity_id=ctrl_module.E_BEDTIME_TEMP, value=-10
        )
        assert controller._log_to_postgres.call_args.kwargs["action"] == "overheat_hard"

    def test_overheat_hard_bypasses_manual_mode(self):
        controller = _make_controller(current_setting=-5, current_blower=41)
        _set_states(controller, running="on", responsive="off")
        controller._state["manual_mode"] = True
        controller._compute_setting.return_value = self._safety_plan(overheat_hard=True)

        controller._control_loop({})

        controller.call_service.assert_any_call(
            "number/set_value", entity_id=ctrl_module.E_BEDTIME_TEMP, value=-10
        )
        assert controller._log_to_postgres.call_args.kwargs["action"] == "overheat_hard"

    def test_hot_safety_bypasses_freeze_but_not_manual_mode(self):
        controller = _make_controller(current_setting=-5, current_blower=41)
        _set_states(controller, running="on", responsive="off")
        controller._state["override_freeze_until"] = (
            datetime.now() + timedelta(minutes=30)
        ).isoformat()
        controller._compute_setting.return_value = self._safety_plan(
            setting=-6, hot_safety=True
        )

        controller._control_loop({})

        controller.call_service.assert_any_call(
            "number/set_value", entity_id=ctrl_module.E_BEDTIME_TEMP, value=-6
        )
        assert controller._log_to_postgres.call_args.kwargs["action"] == "hot_safety"

        manual = _make_controller(current_setting=-5, current_blower=41)
        _set_states(manual, running="on", responsive="off")
        manual._state["manual_mode"] = True
        manual._compute_setting.return_value = self._safety_plan(setting=-6, hot_safety=True)

        manual._control_loop({})

        manual.call_service.assert_not_called()
        assert manual._log_to_postgres.call_args.kwargs["action"] == "manual_hold"



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

    def test_learner_excludes_initial_bed_cooling_overrides(self):
        controller = _make_controller()
        fake_conn = _FakeConn(rows=[])
        controller._get_pg = MagicMock(return_value=fake_conn)

        SleepControllerV5._learn_from_history(controller)

        query = fake_conn.cursor_obj.query
        assert "COALESCE(notes, '') NOT LIKE '%initial_bed_cooling%'" in query
        assert "COALESCE(notes, '') NOT LIKE '%bedjet_window%'" in query
        assert "COALESCE(notes, '') NOT LIKE '%pre_sleep%'" in query


    def test_learner_filter_excludes_tagged_rows_from_deltas(self):
        controller = _make_controller()
        fake_conn = _FilteringConn(
            rows_with_notes=[
                (30.0, -6, -8, "2026-04-10", "state=initial_bed_cooling(10m)"),
                (120.0, -5, -7, "2026-04-11", "state=pre_sleep_precool"),
                (120.0, -4, -6, "2026-04-12", "state=cycle+body_fb+bedjet_window_20min"),
                (120.0, -5, -7, "2026-04-13", "state=cycle+body_fb"),
            ]
        )
        controller._get_pg = MagicMock(return_value=fake_conn)

        adjustments = SleepControllerV5._learn_from_history(controller)

        assert adjustments == {"2": -24}
        query = fake_conn.cursor_obj.query
        assert "COALESCE(notes, '') NOT LIKE '%initial_bed_cooling%'" in query
        assert "COALESCE(notes, '') NOT LIKE '%bedjet_window%'" in query
        assert "COALESCE(notes, '') NOT LIKE '%pre_sleep%'" in query


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


class TestRightRailWriteDetection:
    def _controller(self, *, body_left=87.0, helper_state="on", live_flag="on"):
        controller = _make_controller(current_setting=-5, current_blower=41)
        controller._is_sleeping = MagicMock(return_value=True)
        controller._state["right_hot_streak"] = ctrl_module.RIGHT_HOT_RAIL_STREAK
        controller._log_override = MagicMock()
        controller._save_state = MagicMock()
        controller._read_temperature = MagicMock(return_value=70.0)
        controller._read_bool = MagicMock(return_value=True)
        controller._read_zone_snapshot = MagicMock(
            return_value=_snapshot(setting=-5, blower_pct=41) | {"body_left": body_left}
        )

        def _read_str(entity_id):
            if entity_id == ctrl_module.E_RIGHT_RAIL_ENGAGED:
                return helper_state
            if entity_id == ctrl_module.E_RIGHT_CONTROLLER_FLAG:
                return live_flag
            if entity_id == ctrl_module.E_SLEEP_STAGE:
                return "core"
            return None

        controller._read_str = MagicMock(side_effect=_read_str)
        return controller

    def test_rail_force_helper_on_not_classified_as_override(self):
        controller = self._controller(body_left=87.0, helper_state="on")

        controller._on_right_setting_change(
            ctrl_module.ZONE_ENTITY_IDS["right"]["bedtime"], None, "-5", "-10", {}
        )

        assert "right_zone_override_until" not in controller._state
        assert controller._state["right_rail_force_seen"] is True
        assert controller._state.get("right_rail_helper_seen_on_at") is not None
        assert controller._log_override.call_args.kwargs["action"] == "rail_force"
        assert controller._log_override.call_args.kwargs["source"] == "rail_force"

    def test_rail_release_helper_on_not_classified_as_override(self):
        controller = self._controller(body_left=83.0, helper_state="on")

        controller._on_right_setting_change(
            ctrl_module.ZONE_ENTITY_IDS["right"]["bedtime"], None, "-10", "-5", {}
        )

        assert "right_zone_override_until" not in controller._state
        assert controller._state["right_rail_force_seen"] is False
        assert controller._log_override.call_args.kwargs["action"] == "rail_force"
        assert controller._log_override.call_args.kwargs["source"] == "rail_release"

    def test_user_max_cool_when_helper_off_still_classified_as_override(self):
        controller = self._controller(body_left=87.0, helper_state="off")

        controller._on_right_setting_change(
            ctrl_module.ZONE_ENTITY_IDS["right"]["bedtime"], None, "-5", "-10", {}
        )

        assert controller._state.get("right_zone_override_until") is not None
        assert controller._state["right_rail_force_seen"] is False
        assert controller._log_override.call_args.kwargs.get("action", "override") == "override"


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

        notes = fake_conn.cursor_obj.params[16]
        assert "controller_proxy_blower=100" in notes
        assert "override_proxy_blower=75" in notes
        assert "actual_blower=33" in notes

    def test_override_notes_include_last_control_state_for_excluded_windows(self):
        cases = [
            ("left", "initial_bed_cooling(12m)", "initial_bed_cooling"),
            ("left", "pre_sleep_precool", "pre_sleep"),
            ("right", "cycle+body_fb+bedjet_window_20min", "bedjet_window"),
        ]
        for zone, state, expected in cases:
            controller = _make_controller()
            controller._state[f"{zone}_last_data_source"] = state
            fake_conn = _FakeConn()
            controller._get_pg = MagicMock(return_value=fake_conn)
            controller._get_cycle_num = MagicMock(return_value=1)

            SleepControllerV5._log_override(
                controller,
                zone=zone,
                value=-8,
                controller_value=-10,
                delta=2,
                room_temp=72.0,
                sleep_stage="unknown",
                snapshot=_snapshot(setting=-10, blower_pct=33),
            )

            notes = fake_conn.cursor_obj.params[16]
            assert f"state={state}" in notes
            assert expected in notes

    def test_initial_override_after_sleep_on_is_tagged_initial_setting(self):
        controller = _make_controller(current_setting=-6, current_blower=50)
        controller._learn_from_history = MagicMock(return_value={})
        controller._save_learned = MagicMock()
        controller._ensure_responsive_cooling_off = MagicMock()
        controller._ensure_3_level_off = MagicMock()
        controller._read_temperature = MagicMock(return_value=72.0)
        controller._read_zone_snapshot = MagicMock(return_value=_snapshot(setting=-6, blower_pct=50))
        controller._read_str = MagicMock(return_value="unknown")
        controller.get_state = MagicMock(return_value="on")
        fake_conn = _FakeConn()
        controller._get_pg = MagicMock(return_value=fake_conn)
        controller._get_cycle_num = MagicMock(return_value=1)

        SleepControllerV5._on_sleep_mode(
            controller, ctrl_module.E_SLEEP_MODE, None, "off", "on", {}
        )
        SleepControllerV5._on_setting_change(
            controller, ctrl_module.E_BEDTIME_TEMP, None, "-6", "-5", {}
        )

        notes = fake_conn.cursor_obj.params[16]
        assert "state=initial_setting" in notes


class TestBedOnsetEvent:
    def _fresh_controller(self):
        c = _make_controller()
        c._state["left_bed_onset_ts"] = None
        c._state["left_zone_occupied_since"] = None
        c._state["left_bed_vacated_since"] = None
        c._compute_setting = SleepControllerV5._compute_setting.__get__(c, SleepControllerV5)
        c._learned = {}
        return c

    def test_bed_onset_event_schedules_immediate_tick(self):
        c = self._fresh_controller()

        c._on_bed_onset(
            ctrl_module.BED_PRESENCE_ENTITIES["occupied_left"],
            "state",
            "off",
            "on",
            {"zone": "left"},
        )

        c.run_in.assert_called_once_with(c._control_loop, 1)

    def test_bed_onset_event_sets_state_timestamp(self):
        c = self._fresh_controller()

        c._on_bed_onset(
            ctrl_module.BED_PRESENCE_ENTITIES["occupied_left"],
            "state",
            "off",
            "on",
            {"zone": "left"},
        )

        assert c._state["left_bed_onset_ts"] is not None
        datetime.fromisoformat(c._state["left_bed_onset_ts"])
        assert c._state["left_zone_occupied_since"] == c._state["left_bed_onset_ts"]
        c._save_state.assert_called()

    def test_initial_bed_cooling_uses_event_timestamp(self):
        c = self._fresh_controller()
        now = datetime.now()
        c._state["left_bed_onset_ts"] = (now - timedelta(minutes=10)).isoformat()
        c._state["left_zone_occupied_since"] = None
        c._state["left_zone_last_occupied"] = True

        mins_since_occupied = c._update_zone_occupancy_onset("left", True, now)
        plan = c._compute_setting(
            elapsed_min=200,
            room_temp=72.0,
            sleep_stage=None,
            body_avg=75.0,
            body_left=75.0,
            mins_since_occupied=mins_since_occupied,
            bed_occupied=True,
        )

        assert mins_since_occupied < ctrl_module.INITIAL_BED_COOLING_MIN
        assert plan["setting"] == ctrl_module.INITIAL_BED_LEFT_SETTING
        assert "initial_bed_cooling" in plan["data_source"]

    def test_brief_vacancy_retains_original_onset(self):
        c = self._fresh_controller()
        original = (datetime.now() - timedelta(hours=3)).isoformat()
        c._state["left_bed_onset_ts"] = original
        c._state["left_zone_occupied_since"] = original

        c._on_bed_vacated(
            ctrl_module.BED_PRESENCE_ENTITIES["occupied_left"],
            "state",
            "on",
            "off",
            {"zone": "left"},
        )
        c._on_bed_onset(
            ctrl_module.BED_PRESENCE_ENTITIES["occupied_left"],
            "state",
            "off",
            "on",
            {"zone": "left"},
        )

        assert c._state["left_bed_onset_ts"] == original


class TestBodyFbOccupancyGate:
    def _fresh_controller(self):
        c = SleepControllerV5()
        c._learned = {}
        c._state = {"current_cycle_num": 0, "hot_streak": 0}
        return c

    def test_body_fb_skipped_when_unoccupied(self):
        c = self._fresh_controller()
        plan = c._compute_setting(
            elapsed_min=10,
            room_temp=72.0,
            sleep_stage=None,
            body_avg=75.0,
            body_left=75.0,
            bed_occupied=False,
        )

        assert plan["base_setting"] == ctrl_module.CYCLE_SETTINGS[1]
        assert plan["setting"] == ctrl_module.CYCLE_SETTINGS[1]
        assert "body_fb_skipped(unoccupied)" in plan["data_source"]
        assert "body_fb(" not in plan["data_source"]

    def test_body_fb_applied_when_occupied(self):
        c = self._fresh_controller()
        plan = c._compute_setting(
            elapsed_min=10,
            room_temp=72.0,
            sleep_stage=None,
            body_avg=75.0,
            body_left=75.0,
            bed_occupied=True,
        )

        assert plan["base_setting"] == ctrl_module.CYCLE_SETTINGS[1] + ctrl_module.BODY_FB_MAX_DELTA
        assert "body_fb" in plan["data_source"]
        assert "skipped" not in plan["data_source"]

    def test_body_fb_skipped_when_occupancy_unknown(self):
        c = self._fresh_controller()
        plan = c._compute_setting(
            elapsed_min=10,
            room_temp=72.0,
            sleep_stage=None,
            body_avg=75.0,
            body_left=75.0,
            bed_occupied=None,
        )

        assert plan["base_setting"] == ctrl_module.CYCLE_SETTINGS[1]
        assert plan["setting"] == ctrl_module.CYCLE_SETTINGS[1]
        assert "body_fb_skipped(unknown_occupancy)" in plan["data_source"]
        assert "body_fb(" not in plan["data_source"]


class TestBodyFeedback:
    """v5.2: closed-loop body-temperature feedback on cycle baselines."""

    def _fresh_controller(self):
        c = SleepControllerV5()
        c._learned = {}
        c._state = {"current_cycle_num": 0}
        return c

    def test_early_sleep_feedback_warms_cycles_1_2(self):
        """After initial-bed gate expires, early cycles can warm from body feedback."""
        c = self._fresh_controller()
        plan = c._compute_setting(elapsed_min=10, room_temp=68.0, sleep_stage=None,
                                   body_avg=72.0, body_left=68.0,
                                   mins_since_occupied=45.0,
                                   bed_occupied=True)
        assert plan["base_setting"] == ctrl_module.CYCLE_SETTINGS[1] + ctrl_module.BODY_FB_MAX_DELTA
        assert "body_fb" in plan["data_source"]

        plan = c._compute_setting(elapsed_min=120, room_temp=68.0, sleep_stage=None,
                                   body_avg=72.0, body_left=68.0,
                                   bed_occupied=True)
        assert plan["base_setting"] == ctrl_module.CYCLE_SETTINGS[2] + ctrl_module.BODY_FB_MAX_DELTA
        assert "body_fb" in plan["data_source"]

    def test_inbed_pre_sleep_precooling_stays_aggressive(self):
        """Explicit in-bed/not-yet-asleep stage preserves intentional pre-cooling."""
        c = self._fresh_controller()
        plan = c._compute_setting(elapsed_min=10, room_temp=68.0, sleep_stage="inbed",
                                  body_avg=72.0, body_left=68.0)
        assert plan["setting"] == ctrl_module.INITIAL_BED_LEFT_SETTING
        assert plan["base_setting"] == ctrl_module.INITIAL_BED_LEFT_SETTING
        assert "body_fb" not in plan["data_source"]
        assert plan["room_temp_comp"] == 0
        assert plan["data_source"] == "pre_sleep_precool"

    def test_left_first_30_min_occupied_forces_max_despite_cold_body(self):
        """Occupancy-based initial-bed gate overrides body feedback and room comp."""
        c = self._fresh_controller()
        plan = c._compute_setting(elapsed_min=10, room_temp=60.0, sleep_stage=None,
                                  body_avg=72.0, body_left=68.0,
                                  mins_since_occupied=10.0)
        assert plan["setting"] == ctrl_module.INITIAL_BED_LEFT_SETTING
        assert plan["base_setting"] == ctrl_module.INITIAL_BED_LEFT_SETTING
        assert plan["target_blower_pct"] == 100
        assert plan["room_temp_comp"] == 0
        assert "body_fb" not in plan["data_source"]
        assert "initial_bed_cooling" in plan["data_source"]

    def test_deep_sleep_cycle_1_can_warm_from_stage_baseline(self):
        """After initial-bed gate, stage deep can warm from low body feedback."""
        c = self._fresh_controller()
        plan = c._compute_setting(elapsed_min=10, room_temp=68.0, sleep_stage="deep",
                                  body_avg=72.0, body_left=68.0,
                                  mins_since_occupied=45.0,
                                  bed_occupied=True)
        assert plan["base_setting"] == -10 + ctrl_module.BODY_FB_MAX_DELTA
        assert "body_fb" in plan["data_source"]

    def test_no_correction_when_body_at_or_above_target(self):
        c = self._fresh_controller()
        # body_left at/above 80°F target (was 86°F on body_avg)
        for body in (80.0, 82.0, 86.0):
            plan = c._compute_setting(elapsed_min=200, room_temp=68.0,
                                      sleep_stage=None,
                                      body_avg=body + 3, body_left=body,
                                      bed_occupied=True)
            assert plan["base_setting"] == ctrl_module.CYCLE_SETTINGS[3], \
                f"body_left={body} should not trigger correction"

    def test_correction_when_body_below_target(self):
        """body_left 2°F below target → 1.25*2 = 2.5 → +2 (banker's rounding)."""
        c = self._fresh_controller()
        plan = c._compute_setting(elapsed_min=200, room_temp=68.0,
                                  sleep_stage=None,
                                  body_avg=81.0, body_left=78.0,
                                  bed_occupied=True)
        # cycle 3 baseline -7 + 2 = -5 (round(1.25*2)=round(2.5)=2 banker's)
        assert plan["base_setting"] == ctrl_module.CYCLE_SETTINGS[3] + 2

    def test_correction_capped(self):
        """body_left 8°F below target → 1.25*8=10 → cap at +5."""
        c = self._fresh_controller()
        plan = c._compute_setting(elapsed_min=200, room_temp=68.0,
                                  sleep_stage=None,
                                  body_avg=75.0, body_left=72.0,
                                  bed_occupied=True)
        cap = ctrl_module.BODY_FB_MAX_DELTA
        expected = max(-10, ctrl_module.CYCLE_SETTINGS[3] + cap)
        assert plan["base_setting"] == expected

    def test_no_correction_when_body_missing(self):
        c = self._fresh_controller()
        plan = c._compute_setting(elapsed_min=200, room_temp=68.0,
                                  sleep_stage=None,
                                  body_avg=None, body_left=None,
                                  bed_occupied=True)
        assert plan["base_setting"] == ctrl_module.CYCLE_SETTINGS[3]

    def test_correction_does_not_exceed_zero(self):
        """If correction would push setting above 0 (heating), clamp at 0."""
        c = self._fresh_controller()
        original = dict(ctrl_module.CYCLE_SETTINGS)
        try:
            ctrl_module.CYCLE_SETTINGS[3] = -1
            plan = c._compute_setting(elapsed_min=200, room_temp=68.0,
                                      sleep_stage=None,
                                      body_avg=75.0, body_left=72.0,
                                      bed_occupied=True)
            assert plan["base_setting"] == 0  # MAX_SETTING
        finally:
            ctrl_module.CYCLE_SETTINGS.clear()
            ctrl_module.CYCLE_SETTINGS.update(original)

    def test_data_source_logs_correction(self):
        c = self._fresh_controller()
        plan = c._compute_setting(elapsed_min=200, room_temp=68.0,
                                  sleep_stage=None,
                                  body_avg=81.0, body_left=78.0)
        assert "body_fb" in plan["data_source"]

    def test_input_is_body_left_when_configured(self):
        """When BODY_FB_INPUT='body_left', body_avg deviation alone shouldn't fire."""
        c = self._fresh_controller()
        # body_left ABOVE target → no correction even if body_avg is well below
        plan = c._compute_setting(elapsed_min=200, room_temp=68.0,
                                  sleep_stage=None,
                                  body_avg=70.0,   # would trigger if input=body_avg
                                  body_left=82.0,  # above target → no fire
                                  bed_occupied=True)
        assert plan["base_setting"] == ctrl_module.CYCLE_SETTINGS[3]

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
        assert consts.get("BODY_FB_INPUT") == "body_left"
        assert consts.get("BODY_FB_TARGET_F") == 80.0
        assert consts.get("BODY_FB_KP_COLD") == 1.25
        assert consts.get("BODY_FB_MAX_DELTA") == 5
        assert consts.get("BODY_FB_MIN_CYCLE") == 1
        assert consts.get("INITIAL_BED_COOLING_MIN") == 30.0
        assert consts.get("INITIAL_BED_LEFT_SETTING") == -10
        assert consts.get("INITIAL_BED_RIGHT_SETTING") == -10


class TestRightRoomCompensation:
    """Right-zone room compensation: shared 72°F reference, wife-specific gains."""

    def _fresh_controller(self):
        c = SleepControllerV5()
        c._learned = {}
        c._state = {}
        return c

    def test_constants_are_safe_and_hot_only(self):
        assert ctrl_module.RIGHT_ROOM_BLOWER_REFERENCE_F == ctrl_module.ROOM_BLOWER_REFERENCE_F
        assert ctrl_module.RIGHT_ROOM_BLOWER_REFERENCE_F == 72.0
        assert ctrl_module.RIGHT_ROOM_BLOWER_HOT_COMP_PER_F == 4.0
        assert ctrl_module.RIGHT_ROOM_BLOWER_COLD_COMP_PER_F == 0.0

    def test_cold_room_has_zero_right_compensation(self):
        c = self._fresh_controller()
        assert c._right_room_temp_to_blower_comp(67.0) == 0
        assert c._right_room_temp_to_blower_comp(68.3) == 0
        assert c._right_room_temp_to_blower_comp(72.0) == 0

    def test_hot_room_adds_only_cooling_blower_points(self):
        c = self._fresh_controller()
        assert c._right_room_temp_to_blower_comp(72.2) == 1
        assert c._right_room_temp_to_blower_comp(75.0) == 12


class TestRightEarlySleepFeedback:
    """Right-zone controller no longer skips body feedback in cycle 1 during sleep."""

    def _fresh_controller(self, monkeypatch):
        import builtins
        import io
        from datetime import datetime as _dt, timedelta as _td

        c = SleepControllerV5()
        c._state = {
            "right_zone_last_occupied": True,
            "right_zone_occupied_since": (_dt.now() - _td(minutes=45)).isoformat(),
        }
        c._save_state = lambda: None
        c._set_l1_right = lambda value: None
        c.log = lambda *args, **kwargs: None
        c._read_str = lambda entity_id: "off"
        c._read_bool = lambda entity_id: (
            True if entity_id == ctrl_module.BED_PRESENCE_ENTITIES["occupied_right"] else None
        )
        sink = io.StringIO()

        class _FakeOpen:
            def __enter__(self):
                return sink

            def __exit__(self, exc_type, exc, tb):
                return False

        monkeypatch.setattr(builtins, "open", lambda *args, **kwargs: _FakeOpen())
        return c, sink

    def test_right_cycle_1_cold_body_feedback_warms(self, monkeypatch):
        import json

        c, sink = self._fresh_controller(monkeypatch)
        c._right_v52_shadow_tick(
            elapsed_min=10,
            room_temp=68.0,
            sleep_stage="core",
            right_snap=_snapshot(setting=-8, blower_pct=75) | {"body_left": 76.0, "body_avg": 78.0},
        )
        entry = json.loads(sink.getvalue().strip())
        assert entry["cycle"] == 1
        assert entry["right_v52_base"] == ctrl_module.RIGHT_CYCLE_SETTINGS[1]
        assert entry["right_v52_correction"] == 1
        assert entry["right_v52_body_proposed"] == ctrl_module.RIGHT_CYCLE_SETTINGS[1] + 1
        assert entry["right_v52_reason"].startswith("cold_")

    def test_right_first_30_min_occupied_forces_max_despite_cold_body(self, monkeypatch):
        import json
        from datetime import datetime as _dt, timedelta as _td

        c, sink = self._fresh_controller(monkeypatch)
        c._state["right_zone_occupied_since"] = (_dt.now() - _td(minutes=10)).isoformat()
        c._right_v52_shadow_tick(
            elapsed_min=10,
            room_temp=68.0,
            sleep_stage="core",
            right_snap=_snapshot(setting=-8, blower_pct=75) | {"body_left": 76.0, "body_avg": 78.0},
        )
        entry = json.loads(sink.getvalue().strip())
        assert entry["in_initial_bed_cooling"] is True
        assert entry["right_v52_correction"] == 0
        assert entry["right_v52_body_proposed"] == ctrl_module.INITIAL_BED_RIGHT_SETTING
        assert entry["right_v52_proposed"] == ctrl_module.INITIAL_BED_RIGHT_SETTING
        assert entry["right_v52_source"] == "initial_bed_cooling"
        assert entry["right_v52_reason"].startswith("initial_bed_cooling_")

    def test_right_inbed_pre_sleep_feedback_suppressed(self, monkeypatch):
        import json

        c, sink = self._fresh_controller(monkeypatch)
        c._right_v52_shadow_tick(
            elapsed_min=10,
            room_temp=68.0,
            sleep_stage="inbed",
            right_snap=_snapshot(setting=-8, blower_pct=75) | {"body_left": 76.0, "body_avg": 78.0},
        )
        entry = json.loads(sink.getvalue().strip())
        assert entry["right_v52_correction"] == 0
        assert entry["right_v52_body_proposed"] == ctrl_module.INITIAL_BED_RIGHT_SETTING
        assert entry["right_v52_proposed"] == ctrl_module.INITIAL_BED_RIGHT_SETTING
        assert entry["right_v52_reason"] == "pre_sleep_inbed"
        assert entry["right_v52_source"] == "pre_sleep_precool"

    def test_right_live_write_suppressed_while_rail_helper_on(self, monkeypatch):
        import json

        c, sink = self._fresh_controller(monkeypatch)
        c._set_l1_right = MagicMock()
        logs = []
        c.log = lambda msg, *args, **kwargs: logs.append(msg)

        def _read_str(entity_id):
            if entity_id in (ctrl_module.E_RIGHT_CONTROLLER_FLAG, ctrl_module.E_RIGHT_RAIL_ENGAGED):
                return "on"
            return "off"

        c._read_str = _read_str
        c._right_v52_shadow_tick(
            elapsed_min=45,
            room_temp=72.0,
            sleep_stage="core",
            right_snap=_snapshot(setting=-5, blower_pct=41) | {"body_left": 82.0, "body_avg": 82.0},
        )

        entry = json.loads(sink.getvalue().strip())
        assert entry["actuation_blocked"] == "rail_engaged"
        assert entry["actuated"] is False
        c._set_l1_right.assert_not_called()
        assert any("right_zone_suppressed=rail_engaged" in msg for msg in logs)

    def test_right_sensor_invalid_idle_writes_zero(self, monkeypatch):
        """2026-05-03 fix: empty-bed-equilibrium body sensor → write L1=0 to let firmware idle."""
        import json
        c, sink = self._fresh_controller(monkeypatch)
        # body=72°F, room=70°F → delta=2°F, below 6°F threshold → sensor invalid
        c._right_v52_shadow_tick(
            elapsed_min=200,  # mid-sleep, past initial-bed window
            room_temp=70.0,
            sleep_stage="core",
            right_snap=_snapshot(setting=-5, blower_pct=100) | {"body_left": 72.0, "body_avg": 73.0},
        )
        entry = json.loads(sink.getvalue().strip())
        assert entry["right_v52_body_proposed"] == 0
        assert entry["right_v52_proposed"] == 0
        assert entry["right_v52_source"] == "sensor_invalid_idle"
        assert "sensor_invalid_idle" in entry["right_v52_reason"]

    def test_right_sensor_valid_at_threshold(self, monkeypatch):
        """body - room == 6°F (boundary): sensor counts as valid → normal control."""
        import json
        c, sink = self._fresh_controller(monkeypatch)
        # body=76°F, room=70°F → delta=6°F, exactly at threshold → valid
        c._right_v52_shadow_tick(
            elapsed_min=200,
            room_temp=70.0,
            sleep_stage="core",
            right_snap=_snapshot(setting=-5, blower_pct=100) | {"body_left": 76.0, "body_avg": 76.0},
        )
        entry = json.loads(sink.getvalue().strip())
        assert entry["right_v52_source"] != "sensor_invalid_idle"
        # body 76 < target 80 → cold delta -4 → Kp_cold=0.3*4 = 1.2 → +1 warmer
        assert entry["right_v52_correction"] == 1

    def test_right_sensor_invalid_does_not_override_initial_bed_cooling(self, monkeypatch):
        """During initial_bed_cooling, write -10 even if sensor reads 'invalid' (empty-bed pattern at bed-onset)."""
        import json
        from datetime import datetime as _dt, timedelta as _td

        c, sink = self._fresh_controller(monkeypatch)
        c._state["right_zone_occupied_since"] = (_dt.now() - _td(minutes=10)).isoformat()
        c._right_v52_shadow_tick(
            elapsed_min=10,
            room_temp=70.0,
            sleep_stage="core",
            right_snap=_snapshot(setting=-8, blower_pct=75) | {"body_left": 72.0, "body_avg": 73.0},
        )
        entry = json.loads(sink.getvalue().strip())
        assert entry["in_initial_bed_cooling"] is True
        assert entry["right_v52_proposed"] == ctrl_module.INITIAL_BED_RIGHT_SETTING
        # Initial-bed-cooling source wins, not sensor_invalid_idle
        assert entry["right_v52_source"] == "initial_bed_cooling"

    def test_right_sensor_invalid_constants_locked(self):
        assert ctrl_module.RIGHT_BODY_SENSOR_VALID_DELTA_F == 6.0
        assert ctrl_module.RIGHT_SENSOR_INVALID_IDLE_SETTING == 0
        # Patch level token must include the new gate
        assert "+rightSensorValidGate" in ctrl_module.CONTROLLER_PATCH_LEVEL


class TestHotRailNotesFlow:
    def test_hot_rail_appears_in_passive_snapshot_notes(self, monkeypatch):
        import builtins
        import io

        c = SleepControllerV5()
        onset = (datetime.now() - timedelta(minutes=45)).isoformat()
        c._state = {
            "right_zone_last_occupied": True,
            "right_zone_occupied_since": onset,
            "right_bed_onset_ts": onset,
            "right_hot_streak": ctrl_module.RIGHT_HOT_RAIL_STREAK - 1,
        }
        c._learned = {}
        c._save_state = lambda: None
        c._set_l1_right = lambda value: None
        c.log = lambda *args, **kwargs: None
        c._read_str = lambda entity_id: "off"
        c._read_bool = lambda entity_id: (
            True if entity_id == ctrl_module.BED_PRESENCE_ENTITIES["occupied_right"] else None
        )
        sink = io.StringIO()

        class _FakeOpen:
            def __enter__(self):
                return sink

            def __exit__(self, exc_type, exc, tb):
                return False

        monkeypatch.setattr(builtins, "open", lambda *args, **kwargs: _FakeOpen())
        right_snap = _snapshot(setting=-6, blower_pct=50) | {"body_left": 87.0, "body_avg": 85.0}

        entry = c._right_v52_shadow_tick(
            elapsed_min=45,
            room_temp=72.0,
            sleep_stage="core",
            right_snap=right_snap,
        )
        fake_conn = _FakeConn()
        c._get_pg = MagicMock(return_value=fake_conn)

        c._log_passive_zone_snapshot(
            "right",
            elapsed_min=45,
            room_temp=72.0,
            sleep_stage="core",
            bed_presence=_bed_presence_snapshot() | {"occupied_right": True},
            snapshot=right_snap,
            data_source_suffix=entry["right_v52_source"] if entry["hot_rail_fired"] else None,
        )

        notes = fake_conn.cursor_obj.params[15]
        assert entry["hot_rail_fired"] is True
        assert "hot_rail" in entry["right_v52_source"]
        assert "hot_rail" in notes


class TestSelfWriteRace:
    def test_left_self_write_does_not_trigger_override(self):
        controller = _make_controller(current_setting=-5, current_blower=41)
        controller._is_sleeping = MagicMock(return_value=True)
        controller._log_override = MagicMock()
        controller._read_temperature = MagicMock(return_value=70.0)
        controller._read_zone_snapshot = MagicMock(return_value=_snapshot(setting=-8, blower_pct=75))
        controller._read_str = MagicMock(return_value="core")
        controller._state["recent_changes"] = []

        def _echo(*args, **kwargs):
            controller._on_setting_change(ctrl_module.E_BEDTIME_TEMP, None, "-5", "-8", {})

        controller.call_service = MagicMock(side_effect=_echo)

        controller._set_l1(-8)

        controller._log_override.assert_not_called()
        assert controller._state.get("override_freeze_until") is None
        assert controller._state["recent_changes"] == []



class TestRightZoneLiveGate:
    """v5.2: right-zone live-actuation two-key arming and gates."""

    def _fresh(self):
        c = SleepControllerV5()
        c._state = {}
        c._set_calls = []
        c._service_calls = []
        # Capture calls via monkeypatch
        c.call_service = lambda *args, **kw: c._service_calls.append((args, kw))
        c.log = lambda *args, **kw: None
        return c

    def test_default_constants_safe(self):
        """Code key armed; HA helper key is the operational kill switch."""
        # RIGHT_LIVE_ENABLED is armed in code so the user can flip the HA
        # helper in UI without a redeploy. The HA helper defaults off.
        assert ctrl_module.RIGHT_LIVE_ENABLED is True
        # Helper entity must exist before this could matter
        assert ctrl_module.E_RIGHT_CONTROLLER_FLAG == \
            "input_boolean.snug_right_controller_enabled"

    def test_set_l1_right_writes_and_records(self):
        c = self._fresh()
        c._set_l1_right(-5)
        assert c._state["right_zone_last_setting"] == -5
        assert any(args[0:2] == ("number/set_value",) or
                   args[0] == "number/set_value"
                   for args, kw in c._service_calls)
        # Above-zero clamped
        c._set_l1_right(2)
        assert c._state["right_zone_last_setting"] == 0
        # Below -10 clamped
        c._set_l1_right(-50)
        assert c._state["right_zone_last_setting"] == -10

    def test_freeze_active_during_window(self):
        c = self._fresh()
        from datetime import datetime as _dt, timedelta as _td
        c._state["right_zone_override_until"] = (_dt.now() + _td(minutes=30)).isoformat()
        assert c._right_zone_in_freeze(_dt.now()) is True
        # Past freeze
        c._state["right_zone_override_until"] = (_dt.now() - _td(minutes=1)).isoformat()
        assert c._right_zone_in_freeze(_dt.now()) is False

    def test_freeze_no_history_returns_false(self):
        c = self._fresh()
        from datetime import datetime as _dt
        assert c._right_zone_in_freeze(_dt.now()) is False

    def test_rate_ok_no_history(self):
        c = self._fresh()
        from datetime import datetime as _dt
        assert c._right_zone_rate_ok(_dt.now()) is True

    def test_rate_blocks_within_interval(self):
        c = self._fresh()
        from datetime import datetime as _dt, timedelta as _td
        # Last change 5 min ago — interval is 30 min, should block
        c._state["right_zone_last_change_ts"] = (_dt.now() - _td(minutes=5)).isoformat()
        assert c._right_zone_rate_ok(_dt.now()) is False
        # Last change 35 min ago — should allow
        c._state["right_zone_last_change_ts"] = (_dt.now() - _td(minutes=35)).isoformat()
        assert c._right_zone_rate_ok(_dt.now()) is True

    def test_self_write_not_classified_as_override(self):
        """If the controller wrote -5 and HA echoes -5, _on_right_setting_change
        must NOT engage the freeze."""
        c = self._fresh()
        c._is_sleeping = lambda: True
        c._read_str = lambda eid: "on"
        c._read_temperature = lambda *a, **kw: 70
        c._read_zone_snapshot = lambda zone: {}
        c._log_override = lambda *a, **kw: None
        c._state["right_zone_last_setting"] = -5
        # Simulate HA echoing back the controller's write
        c._on_right_setting_change("entity", "state", "0", "-5", {})
        assert "right_zone_override_until" not in c._state, \
            "Self-write must not engage override freeze."

    def test_user_change_engages_freeze_when_live(self):
        c = self._fresh()
        c._is_sleeping = lambda: True
        c._read_str = lambda eid: "on"  # HA flag on
        c._read_temperature = lambda *a, **kw: 70
        c._read_zone_snapshot = lambda zone: {}
        c._log_override = lambda *a, **kw: None
        # Pretend RIGHT_LIVE_ENABLED is True for this test
        original = ctrl_module.RIGHT_LIVE_ENABLED
        try:
            ctrl_module.RIGHT_LIVE_ENABLED = True
            c._on_right_setting_change("entity", "state", "-4", "-7", {})
            assert "right_zone_override_until" in c._state
        finally:
            ctrl_module.RIGHT_LIVE_ENABLED = original

    def test_user_change_no_freeze_when_dead(self):
        """When the system is not armed (Python const False OR HA helper off),
        manual changes still log but don't bother engaging a freeze (controller
        can't actuate anyway)."""
        c = self._fresh()
        c._is_sleeping = lambda: True
        c._read_str = lambda eid: "off"  # HA flag OFF — dead arm
        c._read_temperature = lambda *a, **kw: 70
        c._read_zone_snapshot = lambda zone: {}
        c._log_override = lambda *a, **kw: None
        # Even with RIGHT_LIVE_ENABLED True (default), HA helper off blocks freeze
        c._on_right_setting_change("entity", "state", "-4", "-7", {})
        assert "right_zone_override_until" not in c._state

    def test_constants_are_locked_right_live(self):
        """AST-pin the right-zone live actuation constants."""
        import ast
        src = CONTROLLER_PATH.read_text()
        tree = ast.parse(src)
        consts = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign) and len(node.targets) == 1:
                t = node.targets[0]
                if not isinstance(t, ast.Name):
                    continue
                v = node.value
                if isinstance(v, ast.Constant):
                    consts[t.id] = v.value
        # Safety-critical: live arming key
        assert consts.get("RIGHT_LIVE_ENABLED") is True
        # Helper entity ID locked (auditor 6 said this must exist before go-live)
        assert consts.get("E_RIGHT_CONTROLLER_FLAG") == \
            "input_boolean.snug_right_controller_enabled"
        assert consts.get("E_RIGHT_RAIL_ENGAGED") == \
            "input_boolean.snug_right_rail_engaged"
        # Bedtime entity locked
        assert consts.get("E_BEDTIME_TEMP_RIGHT") == \
            "number.smart_topper_right_side_bedtime_temperature"
        # Rate limit and freeze duration locked
        assert consts.get("RIGHT_MIN_CHANGE_INTERVAL_SEC") == 1800
        assert consts.get("RIGHT_OVERRIDE_FREEZE_MIN") == 60
