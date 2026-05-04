"""Unit tests for ml/v6/state_estimator.py — rule cascade + degraded fallbacks.

Spec: docs/proposals/2026-05-04_state_estimation.md
"""
from __future__ import annotations

import pytest

from ml.v6.state_estimator import (
    BODY_VALID_DELTA_F,
    BODY_VALID_WARMUP_S,
    DEGRADED_CONFIDENCE_CAP,
    Features,
    Percentiles,
    STATE_AWAKE_IN_BED,
    STATE_DISTURBANCE,
    STATE_OCCUPIED_AWAKE,
    STATE_OCCUPIED_QUIET,
    STATE_OFF_BED,
    STATE_RESTLESS,
    STATE_SETTLING,
    STATE_STABLE_SLEEP,
    STATE_WAKE_TRANSITION,
    estimate_state,
    replay_iter,
)


# ── Helper: build a “normal stable sleep” feature snapshot ──────────────
def _stable_features(**overrides) -> Features:
    base = dict(
        movement_rms_5min=0.02,
        movement_rms_15min=0.03,           # < p25 default 0.05
        movement_variance_15min=0.01,
        movement_max_delta_60s=0.5,
        presence_binary=True,
        seconds_since_presence_change=4 * 3600,
        body_avg_f=92.0,                   # well above room+6
        body_trend_15min=0.05,
        room_temp_f=72.0,
        setting_recent_change_30min=0,
    )
    base.update(overrides)
    return Features(**base)


# ── Body sensor validity ────────────────────────────────────────────────
class TestBodyValidity:
    def test_valid_when_delta_and_warmup_met(self):
        f = _stable_features(body_avg_f=80.0, room_temp_f=72.0,
                             seconds_since_presence_change=BODY_VALID_WARMUP_S)
        assert f.body_sensor_validity is True

    def test_invalid_when_delta_too_small(self):
        f = _stable_features(body_avg_f=77.0, room_temp_f=72.0)  # delta=5 < 6
        assert f.body_sensor_validity is False

    def test_invalid_when_warmup_not_reached(self):
        f = _stable_features(seconds_since_presence_change=BODY_VALID_WARMUP_S - 1)
        assert f.body_sensor_validity is False

    def test_override_takes_precedence(self):
        f = _stable_features(body_sensor_validity_override=False)
        assert f.body_sensor_validity is False


# ── Rule 1: OFF_BED ─────────────────────────────────────────────────────
class TestOffBed:
    def test_presence_false_debounced_returns_high_conf(self):
        f = _stable_features(presence_binary=False,
                             seconds_since_presence_change=600)
        s = estimate_state(f)
        assert s.state == STATE_OFF_BED and s.confidence == 1.0

    def test_presence_false_recent_transition_lower_conf(self):
        f = _stable_features(presence_binary=False,
                             seconds_since_presence_change=30)
        s = estimate_state(f)
        assert s.state == STATE_OFF_BED and s.confidence == pytest.approx(0.7)

    def test_presence_unknown_fail_closed(self):
        f = _stable_features(presence_binary=None)
        s = estimate_state(f)
        assert s.state == STATE_OFF_BED
        assert "fail_closed" in s.trigger


# ── Rule 2: AWAKE_IN_BED ────────────────────────────────────────────────
class TestAwakeInBed:
    def test_recent_presence_change_fires(self):
        f = _stable_features(seconds_since_presence_change=300)  # < 600
        s = estimate_state(f)
        assert s.state == STATE_AWAKE_IN_BED
        assert s.trigger == "awake_recent_arrival"

    def test_high_movement_fires_even_when_seated(self):
        f = _stable_features(movement_rms_5min=0.50)  # > p75 default 0.20
        s = estimate_state(f)
        assert s.state == STATE_AWAKE_IN_BED
        assert s.trigger == "awake_movement_high"

    def test_body_invalid_lowers_confidence(self):
        f = _stable_features(seconds_since_presence_change=300, body_avg_f=75.0)
        s = estimate_state(f)
        assert s.state == STATE_AWAKE_IN_BED and s.confidence == pytest.approx(0.6)


# ── Rule 3: DISTURBANCE ─────────────────────────────────────────────────
class TestDisturbance:
    def test_max_delta_spike_during_stable_fires(self):
        f = _stable_features(movement_max_delta_60s=10.0)  # > 8.0
        s = estimate_state(f, prev_state=STATE_STABLE_SLEEP)
        assert s.state == STATE_DISTURBANCE

    def test_does_not_fire_outside_stable_or_settling(self):
        f = _stable_features(movement_max_delta_60s=10.0)
        s = estimate_state(f, prev_state=STATE_AWAKE_IN_BED)
        assert s.state != STATE_DISTURBANCE

    def test_does_not_fire_if_variance_already_high(self):
        f = _stable_features(movement_max_delta_60s=10.0,
                             movement_rms_15min=0.30)  # > p75
        s = estimate_state(f, prev_state=STATE_STABLE_SLEEP)
        assert s.state != STATE_DISTURBANCE


# ── Rule 4: RESTLESS ────────────────────────────────────────────────────
class TestRestless:
    def test_variance_spike_during_stable_fires(self):
        f = _stable_features(
            movement_variance_15min=0.40,  # > p90 default 0.30
            movement_rms_15min=0.25,        # > p75 default 0.20
        )
        s = estimate_state(f, prev_state=STATE_STABLE_SLEEP)
        assert s.state == STATE_RESTLESS
        assert s.confidence == pytest.approx(0.7)

    def test_does_not_fire_without_prev_stable(self):
        f = _stable_features(movement_variance_15min=0.40, movement_rms_15min=0.25)
        s = estimate_state(f, prev_state=STATE_AWAKE_IN_BED)
        assert s.state != STATE_RESTLESS


# ── Rule 5: WAKE_TRANSITION ────────────────────────────────────────────
class TestWakeTransition:
    def test_late_session_with_rising_body_and_variance(self):
        f = _stable_features(
            movement_variance_15min=0.20,    # > p75 0.10
            movement_rms_15min=0.10,          # < p75; not RESTLESS
            body_trend_15min=0.50,             # > 0.30 rising
            seconds_since_presence_change=6 * 3600,
        )
        s = estimate_state(f, prev_state=STATE_STABLE_SLEEP)
        assert s.state == STATE_WAKE_TRANSITION

    def test_does_not_fire_before_5h(self):
        f = _stable_features(
            movement_variance_15min=0.20,
            movement_rms_15min=0.10,
            body_trend_15min=0.50,
            seconds_since_presence_change=4 * 3600,
        )
        s = estimate_state(f, prev_state=STATE_STABLE_SLEEP)
        assert s.state != STATE_WAKE_TRANSITION

    def test_suppressed_when_body_invalid(self):
        f = _stable_features(
            movement_variance_15min=0.20,
            movement_rms_15min=0.10,
            body_trend_15min=0.50,
            seconds_since_presence_change=6 * 3600,
            body_avg_f=75.0,                   # invalid (delta < 6)
        )
        s = estimate_state(f, prev_state=STATE_STABLE_SLEEP)
        assert s.state != STATE_WAKE_TRANSITION


# ── Rule 6: STABLE_SLEEP ───────────────────────────────────────────────
class TestStableSleep:
    def test_canonical_stable(self):
        s = estimate_state(_stable_features())
        assert s.state == STATE_STABLE_SLEEP
        assert s.confidence == pytest.approx(0.9)

    def test_suppressed_when_body_invalid(self):
        f = _stable_features(body_avg_f=75.0)  # delta < 6
        s = estimate_state(f)
        # Body-invalid degrades; downstream rule chooses SETTLING with reduced conf.
        assert s.state == STATE_SETTLING
        assert s.degraded == "body_validity"

    def test_suppressed_when_body_trending(self):
        f = _stable_features(body_trend_15min=0.50)  # > flat 0.30
        s = estimate_state(f)
        assert s.state != STATE_STABLE_SLEEP


# ── Rule 7: SETTLING ────────────────────────────────────────────────────
class TestSettling:
    def test_decreasing_movement_warming_body(self):
        f = _stable_features(
            movement_rms_5min=0.08,
            movement_rms_15min=0.15,    # 5min < 15min ⇒ decreasing
            body_trend_15min=0.20,        # warming but < flat threshold
        )
        s = estimate_state(f)
        # body_trend_15min=0.20 is < 0.30 flat threshold AND movement_rms_15min
        # 0.15 > p25 0.05 so STABLE doesn't fire; SETTLING wins.
        assert s.state == STATE_SETTLING


# ── Degraded paths (§6) ─────────────────────────────────────────────────
class TestDegraded:
    def test_movement_stale_occupied_early(self):
        f = _stable_features(
            movement_rms_5min=None, movement_rms_15min=None,
            movement_variance_15min=None, movement_max_delta_60s=None,
            seconds_since_presence_change=15 * 60,  # < 90min
        )
        s = estimate_state(f)
        assert s.state == STATE_OCCUPIED_AWAKE
        assert s.degraded == "movement"
        assert s.confidence == pytest.approx(DEGRADED_CONFIDENCE_CAP)

    def test_movement_stale_occupied_late_quiet(self):
        f = _stable_features(
            movement_rms_5min=None, movement_rms_15min=None,
            movement_variance_15min=None, movement_max_delta_60s=None,
            seconds_since_presence_change=4 * 3600,
            body_trend_15min=0.0,
        )
        s = estimate_state(f)
        assert s.state == STATE_OCCUPIED_QUIET
        assert s.degraded == "movement"

    def test_movement_stale_off_bed(self):
        f = _stable_features(
            presence_binary=False,
            movement_rms_5min=None, movement_rms_15min=None,
            movement_variance_15min=None, movement_max_delta_60s=None,
        )
        s = estimate_state(f)
        assert s.state == STATE_OFF_BED
        assert s.degraded == "movement"

    def test_both_degraded_collapses_to_awake_low_conf(self):
        f = _stable_features(
            movement_rms_5min=None, movement_rms_15min=None,
            movement_variance_15min=None, movement_max_delta_60s=None,
            body_avg_f=75.0,  # invalid body
        )
        s = estimate_state(f)
        assert s.state == STATE_OCCUPIED_AWAKE
        assert s.degraded == "both"
        assert s.confidence == pytest.approx(0.3)


# ── Time-of-night invariants (§5: forbidden uses) ──────────────────────
class TestTimeOfNightConstraints:
    """Time-of-night must NEVER be the primary driver. Audit guarantees:
       - WAKE_TRANSITION cannot fire on time alone (always needs movement + body)
       - STABLE_SLEEP / SETTLING / RESTLESS / DISTURBANCE never use time
    """
    def test_late_session_alone_does_not_force_wake(self):
        # Late, but movement+body are all "stable". Should stay STABLE_SLEEP.
        f = _stable_features(seconds_since_presence_change=8 * 3600)
        s = estimate_state(f, prev_state=STATE_STABLE_SLEEP)
        assert s.state == STATE_STABLE_SLEEP

    def test_late_session_alone_with_no_body_signal_no_wake(self):
        f = _stable_features(
            seconds_since_presence_change=8 * 3600,
            body_trend_15min=None,
        )
        s = estimate_state(f, prev_state=STATE_STABLE_SLEEP)
        # No body trend ⇒ Rule 6 STABLE_SLEEP suppressed, but Rule 5 also blocked.
        # Rule 7 SETTLING fires from the default cascade.
        assert s.state in (STATE_SETTLING, STATE_STABLE_SLEEP)
        assert s.state != STATE_WAKE_TRANSITION


# ── Replay iterator preserves prev_state correctly ─────────────────────
class TestReplayIter:
    def test_disturbance_does_not_update_prev_state(self):
        # Build a 3-tick stream: STABLE → DISTURBANCE → STABLE
        stable = _stable_features()
        spike = _stable_features(movement_max_delta_60s=10.0)
        rows = [
            (1, stable),
            (2, spike),    # should fire DISTURBANCE because prev=STABLE_SLEEP
            (3, stable),   # should remain STABLE_SLEEP because prev wasn't overwritten
        ]
        results = list(replay_iter(rows))
        assert results[0][2].state == STATE_STABLE_SLEEP
        assert results[1][2].state == STATE_DISTURBANCE
        # Critical invariant: third tick still treats prev as STABLE_SLEEP.
        assert results[2][2].state == STATE_STABLE_SLEEP

    def test_state_progression_stable_to_restless(self):
        stable = _stable_features()
        restless = _stable_features(
            movement_variance_15min=0.40,
            movement_rms_15min=0.25,
        )
        rows = [(1, stable), (2, restless)]
        results = list(replay_iter(rows))
        assert results[0][2].state == STATE_STABLE_SLEEP
        assert results[1][2].state == STATE_RESTLESS
