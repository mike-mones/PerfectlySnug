"""Behavioral tests for right_overheat_safety state machine.

Tests the pure transition logic without importing AppDaemon. The replicated
function below MUST stay in lockstep with right_overheat_safety._tick_inner.
"""
from __future__ import annotations

OVERHEAT_HARD_F = 88.0
OVERHEAT_HARD_STREAK = 2
OVERHEAT_RELEASE_F = 84.0
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
    for t in [80, 82, 85, 86, 87.9]:
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


def test_releases_below_84():
    s = {"snapshot_setting": -4}
    _step(s, body=89.0, minutes_since_onset=60)
    _step(s, body=89.0, minutes_since_onset=61)
    s["snapshot_setting"] = -4
    a = _step(s, body=83.5, minutes_since_onset=62)
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
    """Replays the wife's 80-min overheat on 2026-04-24, AFTER BedJet window."""
    s = {"last_occupied": True}
    body_temps = [87, 88, 89, 90, 91, 92, 94, 96, 94, 92, 90, 88, 86, 85, 84, 83]
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

