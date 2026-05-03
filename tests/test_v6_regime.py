"""Tests for ml.v6.regime — deterministic regime classifier."""

import pytest
from ml.v6.regime import classify, divergence_check, RegimeConfig, DEFAULT_CONFIG


# ─── Shared fixtures ──────────────────────────────────────────────────

def _base_kwargs(zone="left"):
    """Baseline kwargs that produce NORMAL_COOL."""
    return dict(
        zone=zone,
        elapsed_min=120,
        mins_since_onset=120,
        post_bedjet_min=90,
        sleep_stage="deep",
        bed_occupied=True,
        room_f=72.0,
        body_skin_f=80.0,
        body_hot_f=80.0,
        body_avg_f=79.0,
        override_freeze_active=False,
        right_rail_engaged=False,
        pre_sleep_active=False,
        three_level_off=True,
    )


# ─── UNOCCUPIED ──────────────────────────────────────────────────────

class TestUnoccupied:
    def test_bed_not_occupied(self):
        kw = _base_kwargs()
        kw["bed_occupied"] = False
        result = classify(**kw)
        assert result["regime"] == "UNOCCUPIED"
        assert result["base_setting"] == 0

    def test_bed_occupied_none_fail_closed(self):
        """bed_occupied=None → UNOCCUPIED (fail-closed per v5.2 patch)."""
        kw = _base_kwargs()
        kw["bed_occupied"] = None
        result = classify(**kw)
        assert result["regime"] == "UNOCCUPIED"


# ─── PRE_BED ─────────────────────────────────────────────────────────

class TestPreBed:
    def test_pre_sleep_active(self):
        kw = _base_kwargs()
        kw["pre_sleep_active"] = True
        result = classify(**kw)
        assert result["regime"] == "PRE_BED"
        assert result["base_setting"] == -10

    def test_pre_bed_right_zone(self):
        kw = _base_kwargs("right")
        kw["pre_sleep_active"] = True
        result = classify(**kw)
        assert result["regime"] == "PRE_BED"


# ─── INITIAL_COOL ────────────────────────────────────────────────────

class TestInitialCool:
    def test_within_30min_window(self):
        kw = _base_kwargs()
        kw["mins_since_onset"] = 15
        result = classify(**kw)
        assert result["regime"] == "INITIAL_COOL"
        assert result["base_setting"] == -10

    def test_at_boundary_30min(self):
        kw = _base_kwargs()
        kw["mins_since_onset"] = 30
        result = classify(**kw)
        assert result["regime"] == "INITIAL_COOL"

    def test_past_30min_window(self):
        kw = _base_kwargs()
        kw["mins_since_onset"] = 31
        result = classify(**kw)
        assert result["regime"] != "INITIAL_COOL"

    def test_cold_room_shrinks_window(self):
        """When room < 66°F, window shrinks to 15 min."""
        kw = _base_kwargs()
        kw["room_f"] = 64.0
        kw["mins_since_onset"] = 20
        result = classify(**kw)
        # 20 > 15 so NOT initial_cool
        assert result["regime"] != "INITIAL_COOL"

    def test_cold_room_within_shrunk_window(self):
        kw = _base_kwargs()
        kw["room_f"] = 64.0
        kw["mins_since_onset"] = 14
        result = classify(**kw)
        assert result["regime"] == "INITIAL_COOL"

    def test_transition_pre_bed_to_initial_cool(self):
        """PRE_BED takes priority over INITIAL_COOL."""
        kw = _base_kwargs()
        kw["pre_sleep_active"] = True
        kw["mins_since_onset"] = 5
        result = classify(**kw)
        assert result["regime"] == "PRE_BED"


# ─── BEDJET_WARM ─────────────────────────────────────────────────────

class TestBedjetWarm:
    def test_right_zone_bedjet_active(self):
        kw = _base_kwargs("right")
        kw["post_bedjet_min"] = 15
        kw["mins_since_onset"] = 60  # past initial cool
        result = classify(**kw)
        assert result["regime"] == "BEDJET_WARM"
        assert result["base_setting"] == -5

    def test_left_zone_no_bedjet(self):
        """BEDJET_WARM is right-zone only."""
        kw = _base_kwargs("left")
        kw["post_bedjet_min"] = 15
        kw["mins_since_onset"] = 60
        result = classify(**kw)
        assert result["regime"] != "BEDJET_WARM"

    def test_bedjet_past_window(self):
        kw = _base_kwargs("right")
        kw["post_bedjet_min"] = 35
        kw["mins_since_onset"] = 60
        result = classify(**kw)
        assert result["regime"] != "BEDJET_WARM"


# ─── SAFETY_YIELD ────────────────────────────────────────────────────

class TestSafetyYield:
    def test_right_rail_engaged(self):
        kw = _base_kwargs("right")
        kw["right_rail_engaged"] = True
        kw["mins_since_onset"] = 60
        result = classify(**kw)
        assert result["regime"] == "SAFETY_YIELD"
        assert result["base_setting"] == -10

    def test_left_zone_rail_not_applicable(self):
        """SAFETY_YIELD is right-zone only."""
        kw = _base_kwargs("left")
        kw["right_rail_engaged"] = True
        kw["mins_since_onset"] = 60
        result = classify(**kw)
        assert result["regime"] != "SAFETY_YIELD"


# ─── OVERRIDE ────────────────────────────────────────────────────────

class TestOverride:
    def test_override_freeze_active(self):
        kw = _base_kwargs()
        kw["override_freeze_active"] = True
        kw["mins_since_onset"] = 60
        result = classify(**kw)
        assert result["regime"] == "OVERRIDE"
        assert result["base_setting"] is None

    def test_override_right_zone(self):
        kw = _base_kwargs("right")
        kw["override_freeze_active"] = True
        kw["mins_since_onset"] = 60
        result = classify(**kw)
        assert result["regime"] == "OVERRIDE"


# ─── COLD_ROOM_COMP ──────────────────────────────────────────────────

class TestColdRoomComp:
    def test_cold_room_with_body_delta(self):
        kw = _base_kwargs("left")
        kw["room_f"] = 67.0
        kw["body_skin_f"] = 76.0  # delta = 9 >= 5
        kw["mins_since_onset"] = 60
        result = classify(**kw)
        assert result["regime"] == "COLD_ROOM_COMP"

    def test_cold_room_insufficient_delta(self):
        kw = _base_kwargs("left")
        kw["room_f"] = 68.0
        kw["body_skin_f"] = 71.0  # delta = 3 < 5
        kw["mins_since_onset"] = 60
        result = classify(**kw)
        assert result["regime"] != "COLD_ROOM_COMP"

    def test_cold_room_right_zone(self):
        kw = _base_kwargs("right")
        kw["room_f"] = 67.0
        kw["body_skin_f"] = 76.0
        kw["mins_since_onset"] = 60
        result = classify(**kw)
        assert result["regime"] == "COLD_ROOM_COMP"

    def test_cold_room_left_base_setting_capped(self):
        kw = _base_kwargs("left")
        kw["room_f"] = 67.0
        kw["body_skin_f"] = 76.0
        kw["mins_since_onset"] = 60
        result = classify(**kw)
        # Left cap is -3 per config
        assert result["base_setting"] >= -10
        assert result["base_setting"] <= DEFAULT_CONFIG.cold_room_comp_cap_left


# ─── WAKE_COOL ────────────────────────────────────────────────────────

class TestWakeCool:
    def test_awake_after_240min(self):
        kw = _base_kwargs("left")
        kw["sleep_stage"] = "awake"
        kw["elapsed_min"] = 300
        kw["mins_since_onset"] = 300
        result = classify(**kw)
        assert result["regime"] == "WAKE_COOL"

    def test_wake_stage_string(self):
        kw = _base_kwargs("left")
        kw["sleep_stage"] = "wake"
        kw["elapsed_min"] = 300
        kw["mins_since_onset"] = 300
        result = classify(**kw)
        assert result["regime"] == "WAKE_COOL"

    def test_awake_too_early_not_wake_cool(self):
        """Don't trigger from brief midnight wake."""
        kw = _base_kwargs("left")
        kw["sleep_stage"] = "awake"
        kw["elapsed_min"] = 60  # too early
        kw["mins_since_onset"] = 60
        result = classify(**kw)
        assert result["regime"] != "WAKE_COOL"

    def test_wake_cool_left_base(self):
        kw = _base_kwargs("left")
        kw["sleep_stage"] = "awake"
        kw["elapsed_min"] = 300
        kw["mins_since_onset"] = 300
        result = classify(**kw)
        assert result["base_setting"] == -2

    def test_wake_cool_right_body_hot(self):
        """Right zone: cool-bias when body_hot > 84°F."""
        kw = _base_kwargs("right")
        kw["sleep_stage"] = "awake"
        kw["elapsed_min"] = 300
        kw["mins_since_onset"] = 300
        kw["body_hot_f"] = 85.0
        result = classify(**kw)
        assert result["regime"] == "WAKE_COOL"
        assert result["base_setting"] == -2

    def test_wake_cool_right_body_not_hot(self):
        """Right zone: no cool-bias when body_hot <= 84°F."""
        kw = _base_kwargs("right")
        kw["sleep_stage"] = "awake"
        kw["elapsed_min"] = 300
        kw["mins_since_onset"] = 300
        kw["body_hot_f"] = 82.0
        result = classify(**kw)
        assert result["regime"] == "WAKE_COOL"
        assert result["base_setting"] == 0


# ─── NORMAL_COOL ──────────────────────────────────────────────────────

class TestNormalCool:
    def test_default_regime(self):
        kw = _base_kwargs()
        result = classify(**kw)
        assert result["regime"] == "NORMAL_COOL"

    def test_left_cycle_baseline(self):
        """Cycle index 0 (first 90 min) → left baseline -10."""
        kw = _base_kwargs("left")
        kw["elapsed_min"] = 30
        kw["mins_since_onset"] = 60  # past initial cool
        result = classify(**kw)
        assert result["regime"] == "NORMAL_COOL"
        assert result["base_setting"] == DEFAULT_CONFIG.cycle_baseline_left[0]

    def test_right_cycle_baseline(self):
        kw = _base_kwargs("right")
        kw["elapsed_min"] = 120  # cycle index 1
        kw["mins_since_onset"] = 120
        result = classify(**kw)
        assert result["regime"] == "NORMAL_COOL"
        assert result["base_setting"] == DEFAULT_CONFIG.cycle_baseline_right[1]


# ─── DEFAULT_CONFIG matches §8 ────────────────────────────────────────

class TestDefaultConfig:
    def test_initial_bed_cooling_min(self):
        assert DEFAULT_CONFIG.initial_bed_cooling_min == 30

    def test_body_fb_kp_cold_left(self):
        assert DEFAULT_CONFIG.body_fb_kp_cold_left == 1.25

    def test_body_fb_kp_hot_right(self):
        assert DEFAULT_CONFIG.body_fb_kp_hot_right == 0.50

    def test_right_proactive_hot_f(self):
        assert DEFAULT_CONFIG.right_proactive_hot_f == 84.0

    def test_room_blower_reference_f(self):
        assert DEFAULT_CONFIG.room_blower_reference_f == 72.0

    def test_cold_room_comp_cap_left(self):
        assert DEFAULT_CONFIG.cold_room_comp_cap_left == -3

    def test_residual_lcb_k(self):
        assert DEFAULT_CONFIG.residual_lcb_k == 1.0

    def test_max_divergence_steps_normal_cool(self):
        assert DEFAULT_CONFIG.max_divergence_steps["NORMAL_COOL"] == 3


# ─── divergence_check ─────────────────────────────────────────────────

class TestDivergenceCheck:
    def test_no_divergence(self):
        assert divergence_check(-5, 80.0, 80.0) == 0.0

    def test_divergence_detected(self):
        assert divergence_check(-5, 80.0, 83.0) == 3.0

    def test_negative_divergence(self):
        assert divergence_check(-5, 83.0, 80.0) == 3.0
