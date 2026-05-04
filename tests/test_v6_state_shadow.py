"""Unit tests for appdaemon/v6_state_shadow.py — pure helpers only.

The full AppDaemon hass.Hass class requires the AppDaemon runtime; we test
the importable pure helpers (_rms_consecutive_deltas, _variance_consecutive_deltas,
_max_consecutive_delta, _ols_slope_per_15m) directly.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Avoid importing hassapi at test time. The pure helpers are at module
# bottom; import path needs the appdaemon dir on sys.path. We import the
# module under test by stubbing out hassapi first.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


class _FakeHass:
    class Hass:
        pass


sys.modules.setdefault("hassapi", _FakeHass())

from appdaemon.v6_state_shadow import (  # noqa: E402
    _max_consecutive_delta,
    _ols_slope_per_15m,
    _rms_consecutive_deltas,
    _variance_consecutive_deltas,
)


class TestRMS:
    def test_empty_returns_none(self):
        assert _rms_consecutive_deltas([]) is None

    def test_single_value_returns_none(self):
        assert _rms_consecutive_deltas([5.0]) is None

    def test_constant_series_returns_zero(self):
        assert _rms_consecutive_deltas([3.0, 3.0, 3.0, 3.0]) == 0.0

    def test_known_rms(self):
        # values 0, 3, 0  ⇒ deltas |3-0|=3, |0-3|=3 ⇒ rms = 3.0
        assert _rms_consecutive_deltas([0.0, 3.0, 0.0]) == pytest.approx(3.0)


class TestVariance:
    def test_too_few_returns_none(self):
        assert _variance_consecutive_deltas([1.0, 2.0]) is None

    def test_constant_deltas_returns_zero(self):
        # values 0,1,2,3 ⇒ deltas all 1 ⇒ variance 0
        assert _variance_consecutive_deltas([0, 1, 2, 3]) == 0.0

    def test_varied_deltas(self):
        # values 0,1,4,5 ⇒ deltas [1,3,1] mean=1.667 var = sample_variance
        v = _variance_consecutive_deltas([0, 1, 4, 5])
        assert v == pytest.approx(1.3333333, rel=1e-3)


class TestMaxDelta:
    def test_empty(self):
        assert _max_consecutive_delta([]) is None

    def test_known_max(self):
        assert _max_consecutive_delta([0, 1, 10, 11, 2]) == 9


class TestOLSSlope:
    def test_too_few_samples_returns_none(self):
        # <5 samples
        assert _ols_slope_per_15m([(0, 70), (60, 71), (120, 72)]) is None

    def test_flat_line_zero_slope(self):
        samples = [(i * 60.0, 80.0) for i in range(15)]
        assert _ols_slope_per_15m(samples) == pytest.approx(0.0)

    def test_known_rising_slope(self):
        # Rise 1°F over 15 min ⇒ slope_per_15m = 1.0
        samples = [(i * 60.0, 70.0 + i / 15.0) for i in range(16)]
        slope = _ols_slope_per_15m(samples)
        assert slope == pytest.approx(1.0, rel=0.01)

    def test_drops_invalid_samples(self):
        samples = [(i * 60.0, None if i == 5 else 70.0) for i in range(10)]
        # Remaining 9 samples are constant ⇒ slope = 0
        assert _ols_slope_per_15m(samples) == pytest.approx(0.0)
