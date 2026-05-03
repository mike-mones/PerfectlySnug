"""Behavioral tests for right_overheat_safety state machine.

Tests the pure transition logic without importing AppDaemon. The replicated
function below MUST stay in lockstep with right_overheat_safety._tick_inner.
"""
from __future__ import annotations

OVERHEAT_HARD_F = 86.0
OVERHEAT_HARD_STREAK = 2
OVERHEAT_RELEASE_F = 82.0
BEDJET_SUPPRESS_MIN = 30.0


def _step(state, *, body, occupied=True, rail_enabled=True,
          minutes_since_onset=None):
    """Pure replica of RightOverheatSafety._tick_inner state transitions.

    Args:
      minutes_since_onset: simulated time since right-bed occupancy onset.
        None == not occupied yet (pre-onset). When occupied=True and this
        is None, the harness assumes minute 0 (just got in bed).

    Returns the action the AppDaemon app would take this tick:
      None             — no setpoint change
      ("force",)       — engage and force -10
      ("restore", val) — release and restore prior setpoint
      ("suppress",)    — observed in BedJet window; no setpoint change,
                         streak forced to 0 (returned as None to mirror app)
    """
    action = None
    if not rail_enabled:
        if state.get("engaged"):
            action = ("restore", state.get("snapshot_setting"))
            state["engaged"] = False
            state["streak"] = 0
            state["snapshot_setting"] = None
        state["last_occupied"] = False
        return action

    if not occupied:
        if state.get("engaged"):
            action = ("restore", state.get("snapshot_setting"))
            state["engaged"] = False
            state["streak"] = 0
            state["snapshot_setting"] = None
        state["last_occupied"] = False
        return action

    if not state.get("last_occupied"):
        state["last_occupied"] = True
        if minutes_since_onset is None:
            minutes_since_onset = 0.0

    if body is None:
        return action

    if minutes_since_onset is not None and minutes_since_onset <= BEDJET_SUPPRESS_MIN:
        # BedJet window: hold streak at 0, never engage on a fresh session.
        state["streak"] = 0
        return action

    already_engaged = state.get("engaged", False)
    if body >= OVERHEAT_HARD_F:
        state["streak"] = state.get("streak", 0) + 1
    elif body < (OVERHEAT_RELEASE_F if already_engaged else OVERHEAT_HARD_F):
        if already_engaged:
            action = ("restore", state.get("snapshot_setting"))
            state["engaged"] = False
            state["snapshot_setting"] = None
        state["streak"] = 0

    if not state.get("engaged") and state.get("streak", 0) >= OVERHEAT_HARD_STREAK:
        action = ("force",)
        state["engaged"] = True

    return action


def test_does_not_engage_below_threshold():
    s = {}
    for t in [78, 80, 82, 84, 85.9]:
        assert _step(s, body=t, minutes_since_onset=60) is None
    assert not s.get("engaged")


def test_engages_after_two_consecutive_overheats():
    s = {}
    assert _step(s, body=88.5, minutes_since_onset=60) is None  # streak=1
    assert _step(s, body=89.0, minutes_since_onset=61) == ("force",)
    assert s["engaged"]


def test_single_spike_does_not_engage():
    s = {}
    _step(s, body=90.0, minutes_since_onset=60)        # streak=1
    _step(s, body=83.0, minutes_since_onset=61)        # cleared
    assert _step(s, body=89.0, minutes_since_onset=62) is None  # streak=1
    assert not s.get("engaged")


def test_hysteresis_holds_engagement_at_86():
    s = {}
    _step(s, body=89.0, minutes_since_onset=60)
    _step(s, body=89.0, minutes_since_onset=61)
    assert s["engaged"]
    assert _step(s, body=86.0, minutes_since_onset=62) is None
    assert s["engaged"]


def test_releases_below_82():
    s = {"snapshot_setting": -4}
    _step(s, body=89.0, minutes_since_onset=60)
    _step(s, body=89.0, minutes_since_onset=61)
    s["snapshot_setting"] = -4
    a = _step(s, body=81.5, minutes_since_onset=62)
    assert a == ("restore", -4)
    assert not s["engaged"]


def test_disabled_rail_releases_immediately():
    s = {}
    _step(s, body=89.0, minutes_since_onset=60)
    _step(s, body=89.0, minutes_since_onset=61)
    s["snapshot_setting"] = -2
    a = _step(s, body=99.0, rail_enabled=False)
    assert a == ("restore", -2)
    assert not s["engaged"]


def test_unoccupied_releases_immediately():
    s = {}
    _step(s, body=89.0, minutes_since_onset=60)
    _step(s, body=89.0, minutes_since_onset=61)
    s["snapshot_setting"] = -3
    a = _step(s, body=99.0, occupied=False)
    assert a == ("restore", -3)


def test_missing_body_does_not_change_state():
    s = {"streak": 1, "last_occupied": True}
    assert _step(s, body=None, minutes_since_onset=60) is None
    assert s["streak"] == 1


def test_prolonged_overheat_stays_engaged_for_full_stretch():
    """Replays the wife's 80-min overheat on 2026-04-24, AFTER BedJet window.
    With the post-2026-04-30 86°F engage / 82°F release thresholds.
    """
    s = {"last_occupied": True}
    body_temps = [85, 86, 87, 88, 89, 90, 92, 94, 92, 90, 88, 86, 84, 83, 82, 81]
    actions = [_step(s, body=t, minutes_since_onset=45 + i)
               for i, t in enumerate(body_temps)]
    assert actions[0] is None and actions[1] is None
    assert actions[2] == ("force",)
    for a in actions[3:-1]:
        assert a is None, f"unexpected mid-stretch action: {a}"
    assert actions[-1][0] == "restore"


def test_restore_when_no_snapshot_passes_through():
    s = {"engaged": True, "snapshot_setting": None, "streak": 2,
         "last_occupied": True}
    a = _step(s, body=80, minutes_since_onset=60)
    assert a == ("restore", None)
    assert not s["engaged"]


# ── BedJet suppression window ─────────────────────────────────────────

def test_bedjet_window_suppresses_high_readings():
    """During first 30 min after onset, even sustained 95°F must NOT engage."""
    s = {}
    for t_min in range(0, 31, 2):
        a = _step(s, body=95.0, minutes_since_onset=float(t_min))
        assert a is None, f"unexpected engage at minute {t_min}"
    assert not s.get("engaged"), "rail engaged inside BedJet window"
    assert s.get("streak", 0) == 0, "streak leaked into post-window state"


def test_bedjet_window_does_not_block_post_window_engage():
    """After the window closes, normal logic runs and a real overheat engages."""
    s = {}
    # First 30 min: BedJet at 95°F.
    for t_min in range(0, 31, 5):
        _step(s, body=95.0, minutes_since_onset=float(t_min))
    assert not s.get("engaged")
    # Now post-window real overheat.
    assert _step(s, body=89.0, minutes_since_onset=31.0) is None      # streak=1
    assert _step(s, body=89.5, minutes_since_onset=32.0) == ("force",)
    assert s["engaged"]


def test_bedjet_window_does_not_block_normal_readings():
    """During the window, normal (<88°F) readings are simply observed."""
    s = {}
    for t_min, t in [(0, 80), (5, 81), (10, 82), (15, 83), (29, 87.5)]:
        a = _step(s, body=t, minutes_since_onset=float(t_min))
        assert a is None
    assert not s.get("engaged")


def test_window_resets_on_bed_empty_then_re_entry():
    """Getting out of bed and back in starts a fresh BedJet window."""
    s = {}
    # First session: enter bed, 30 min of BedJet at 95°F, then 31 min real, force.
    _step(s, body=95.0, minutes_since_onset=10.0)
    _step(s, body=89.0, minutes_since_onset=31.0)
    a = _step(s, body=89.0, minutes_since_onset=32.0)
    assert a == ("force",)
    # Bed empty → release.
    a2 = _step(s, body=None, occupied=False)
    assert a2 and a2[0] == "restore"
    assert not s.get("engaged")
    assert not s.get("last_occupied")
    # Re-enter: harness defaults to minute 0 → BedJet window again, no engage
    # even on 95°F.
    assert _step(s, body=95.0) is None
    assert _step(s, body=95.0, minutes_since_onset=10.0) is None
    assert not s.get("engaged")


# ── Module-level invariants ──────────────────────────────────────────
#
# These tests pin down structural choices that aren't exercised by the
# pure-function _step replica above (which takes body as a parameter and
# never reads any HA entity). Without these, a future refactor could
# silently change the entity name we read, the engage threshold, or the
# release hysteresis without breaking any other test.

def test_entity_constants_locked():
    """Lock the right-zone sensor and engagement thresholds.

    The 2026-04-30 sensor swap (body_center_f → body_left_f) is data-driven
    (see _archive/right_zone_rollout_2026-04-30.md); regressing it would
    cause the rail to engage 5.8× more often on warm-sheet readings. Pin
    the entity name so a future mass rename or copy-paste from left-zone
    code can't silently revert it.
    """
    import importlib.util as _u
    from pathlib import Path as _P
    spec = _u.spec_from_file_location(
        "ros_module",
        _P(__file__).resolve().parents[1] / "appdaemon" / "right_overheat_safety.py",
    )
    # We can't fully import (depends on `hassapi`), so parse the AST.
    import ast
    src = (_P(__file__).resolve().parents[1] / "appdaemon"
           / "right_overheat_safety.py").read_text()
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
            elif (isinstance(v, ast.UnaryOp) and isinstance(v.op, ast.USub)
                  and isinstance(v.operand, ast.Constant)):
                consts[t.id] = -v.operand.value

    assert consts.get("E_BODY_LEFT_R") == \
        "sensor.smart_topper_right_side_body_sensor_left", \
        "right-rail must read body_sensor_left (skin-contact); see " \
        "_archive/right_zone_rollout_2026-04-30.md for the data."
    assert "E_BODY_CENTER_R" not in consts, (
        "stale center-sensor constant — replace with E_BODY_LEFT_R "
        "(body_center_f reads warm-sheet heat, not skin temp)."
    )
    assert consts.get("OVERHEAT_HARD_F") == OVERHEAT_HARD_F
    assert consts.get("OVERHEAT_RELEASE_F") == OVERHEAT_RELEASE_F
    assert consts.get("OVERHEAT_HARD_STREAK") == OVERHEAT_HARD_STREAK
    assert consts.get("BEDJET_SUPPRESS_MIN") == BEDJET_SUPPRESS_MIN
    assert consts.get("RAIL_FORCE_SETTING") == -10
    assert consts.get("E_RAIL_ENGAGED") == "input_boolean.snug_right_rail_engaged"


def test_release_does_not_stomp_user_change_during_engagement():
    """If someone changed the setpoint while rail was engaged, release leaves it."""
    import importlib.util
    import sys
    import types
    from pathlib import Path
    from unittest.mock import MagicMock

    fake_hass_module = types.ModuleType("hassapi")
    fake_hass_module.Hass = object
    sys.modules["hassapi"] = fake_hass_module

    module_path = Path(__file__).resolve().parents[1] / "appdaemon" / "right_overheat_safety.py"
    spec = importlib.util.spec_from_file_location("right_overheat_safety_for_restore", module_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    app = module.RightOverheatSafety.__new__(module.RightOverheatSafety)
    app._state = {
        "engaged": True,
        "streak": 2,
        "snapshot_setting": -4,
        "released_at": None,
    }
    app._read_float = MagicMock(return_value=-7.0)
    app.call_service = MagicMock()
    app.log = MagicMock()
    app._save_state = MagicMock()

    app._release("body_cooled_to_81.5")

    assert not any(call.args and call.args[0] == "number/set_value"
                   for call in app.call_service.call_args_list)
    app.call_service.assert_any_call(
        "input_boolean/turn_off", entity_id=module.E_RAIL_ENGAGED
    )
    assert app._state["engaged"] is False
    assert app._state["snapshot_setting"] is None
    assert any(
        "not restoring prev_setpoint=-4" in call.args[0]
        for call in app.log.call_args_list
    )


def _load_app_module(name="right_overheat_safety_for_helper_tests"):
    import importlib.util
    import sys
    import types
    from pathlib import Path

    fake_hass_module = types.ModuleType("hassapi")
    fake_hass_module.Hass = object
    sys.modules["hassapi"] = fake_hass_module

    module_path = Path(__file__).resolve().parents[1] / "appdaemon" / "right_overheat_safety.py"
    spec = importlib.util.spec_from_file_location(name, module_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_engage_flips_helper_on_before_force_write():
    from unittest.mock import MagicMock

    module = _load_app_module("right_overheat_safety_engage_helper")
    app = module.RightOverheatSafety.__new__(module.RightOverheatSafety)
    app._state = {"engage_count_session": 0}
    app._read_float = MagicMock(return_value=-5.0)
    app.call_service = MagicMock()
    app.log = MagicMock()

    app._engage(body=87.0)

    helper_on = (
        "input_boolean/turn_on", {"entity_id": module.E_RAIL_ENGAGED}
    )
    force_write = (
        "number/set_value",
        {"entity_id": module.E_BEDTIME_R, "value": module.RAIL_FORCE_SETTING},
    )
    calls = [(call.args[0], call.kwargs) for call in app.call_service.call_args_list]
    assert helper_on in calls
    assert force_write in calls
    assert calls.index(helper_on) < calls.index(force_write)
    assert app._state["engaged"] is True


def test_engage_helper_on_is_first_service_call_for_real_force():
    from unittest.mock import MagicMock, call

    module = _load_app_module("right_overheat_safety_engage_first_call")
    app = module.RightOverheatSafety.__new__(module.RightOverheatSafety)
    app._state = {"engage_count_session": 0}
    app._read_float = MagicMock(return_value=-5.0)
    app.call_service = MagicMock()
    app.log = MagicMock()

    app._engage(body=87.0)

    assert app.call_service.call_args_list[:2] == [
        call("input_boolean/turn_on", entity_id=module.E_RAIL_ENGAGED),
        call("number/set_value", entity_id=module.E_BEDTIME_R, value=module.RAIL_FORCE_SETTING),
    ]


def test_release_flips_helper_off_after_restore_write():
    from unittest.mock import MagicMock

    module = _load_app_module("right_overheat_safety_release_helper")
    app = module.RightOverheatSafety.__new__(module.RightOverheatSafety)
    app._state = {"engaged": True, "streak": 2, "snapshot_setting": -4}
    app._read_float = MagicMock(return_value=-10.0)
    app.call_service = MagicMock()
    app.log = MagicMock()
    app._save_state = MagicMock()

    app._release("body_cooled_to_81.5")

    app.call_service.assert_any_call("number/set_value", entity_id=module.E_BEDTIME_R, value=-4)
    app.call_service.assert_any_call("input_boolean/turn_off", entity_id=module.E_RAIL_ENGAGED)
    assert app._state["engaged"] is False


def test_initialize_forces_rail_helper_off():
    from unittest.mock import MagicMock

    module = _load_app_module("right_overheat_safety_init_helper")
    app = module.RightOverheatSafety.__new__(module.RightOverheatSafety)
    app._load_state = MagicMock()
    app.run_in = MagicMock()
    app.run_every = MagicMock()
    app.call_service = MagicMock()
    app.log = MagicMock()

    app.initialize()

    app.call_service.assert_any_call("input_boolean/turn_off", entity_id=module.E_RAIL_ENGAGED)
    app.run_in.assert_called_once_with(
        app._recover_helper_after_restart, module.HA_RESTART_RECOVERY_GRACE_SEC
    )
    app.run_every.assert_called_once()


def test_restart_recovery_turns_helper_on_when_rail_conditions_still_hold():
    from unittest.mock import MagicMock

    module = _load_app_module("right_overheat_safety_restart_recovery")
    app = module.RightOverheatSafety.__new__(module.RightOverheatSafety)
    app._state = {"engaged": False, "streak": 0, "snapshot_setting": None, "engaged_at": None}
    app._read_float = MagicMock(
        side_effect=lambda entity_id: {
            module.E_BEDTIME_R: float(module.RAIL_FORCE_SETTING),
            module.E_BODY_LEFT_R: module.OVERHEAT_HARD_F + 0.5,
        }.get(entity_id)
    )
    app._read_str = MagicMock(return_value="on")
    app.call_service = MagicMock()
    app._save_state = MagicMock()
    app.log = MagicMock()

    app._recover_helper_after_restart({})

    app.call_service.assert_called_once_with(
        "input_boolean/turn_on", entity_id=module.E_RAIL_ENGAGED
    )
    assert app._state["engaged"] is True
    assert app._state["streak"] == module.OVERHEAT_HARD_STREAK
    app._save_state.assert_called_once()
