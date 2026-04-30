"""Unit tests for ml/policy.py — the layer-1 safety rails.

Body-temp rails use ABSOLUTE thresholds (not per-zone percentile-calibrated).
The wife's body sensor frequently sits at 90°F+, but she has reported feeling
hot during those stretches — i.e. those readings represent uncalibrated
discomfort, not a personal "normal." Comfort is a physical property; the
rails must fire for any sleeper at the physical threshold.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import pytest

from ml.policy import (
    BODY_COLD_GRACE_MIN, BODY_OVERHEAT_HARD_F, BODY_OVERHEAT_SOFT_F,
    BODY_TOO_COLD_F, ROOM_HOT_HARD_F, ROOM_TOO_COLD_F, SETTING_MIN,
    apply_rails, controller_decision,
    rail_body_overheat_hard, rail_body_overheat_soft, rail_body_too_cold,
    rail_room_hot_hard, rail_room_too_cold,
)


# ── Hard rails ────────────────────────────────────────────────────────

def test_body_overheat_hard_fires_at_threshold():
    assert rail_body_overheat_hard(BODY_OVERHEAT_HARD_F) == SETTING_MIN
    assert rail_body_overheat_hard(BODY_OVERHEAT_HARD_F + 5) == SETTING_MIN


def test_body_overheat_hard_does_not_fire_below_threshold():
    assert rail_body_overheat_hard(BODY_OVERHEAT_HARD_F - 0.1) is None
    assert rail_body_overheat_hard(80.0) is None


def test_body_overheat_hard_handles_missing_value():
    assert rail_body_overheat_hard(None) is None
    assert rail_body_overheat_hard(float("nan")) is None


def test_room_hot_hard_fires_at_threshold():
    assert rail_room_hot_hard(ROOM_HOT_HARD_F) == SETTING_MIN
    assert rail_room_hot_hard(80.0) == SETTING_MIN


def test_room_hot_hard_does_not_fire_below_threshold():
    assert rail_room_hot_hard(76.9) is None


# ── Soft rails ───────────────────────────────────────────────────────

def test_body_overheat_soft_pulls_to_minus_7():
    assert rail_body_overheat_soft(BODY_OVERHEAT_SOFT_F, -3) == -7


def test_body_overheat_soft_respects_already_cool_smart():
    assert rail_body_overheat_soft(BODY_OVERHEAT_SOFT_F, -9) == -9


def test_body_overheat_soft_does_not_fire_below_threshold():
    assert rail_body_overheat_soft(BODY_OVERHEAT_SOFT_F - 0.1, -3) is None


def test_body_too_cold_caps_cooling_at_minus_3():
    assert rail_body_too_cold(BODY_TOO_COLD_F - 2,
                               BODY_COLD_GRACE_MIN + 30, -8) == -3


def test_body_too_cold_does_not_fire_during_entry_grace():
    assert rail_body_too_cold(BODY_TOO_COLD_F - 2, 10.0, -8) is None


def test_body_too_cold_does_not_fire_above_threshold():
    assert rail_body_too_cold(BODY_TOO_COLD_F + 0.5, 100.0, -8) is None


def test_room_too_cold_caps_cooling_at_minus_3():
    assert rail_room_too_cold(58.0, -10) == -3


def test_room_too_cold_does_not_fire_above_threshold():
    assert rail_room_too_cold(ROOM_TOO_COLD_F + 0.1, -10) is None


# ── Wife-at-90F regression (absolute threshold, both zones) ──────────

def test_wife_at_90F_DOES_fire_hard_rail():
    """Wife reported feeling hot at 90°F — rails must fire on either zone."""
    assert rail_body_overheat_hard(90.0) == SETTING_MIN


def test_anyone_at_88F_fires_soft_rail():
    """88°F is uncomfortable for any sleeper; soft rail should pull to ≤-7."""
    assert rail_body_overheat_soft(88.0, -3) == -7


# ── Composition ──────────────────────────────────────────────────────

def test_apply_rails_uses_smart_when_no_rail_fires():
    setting, rail = apply_rails(smart=-5, room_temp_f=70.0,
                                 body_f=78.0, elapsed_min=180.0)
    assert (setting, rail) == (-5, None)


def test_apply_rails_hard_body_overheat_wins():
    setting, rail = apply_rails(smart=-3, room_temp_f=70.0,
                                 body_f=BODY_OVERHEAT_HARD_F + 1,
                                 elapsed_min=180.0)
    assert setting == SETTING_MIN
    assert rail == "body_overheat_hard"


def test_apply_rails_hard_room_hot_wins():
    setting, rail = apply_rails(smart=-3, room_temp_f=78.0,
                                 body_f=80.0, elapsed_min=180.0)
    assert setting == SETTING_MIN
    assert rail == "room_hot_hard"


def test_apply_rails_soft_overheat_modifies_smart():
    setting, rail = apply_rails(smart=-3, room_temp_f=70.0,
                                 body_f=BODY_OVERHEAT_SOFT_F, elapsed_min=180.0)
    assert setting == -7
    assert rail == "body_overheat_soft"


def test_apply_rails_clamps_to_safety_range():
    setting, _ = apply_rails(smart=15, room_temp_f=70.0,
                              body_f=78.0, elapsed_min=180.0)
    assert setting == 0


# ── End-to-end via controller_decision ──────────────────────────────

def test_controller_decision_normal_path():
    setting, rail = controller_decision(zone="left", elapsed_min=30.0,
                                         room_temp_f=70.0, body_f=78.0)
    assert -10 <= setting <= 0
    assert rail is None


def test_controller_decision_room_77_forces_max_cool_either_zone():
    for zone in ("left", "right"):
        for elapsed in (30.0, 270.0, 450.0):
            setting, rail = controller_decision(zone=zone, elapsed_min=elapsed,
                                                 room_temp_f=77.0, body_f=80.0)
            assert setting == SETTING_MIN
            assert rail == "room_hot_hard"


def test_controller_decision_body_overheat_overrides_cool_room():
    setting, rail = controller_decision(zone="left", elapsed_min=180.0,
                                         room_temp_f=68.0,
                                         body_f=BODY_OVERHEAT_HARD_F + 1)
    assert setting == SETTING_MIN
    assert rail == "body_overheat_hard"


def test_controller_decision_wife_at_90F_fires_rail():
    """Critical regression: previous percentile design let 90°F slip through
    on her side. Absolute threshold must catch it on either zone."""
    setting, rail = controller_decision(zone="right", elapsed_min=180.0,
                                         room_temp_f=70.0, body_f=90.0)
    assert setting == SETTING_MIN
    assert rail == "body_overheat_hard"


