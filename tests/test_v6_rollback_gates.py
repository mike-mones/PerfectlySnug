"""Rollback gate tests per proposal §11.3.

Each numerical rollback criterion gets a test that creates a fake night
meeting (and not meeting) the criterion, confirming the harness flags it.

§11.3 criteria tested:
1. > 6 left overrides on any single night → flagged
2. > 30 min above 86°F on right → flagged
3. > 2 right overrides → flagged
4. Manual_hold trip → flagged
5. > 5 divergence_guard activations per night → flagged
6. Any positive write attempt (target > 0) → flagged
7. right_proxy ≥ 0.5 minutes worse than v5.2 baseline (115) for 3 consecutive → flagged
"""

from __future__ import annotations

import sys
from pathlib import Path
from dataclasses import dataclass, field

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import pytest


# ── RollbackGateChecker implementation ────────────────────────────────

@dataclass
class RollbackGate:
    """A single fired gate with details."""
    gate_id: str
    description: str
    night: str
    zone: str
    value: float
    threshold: float


@dataclass
class NightlySummary:
    """Per-night metrics for rollback gate checking."""
    night: str
    zone: str
    override_count: int = 0
    minutes_above_86f: float = 0.0
    manual_hold_trip: bool = False
    divergence_guard_activations: int = 0
    positive_write_attempts: int = 0
    right_proxy_min_ge_05: float = 0.0
    max_target_written: int = -10


class RollbackGateChecker:
    """Check §11.3 rollback criteria against nightly summaries.

    Returns list of fired RollbackGate for each night.
    """

    V52_RIGHT_PROXY_BASELINE = 115.0  # minutes_score_ge_0.5 from v5.2

    def __init__(
        self,
        left_override_threshold: int = 6,
        right_override_threshold: int = 2,
        right_hot_minutes_threshold: float = 30.0,
        divergence_threshold: int = 5,
        right_proxy_consecutive_nights: int = 3,
    ):
        self.left_override_threshold = left_override_threshold
        self.right_override_threshold = right_override_threshold
        self.right_hot_minutes_threshold = right_hot_minutes_threshold
        self.divergence_threshold = divergence_threshold
        self.right_proxy_consecutive_nights = right_proxy_consecutive_nights

    def check_night(self, summary: NightlySummary) -> list[RollbackGate]:
        """Check a single night's summary against all gates."""
        fired = []

        # Gate 1: Left override count
        if summary.zone == "left" and summary.override_count > self.left_override_threshold:
            fired.append(RollbackGate(
                gate_id="left_override_regression",
                description=f">{self.left_override_threshold} left overrides",
                night=summary.night,
                zone=summary.zone,
                value=summary.override_count,
                threshold=self.left_override_threshold,
            ))

        # Gate 2: Right body > 86°F minutes
        if summary.zone == "right" and summary.minutes_above_86f > self.right_hot_minutes_threshold:
            fired.append(RollbackGate(
                gate_id="right_hot_minutes",
                description=f">{self.right_hot_minutes_threshold} min above 86°F",
                night=summary.night,
                zone=summary.zone,
                value=summary.minutes_above_86f,
                threshold=self.right_hot_minutes_threshold,
            ))

        # Gate 3: Right override count
        if summary.zone == "right" and summary.override_count > self.right_override_threshold:
            fired.append(RollbackGate(
                gate_id="right_override_regression",
                description=f">{self.right_override_threshold} right overrides",
                night=summary.night,
                zone=summary.zone,
                value=summary.override_count,
                threshold=self.right_override_threshold,
            ))

        # Gate 4: Manual hold trip
        if summary.manual_hold_trip:
            fired.append(RollbackGate(
                gate_id="manual_hold_trip",
                description="manual_hold trip detected",
                night=summary.night,
                zone=summary.zone,
                value=1,
                threshold=0,
            ))

        # Gate 5: Divergence guard storm
        if summary.divergence_guard_activations > self.divergence_threshold:
            fired.append(RollbackGate(
                gate_id="divergence_guard_storm",
                description=f">{self.divergence_threshold} divergence_guard activations",
                night=summary.night,
                zone=summary.zone,
                value=summary.divergence_guard_activations,
                threshold=self.divergence_threshold,
            ))

        # Gate 6: Positive write attempt
        if summary.positive_write_attempts > 0 or summary.max_target_written > 0:
            fired.append(RollbackGate(
                gate_id="positive_write",
                description="positive write attempt (target > 0)",
                night=summary.night,
                zone=summary.zone,
                value=max(summary.positive_write_attempts, summary.max_target_written),
                threshold=0,
            ))

        return fired

    def check_consecutive_proxy(
        self, summaries: list[NightlySummary]
    ) -> list[RollbackGate]:
        """Gate 7: right_proxy ≥ 0.5 worse than baseline for 3 consecutive nights."""
        fired = []
        right_nights = [s for s in summaries if s.zone == "right"]
        right_nights.sort(key=lambda s: s.night)

        consecutive_bad = 0
        for s in right_nights:
            if s.right_proxy_min_ge_05 >= self.V52_RIGHT_PROXY_BASELINE:
                consecutive_bad += 1
            else:
                consecutive_bad = 0

            if consecutive_bad >= self.right_proxy_consecutive_nights:
                fired.append(RollbackGate(
                    gate_id="right_proxy_regression",
                    description=(
                        f"right_proxy ≥ baseline ({self.V52_RIGHT_PROXY_BASELINE}) "
                        f"for {self.right_proxy_consecutive_nights} consecutive nights"
                    ),
                    night=s.night,
                    zone="right",
                    value=s.right_proxy_min_ge_05,
                    threshold=self.V52_RIGHT_PROXY_BASELINE,
                ))
                break  # one fire is enough

        return fired


# ── Tests ─────────────────────────────────────────────────────────────

class TestGate1LeftOverrides:
    """Gate 1: > 6 left overrides on any single night."""

    def test_fires_at_7_overrides(self):
        checker = RollbackGateChecker()
        s = NightlySummary(night="2026-05-10", zone="left", override_count=7)
        gates = checker.check_night(s)
        assert any(g.gate_id == "left_override_regression" for g in gates)

    def test_does_not_fire_at_6(self):
        checker = RollbackGateChecker()
        s = NightlySummary(night="2026-05-10", zone="left", override_count=6)
        gates = checker.check_night(s)
        assert not any(g.gate_id == "left_override_regression" for g in gates)

    def test_does_not_fire_for_right_zone(self):
        checker = RollbackGateChecker()
        s = NightlySummary(night="2026-05-10", zone="right", override_count=10)
        gates = checker.check_night(s)
        assert not any(g.gate_id == "left_override_regression" for g in gates)


class TestGate2RightHotMinutes:
    """Gate 2: > 30 min above 86°F on right."""

    def test_fires_at_31_minutes(self):
        checker = RollbackGateChecker()
        s = NightlySummary(night="2026-05-10", zone="right", minutes_above_86f=31.0)
        gates = checker.check_night(s)
        assert any(g.gate_id == "right_hot_minutes" for g in gates)

    def test_does_not_fire_at_30(self):
        checker = RollbackGateChecker()
        s = NightlySummary(night="2026-05-10", zone="right", minutes_above_86f=30.0)
        gates = checker.check_night(s)
        assert not any(g.gate_id == "right_hot_minutes" for g in gates)

    def test_does_not_fire_for_left(self):
        checker = RollbackGateChecker()
        s = NightlySummary(night="2026-05-10", zone="left", minutes_above_86f=50.0)
        gates = checker.check_night(s)
        assert not any(g.gate_id == "right_hot_minutes" for g in gates)


class TestGate3RightOverrides:
    """Gate 3: > 2 right overrides."""

    def test_fires_at_3(self):
        checker = RollbackGateChecker()
        s = NightlySummary(night="2026-05-10", zone="right", override_count=3)
        gates = checker.check_night(s)
        assert any(g.gate_id == "right_override_regression" for g in gates)

    def test_does_not_fire_at_2(self):
        checker = RollbackGateChecker()
        s = NightlySummary(night="2026-05-10", zone="right", override_count=2)
        gates = checker.check_night(s)
        assert not any(g.gate_id == "right_override_regression" for g in gates)


class TestGate4ManualHold:
    """Gate 4: Manual_hold trip."""

    def test_fires_on_manual_hold(self):
        checker = RollbackGateChecker()
        s = NightlySummary(night="2026-05-10", zone="left", manual_hold_trip=True)
        gates = checker.check_night(s)
        assert any(g.gate_id == "manual_hold_trip" for g in gates)

    def test_does_not_fire_without(self):
        checker = RollbackGateChecker()
        s = NightlySummary(night="2026-05-10", zone="left", manual_hold_trip=False)
        gates = checker.check_night(s)
        assert not any(g.gate_id == "manual_hold_trip" for g in gates)


class TestGate5DivergenceStorm:
    """Gate 5: > 5 divergence_guard activations per night."""

    def test_fires_at_6(self):
        checker = RollbackGateChecker()
        s = NightlySummary(night="2026-05-10", zone="left",
                           divergence_guard_activations=6)
        gates = checker.check_night(s)
        assert any(g.gate_id == "divergence_guard_storm" for g in gates)

    def test_does_not_fire_at_5(self):
        checker = RollbackGateChecker()
        s = NightlySummary(night="2026-05-10", zone="left",
                           divergence_guard_activations=5)
        gates = checker.check_night(s)
        assert not any(g.gate_id == "divergence_guard_storm" for g in gates)


class TestGate6PositiveWrite:
    """Gate 6: Any positive write attempt (target > 0)."""

    def test_fires_on_positive_write(self):
        checker = RollbackGateChecker()
        s = NightlySummary(night="2026-05-10", zone="left",
                           positive_write_attempts=1)
        gates = checker.check_night(s)
        assert any(g.gate_id == "positive_write" for g in gates)

    def test_fires_on_max_target_positive(self):
        checker = RollbackGateChecker()
        s = NightlySummary(night="2026-05-10", zone="left",
                           max_target_written=1)
        gates = checker.check_night(s)
        assert any(g.gate_id == "positive_write" for g in gates)

    def test_does_not_fire_at_zero(self):
        checker = RollbackGateChecker()
        s = NightlySummary(night="2026-05-10", zone="left",
                           max_target_written=0, positive_write_attempts=0)
        gates = checker.check_night(s)
        assert not any(g.gate_id == "positive_write" for g in gates)


class TestGate7RightProxyRegression:
    """Gate 7: right_proxy >= baseline for 3 consecutive nights."""

    def test_fires_after_3_consecutive_bad_nights(self):
        checker = RollbackGateChecker()
        summaries = [
            NightlySummary(night="2026-05-10", zone="right", right_proxy_min_ge_05=120.0),
            NightlySummary(night="2026-05-11", zone="right", right_proxy_min_ge_05=118.0),
            NightlySummary(night="2026-05-12", zone="right", right_proxy_min_ge_05=125.0),
        ]
        gates = checker.check_consecutive_proxy(summaries)
        assert any(g.gate_id == "right_proxy_regression" for g in gates)

    def test_does_not_fire_with_one_good_night(self):
        checker = RollbackGateChecker()
        summaries = [
            NightlySummary(night="2026-05-10", zone="right", right_proxy_min_ge_05=120.0),
            NightlySummary(night="2026-05-11", zone="right", right_proxy_min_ge_05=80.0),
            NightlySummary(night="2026-05-12", zone="right", right_proxy_min_ge_05=120.0),
        ]
        gates = checker.check_consecutive_proxy(summaries)
        assert not any(g.gate_id == "right_proxy_regression" for g in gates)

    def test_does_not_fire_with_only_2_bad_nights(self):
        checker = RollbackGateChecker()
        summaries = [
            NightlySummary(night="2026-05-10", zone="right", right_proxy_min_ge_05=120.0),
            NightlySummary(night="2026-05-11", zone="right", right_proxy_min_ge_05=120.0),
        ]
        gates = checker.check_consecutive_proxy(summaries)
        assert not any(g.gate_id == "right_proxy_regression" for g in gates)


class TestMultipleGatesFire:
    """Verify multiple gates can fire simultaneously."""

    def test_multiple_gates_on_bad_night(self):
        checker = RollbackGateChecker()
        s = NightlySummary(
            night="2026-05-10", zone="right",
            override_count=5,
            minutes_above_86f=45.0,
            divergence_guard_activations=8,
            positive_write_attempts=1,
        )
        gates = checker.check_night(s)
        gate_ids = {g.gate_id for g in gates}
        assert "right_override_regression" in gate_ids
        assert "right_hot_minutes" in gate_ids
        assert "divergence_guard_storm" in gate_ids
        assert "positive_write" in gate_ids

    def test_clean_night_no_gates(self):
        checker = RollbackGateChecker()
        s = NightlySummary(
            night="2026-05-10", zone="left",
            override_count=2,
            minutes_above_86f=0,
            divergence_guard_activations=1,
        )
        gates = checker.check_night(s)
        assert len(gates) == 0
