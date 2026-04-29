"""Behavioral tests for right_overheat_safety state machine.

Tests the pure transition logic without importing AppDaemon. The replicated
function below MUST stay in lockstep with right_overheat_safety._tick_inner.
"""
from __future__ import annotations

OVERHEAT_HARD_F = 88.0
OVERHEAT_HARD_STREAK = 2
OVERHEAT_RELEASE_F = 84.0


def _step(state, *, body, occupied=True, rail_enabled=True):
    """Pure replica of RightOverheatSafety._tick_inner state transitions.

    Returns the action the AppDaemon app would take this tick:
      None        — no setpoint change
      ("force",)  — engage and force -10
      ("restore", val) — release and restore prior setpoint
    """
    action = None
    if not rail_enabled:
        if state.get("engaged"):
            action = ("restore", state.get("snapshot_setting"))
            state["engaged"] = False
            state["streak"] = 0
            state["snapshot_setting"] = None
        return action

    if not occupied:
        if state.get("engaged"):
            action = ("restore", state.get("snapshot_setting"))
            state["engaged"] = False
            state["streak"] = 0
            state["snapshot_setting"] = None
        return action

    if body is None:
        return action  # no streak update on missing read

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
        assert _step(s, body=t) is None
    assert not s.get("engaged")


def test_engages_after_two_consecutive_overheats():
    s = {}
    assert _step(s, body=88.5) is None  # streak=1
    assert _step(s, body=89.0) == ("force",)
    assert s["engaged"]


def test_single_spike_does_not_engage():
    s = {}
    _step(s, body=90.0)        # streak=1
    _step(s, body=83.0)        # cleared
    assert _step(s, body=89.0) is None  # back to streak=1, not engaged
    assert not s.get("engaged")


def test_hysteresis_holds_engagement_at_86():
    s = {}
    _step(s, body=89.0); _step(s, body=89.0)  # engage
    assert s["engaged"]
    assert _step(s, body=86.0) is None  # 84 ≤ 86 < 88 → stays engaged
    assert s["engaged"]


def test_releases_below_84():
    s = {"snapshot_setting": -4}
    _step(s, body=89.0); _step(s, body=89.0)  # engage (streak path)
    s["snapshot_setting"] = -4               # would be set inside _engage
    a = _step(s, body=83.5)
    assert a == ("restore", -4)
    assert not s["engaged"]


def test_disabled_rail_releases_immediately():
    s = {}
    _step(s, body=89.0); _step(s, body=89.0)
    s["snapshot_setting"] = -2
    a = _step(s, body=99.0, rail_enabled=False)
    assert a == ("restore", -2)
    assert not s["engaged"]


def test_unoccupied_releases_immediately():
    s = {}
    _step(s, body=89.0); _step(s, body=89.0)
    s["snapshot_setting"] = -3
    a = _step(s, body=99.0, occupied=False)
    assert a == ("restore", -3)


def test_missing_body_does_not_change_state():
    s = {"streak": 1}
    assert _step(s, body=None) is None
    assert s["streak"] == 1


def test_prolonged_overheat_stays_engaged_for_full_stretch():
    """Replays the wife's 80-min overheat on 2026-04-24."""
    s = {}
    body_temps = [87, 88, 89, 90, 91, 92, 94, 96, 94, 92, 90, 88, 86, 85, 84, 83]
    actions = [_step(s, body=t) for t in body_temps]
    # Engages on second 88 (index 2).
    assert actions[0] is None and actions[1] is None
    assert actions[2] == ("force",)
    # Stays engaged through all subsequent 84+ readings (no further forces).
    for a in actions[3:-1]:
        assert a is None, f"unexpected mid-stretch action: {a}"
    # Final reading 83 < release threshold → restore.
    assert actions[-1][0] == "restore"


def test_restore_when_no_snapshot_passes_through():
    s = {"engaged": True, "snapshot_setting": None, "streak": 2}
    a = _step(s, body=80)
    assert a == ("restore", None)
    assert not s["engaged"]
