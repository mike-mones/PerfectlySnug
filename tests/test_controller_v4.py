"""Tests for sleep_controller_v4.py."""

import ast
import importlib.util
import sys
import types
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock


CONTROLLER_PATH = Path(__file__).parent.parent / "appdaemon" / "sleep_controller_v4.py"


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

_spec = importlib.util.spec_from_file_location("sleep_controller_v4", CONTROLLER_PATH)
ctrl_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ctrl_module)
SleepControllerV4 = ctrl_module.SleepControllerV4


def _make_controller():
    controller = SleepControllerV4.__new__(SleepControllerV4)
    controller._state = {
        "sleep_start": datetime.now().isoformat(),
        "sleep_start_epoch": datetime.now().timestamp() - 3600,
        "last_setting": -6,
        "last_change_ts": None,
        "last_restart_ts": None,
        "override_floor": None,
        "override_floor_ts": None,
        "manual_mode": False,
        "recent_changes": [],
        "override_count": 0,
        "body_below_since": None,
    }
    controller._learned = {}
    controller._pg_conn = None
    controller.call_service = MagicMock()
    controller.log = MagicMock()
    controller._save_state = MagicMock()
    controller._log_to_postgres = MagicMock()
    controller._log_passive_zone_snapshot = MagicMock()
    controller._read_zone_snapshot = MagicMock(
        return_value={
            "body_center": 84.0,
            "body_left": 83.5,
            "body_right": 84.5,
            "body_avg": 84.0,
            "ambient": 76.0,
            "setpoint": 72.0,
            "setting": -6,
        }
    )
    controller._read_float = MagicMock(
        side_effect=lambda entity_id: {
            ctrl_module.E_ROOM_TEMP: 74.0,
            ctrl_module.E_BEDTIME_TEMP: -6.0,
        }.get(entity_id)
    )
    controller._read_str = MagicMock(return_value="unknown")
    controller._check_occupancy = MagicMock(return_value=True)
    controller._elapsed_min = MagicMock(return_value=60.0)
    controller._compute_setting = MagicMock(return_value=(-6, 0, "time_cycle"))
    return controller


def _set_states(controller, *, running="off", sleep_mode="on", responsive="on"):
    def _get_state(entity_id, **kwargs):
        return {
            ctrl_module.E_RUNNING: running,
            ctrl_module.E_SLEEP_MODE: sleep_mode,
            ctrl_module.E_RESPONSIVE_COOLING: responsive,
        }.get(entity_id)

    controller.get_state = MagicMock(side_effect=_get_state)


class TestSourceCompiles:
    def test_no_syntax_errors(self):
        ast.parse(CONTROLLER_PATH.read_text())


class TestAutoRestart:
    def test_restarts_when_running_off_and_still_occupied(self):
        controller = _make_controller()
        _set_states(controller, running="off")

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
        _set_states(controller, running="off")
        controller._state["last_restart_ts"] = (
            datetime.now() - timedelta(seconds=ctrl_module.AUTO_RESTART_DEBOUNCE_SEC - 30)
        ).isoformat()

        controller._control_loop({})

        controller.call_service.assert_not_called()
        controller._save_state.assert_not_called()
        controller._log_to_postgres.assert_called_once()

    def test_does_not_restart_when_bed_is_empty(self):
        controller = _make_controller()
        _set_states(controller, running="off")
        controller._check_occupancy.return_value = False

        controller._control_loop({})

        controller.call_service.assert_not_called()
        controller._log_to_postgres.assert_called_once()
        assert controller._log_to_postgres.call_args.kwargs["action"] == "empty_bed"
