"""Behavioral tests for the v5 overheat-hard rail.

These tests don't import sleep_controller_v5 directly (it requires hassapi).
Instead they exercise the rail's STATE-MACHINE logic using the same
constants. If the v5 logic changes, update this test in lockstep.

Logic under test (from sleep_controller_v5.py, _compute_setting):
  - At sustained body_avg ≥ OVERHEAT_HARD_F (90°F) for OVERHEAT_HARD_STREAK
    consecutive control-loop iterations, force max cool (-10).
  - Hysteresis: once engaged, only release when body drops below
    OVERHEAT_HARD_RELEASE_F (86°F).
  - Rail can be globally disabled via input_boolean.
"""
from __future__ import annotations

OVERHEAT_HARD_F = 90.0
OVERHEAT_HARD_STREAK = 2
OVERHEAT_HARD_RELEASE_F = 86.0


def _step(state, body_avg, *, rail_enabled=True):
    """Pure replica of the rail's per-loop state transition."""
    fired = False
    if rail_enabled and body_avg is not None:
        already_engaged = state.get("overheat_hard_engaged", False)
        release_threshold = OVERHEAT_HARD_RELEASE_F if already_engaged else OVERHEAT_HARD_F
        if body_avg >= OVERHEAT_HARD_F:
            state["overheat_hard_streak"] = state.get("overheat_hard_streak", 0) + 1
        elif body_avg < release_threshold:
            state["overheat_hard_streak"] = 0
            state["overheat_hard_engaged"] = False
        if state.get("overheat_hard_streak", 0) >= OVERHEAT_HARD_STREAK:
            state["overheat_hard_engaged"] = True
            fired = True
    return fired


def test_single_spike_does_not_fire():
    """One-sample 90°F spike should not force max cool."""
    s = {}
    assert _step(s, 90.5) is False
    assert _step(s, 85.0) is False
    assert s["overheat_hard_streak"] == 0


def test_two_consecutive_at_90_fires():
    s = {}
    assert _step(s, 90.0) is False
    assert _step(s, 90.0) is True


def test_hysteresis_holds_engagement_at_88():
    """Once engaged, body at 88°F (still above release 86°F) keeps rail on."""
    s = {}
    _step(s, 91.0); _step(s, 91.0)  # engage
    assert s["overheat_hard_engaged"]
    assert _step(s, 88.0) is True
    assert _step(s, 87.0) is True


def test_hysteresis_releases_below_86():
    s = {}
    _step(s, 91.0); _step(s, 91.0)
    assert _step(s, 85.5) is False
    assert s["overheat_hard_engaged"] is False
    assert s["overheat_hard_streak"] == 0


def test_disabled_rail_never_fires():
    s = {}
    for _ in range(10):
        assert _step(s, 95.0, rail_enabled=False) is False


def test_missing_body_does_not_break_state():
    s = {"overheat_hard_streak": 1}
    assert _step(s, None) is False
    assert s["overheat_hard_streak"] == 1  # unchanged


def test_intermittent_below_release_resets_streak():
    """Streak interrupted by a clearly-not-hot reading clears."""
    s = {}
    _step(s, 90.5)
    assert s["overheat_hard_streak"] == 1
    _step(s, 80.0)  # well below release
    assert s["overheat_hard_streak"] == 0
    assert _step(s, 90.5) is False  # back to streak=1, not yet firing


def test_prolonged_overheat_stays_engaged():
    """Wife-style 80-min stretch: rail stays at -10 throughout."""
    s = {}
    body_temps = [89, 90, 91, 92, 93, 94, 93, 92, 91, 90, 89, 88, 87]
    fires = [_step(s, t) for t in body_temps]
    # Streak engages at index 2 (second consecutive ≥90); stays engaged
    # through all subsequent values that are above release.
    assert fires[0] is False  # 89 < 90
    assert fires[1] is False  # first 90, streak=1
    assert all(fires[2:]), f"rail should stay engaged: {fires}"
