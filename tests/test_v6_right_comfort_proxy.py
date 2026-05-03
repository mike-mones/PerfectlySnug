"""Tests for ml.v6.right_comfort_proxy — composite comfort metric."""

import pytest
from ml.v6.right_comfort_proxy import score, minutes_score_ge_0_5, time_too_hot_min


# ─── Basic scoring ────────────────────────────────────────────────────

class TestScore:
    def test_hot_body_still_movement_high_score(self):
        """Body 88°F + still movement = high score (hot excursion dominates)."""
        s = score(
            body_left_f=88.0,
            body_avg_f=87.0,
            room_f=72.0,
            movement_density_15m=0.0,
            post_bedjet_min=120,
        )
        # body_hot_excess = (88-84)/4 = 1.0, weight 0.30 → 0.30 alone
        # plus stage_bad (None default) = 0.10
        assert s >= 0.3

    def test_comfortable_body_low_movement_low_score(self):
        """Body 80°F + low movement = low score."""
        s = score(
            body_left_f=80.0,
            body_avg_f=80.0,
            room_f=72.0,
            movement_density_15m=0.01,
            sleep_stage="deep",
            post_bedjet_min=120,
        )
        assert s < 0.3

    def test_recent_override_lifts_score(self):
        """Recent override (5 min ago) lifts score."""
        s_no_override = score(
            body_left_f=82.0,
            body_avg_f=82.0,
            room_f=72.0,
            movement_density_15m=0.03,
            sleep_stage="rem",
            post_bedjet_min=120,
        )
        s_with_override = score(
            body_left_f=82.0,
            body_avg_f=82.0,
            room_f=72.0,
            movement_density_15m=0.03,
            override_recent=True,
            time_since_override_min=5.0,
            sleep_stage="rem",
            post_bedjet_min=120,
        )
        assert s_with_override > s_no_override

    def test_high_movement_density_raises_score(self):
        """High movement density contributes to discomfort."""
        s = score(
            body_left_f=80.0,
            body_avg_f=80.0,
            room_f=72.0,
            movement_density_15m=0.20,
            sleep_stage="deep",
            zone_baseline_movement_p75=0.05,
            post_bedjet_min=120,
        )
        # movement_excess = 0.20 / (2*0.05) = 2.0 → clipped to 1.0
        # weight 0.30 → adds 0.30
        assert s >= 0.3

    def test_rail_engaged_contributes(self):
        """Rail engagement adds to discomfort score."""
        s = score(
            body_left_f=80.0,
            body_avg_f=80.0,
            room_f=72.0,
            movement_density_15m=0.0,
            rail_engaged=True,
            sleep_stage="deep",
            post_bedjet_min=120,
        )
        assert s >= 0.1  # at least the rail contribution

    def test_cold_body_excursion(self):
        """Very cold body gives body_cold_excess."""
        s = score(
            body_left_f=67.0,
            body_avg_f=67.0,
            room_f=65.0,
            movement_density_15m=0.0,
            sleep_stage="deep",
            post_bedjet_min=120,
        )
        # body_cold_excess = (73-67)/5 = 1.2 → clipped to 1.0, weight 0.30
        assert s >= 0.3

    def test_bedjet_suppresses_hot_signal(self):
        """Within BedJet window, body_hot_excess is suppressed."""
        s_bedjet = score(
            body_left_f=88.0,
            body_avg_f=87.0,
            room_f=72.0,
            movement_density_15m=0.0,
            post_bedjet_min=15,  # within 30-min BedJet window
            sleep_stage="deep",
        )
        s_no_bedjet = score(
            body_left_f=88.0,
            body_avg_f=87.0,
            room_f=72.0,
            movement_density_15m=0.0,
            post_bedjet_min=120,
            sleep_stage="deep",
        )
        assert s_bedjet < s_no_bedjet


# ─── Edge cases ───────────────────────────────────────────────────────

class TestEdgeCases:
    def test_none_body_left_returns_zero(self):
        """None body_left_f → 0.0 score."""
        s = score(
            body_left_f=None,
            body_avg_f=None,
            room_f=72.0,
            movement_density_15m=0.0,
        )
        assert s == 0.0

    def test_none_movement_density(self):
        """None movement_density → 0 contribution."""
        s = score(
            body_left_f=80.0,
            body_avg_f=80.0,
            room_f=72.0,
            movement_density_15m=None,
            sleep_stage="deep",
            post_bedjet_min=120,
        )
        assert 0.0 <= s <= 1.0

    def test_none_room_f(self):
        """None room_f doesn't crash."""
        s = score(
            body_left_f=80.0,
            body_avg_f=80.0,
            room_f=None,
            movement_density_15m=0.0,
            sleep_stage="deep",
            post_bedjet_min=120,
        )
        assert 0.0 <= s <= 1.0

    def test_score_always_bounded(self):
        """Score always in [0, 1]."""
        # Extreme hot + movement + rail + bad stage
        s = score(
            body_left_f=95.0,
            body_avg_f=90.0,
            room_f=72.0,
            movement_density_15m=1.0,
            rail_engaged=True,
            sleep_stage="awake",
            override_recent=True,
            time_since_override_min=1.0,
            post_bedjet_min=120,
        )
        assert 0.0 <= s <= 1.0


# ─── minutes_score_ge_0_5 ────────────────────────────────────────────

class TestMinutesScore:
    def test_all_comfortable(self):
        """All comfortable ticks → 0 minutes."""
        rows = [
            dict(body_left_f=79.0, body_avg_f=79.0, room_f=72.0,
                 movement_density_15m=0.01, sleep_stage="deep", post_bedjet_min=120)
            for _ in range(12)  # 1 hour of 5-min ticks
        ]
        assert minutes_score_ge_0_5(rows) == 0

    def test_all_uncomfortable(self):
        """All hot ticks → 60 minutes (12 ticks × 5 min)."""
        rows = [
            dict(body_left_f=90.0, body_avg_f=89.0, room_f=72.0,
                 movement_density_15m=0.2, sleep_stage="awake", post_bedjet_min=120,
                 zone_baseline_movement_p75=0.05)
            for _ in range(12)
        ]
        result = minutes_score_ge_0_5(rows)
        assert result == 60

    def test_mixed_night_fixture(self):
        """Fixture: 4 hot ticks + 8 comfortable → 20 minutes."""
        hot_rows = [
            dict(body_left_f=89.0, body_avg_f=88.0, room_f=72.0,
                 movement_density_15m=0.15, sleep_stage="awake", post_bedjet_min=120,
                 zone_baseline_movement_p75=0.05)
            for _ in range(4)
        ]
        good_rows = [
            dict(body_left_f=79.0, body_avg_f=79.0, room_f=72.0,
                 movement_density_15m=0.01, sleep_stage="deep", post_bedjet_min=120)
            for _ in range(8)
        ]
        result = minutes_score_ge_0_5(hot_rows + good_rows)
        assert result == 20


# ─── time_too_hot_min ─────────────────────────────────────────────────

class TestTimeTooHot:
    def test_no_hot(self):
        rows = [{"body_left_f": 80.0} for _ in range(12)]
        assert time_too_hot_min(rows) == 0

    def test_all_hot(self):
        rows = [{"body_left_f": 86.0} for _ in range(12)]
        assert time_too_hot_min(rows) == 60

    def test_custom_threshold(self):
        rows = [{"body_left_f": 82.0} for _ in range(12)]
        assert time_too_hot_min(rows, threshold_f=80.0) == 60
        assert time_too_hot_min(rows, threshold_f=84.0) == 0

    def test_none_body_left(self):
        rows = [{"body_left_f": None} for _ in range(12)]
        assert time_too_hot_min(rows) == 0
