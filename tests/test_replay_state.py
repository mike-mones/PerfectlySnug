"""Smoke tests for tools/replay_state.score_summaries() — scoring logic only.

Heavy PG integration is exercised by manual runs (CLI). These tests cover the
scoring math + the OCCUPIED_AWAKE/OCCUPIED_QUIET → in-bed-state mapping.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.replay_state import score_summaries  # noqa: E402


def _summary(state_hist=None, **kw):
    base = dict(
        night="2026-05-02", zone="left",
        ticks=100, degraded_ticks=0,
        state_histogram=state_hist or {},
        overrides=0, overrides_well_anticipated=0,
        persistent_off_bed_ticks=0, persistent_off_bed_correct=0,
        mid_night_ticks=0, mid_night_stable_ticks=0,
    )
    base.update(kw)
    return base


class TestScoreAggregation:
    def test_perfect_night_passes_all_buckets(self):
        s = _summary(
            state_hist={"STABLE_SLEEP": 50, "OFF_BED": 50},
            overrides=10, overrides_well_anticipated=10,
            persistent_off_bed_ticks=50, persistent_off_bed_correct=50,
            mid_night_ticks=20, mid_night_stable_ticks=10,  # 50%
        )
        agg = score_summaries([s])
        assert agg["bucket1_override_lead"]["pass"]
        assert agg["bucket2_off_bed_fp"]["pass"]
        assert agg["bucket3_stability_mass"]["pass"]

    def test_degraded_share_computed(self):
        s = _summary(ticks=100, degraded_ticks=80)
        agg = score_summaries([s])
        assert agg["movement_degraded_share"] == 0.80

    def test_skipped_summaries_are_excluded(self):
        s_ok = _summary(state_hist={"STABLE_SLEEP": 1})
        s_skip = {"skipped": "no_readings", "ticks": 0}
        agg = score_summaries([s_ok, s_skip])
        assert agg["summaries_count"] == 1

    def test_min_movement_filter_excludes_degraded_nights(self):
        instrumented = _summary(ticks=100, degraded_ticks=10,  # 90% movement
                                state_hist={"STABLE_SLEEP": 50})
        degraded = _summary(ticks=100, degraded_ticks=95,       # 5% movement
                            state_hist={"OCCUPIED_QUIET": 50})
        agg = score_summaries([instrumented, degraded], min_movement_share=0.5)
        assert agg["summaries_count"] == 1
        assert agg["summaries_skipped_low_movement"] == 1

    def test_no_overrides_yields_pass_with_none_coverage(self):
        agg = score_summaries([_summary(state_hist={"STABLE_SLEEP": 1})])
        assert agg["bucket1_override_lead"]["coverage"] is None
        assert agg["bucket1_override_lead"]["pass"] is True

    def test_unreached_states_listed(self):
        agg = score_summaries([_summary(state_hist={"STABLE_SLEEP": 1})])
        assert "OFF_BED" in agg["unreached_states"]
        assert "STABLE_SLEEP" not in agg["unreached_states"]

    def test_below_band_fails_stability(self):
        # 5% stable mid-night → below 30% band
        s = _summary(mid_night_ticks=100, mid_night_stable_ticks=5)
        agg = score_summaries([s])
        assert not agg["bucket3_stability_mass"]["pass"]

    def test_above_band_fails_stability(self):
        # 95% stable mid-night → above 80% band (rule too permissive)
        s = _summary(mid_night_ticks=100, mid_night_stable_ticks=95)
        agg = score_summaries([s])
        assert not agg["bucket3_stability_mass"]["pass"]
