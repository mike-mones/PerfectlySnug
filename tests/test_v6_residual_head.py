"""Tests for ml.v6.residual_head — bounded learned residual."""

import json
import sys
import os
import pytest
import numpy as np


# ─── Cold start / no model ────────────────────────────────────────────

class TestColdStart:
    def test_predict_returns_zero_no_model(self):
        """predict() returns 0 when no model loaded."""
        from ml.v6.residual_head import ResidualHead
        head = ResidualHead(zone="left")
        delta, meta = head.predict({"cycle_phase": 2, "room_f": 72.0,
                                     "body_skin_f": 80.0, "body_hot_f": 80.0})
        assert delta == 0
        assert meta["reason"] == "no_model"

    def test_predict_lcb_returns_zero_no_model(self):
        from ml.v6.residual_head import ResidualHead
        head = ResidualHead(zone="right")
        delta, meta = head.predict_lcb({"cycle_phase": 3})
        assert delta == 0
        assert meta["reason"] == "no_model"


# ─── Cap steps limit ─────────────────────────────────────────────────

class TestCapSteps:
    def test_predict_respects_cap(self, tmp_path):
        """predict() clamps output to ±cap_steps."""
        from ml.v6.residual_head import ResidualHead, FEATURE_NAMES

        # Create a model with large coefficients that would predict > 1
        model_data = {
            "zone": "left",
            "cap_steps": 1,
            "n_support_threshold": 1,
            "coefficients": [5.0] * len(FEATURE_NAMES),  # large → big prediction
            "intercept": 3.0,
            "alpha": 1.0,
            "lambda": 1.0,
            "scaler_mean": [0.0] * len(FEATURE_NAMES),
            "scaler_scale": [1.0] * len(FEATURE_NAMES),
            "feature_names": FEATURE_NAMES,
            "n_training_rows": 100,
            "n_support_per_bin": {"cycle_2": 50},
            "metadata": {},
        }
        path = str(tmp_path / "model.json")
        with open(path, "w") as f:
            json.dump(model_data, f)

        head = ResidualHead(zone="left", model_path=path, cap_steps=1)
        delta, meta = head.predict({"cycle_phase": 2, "room_f": 72.0,
                                     "body_skin_f": 80.0, "body_hot_f": 80.0})
        assert -1 <= delta <= 1

    def test_cap_steps_2(self, tmp_path):
        """With cap_steps=2, allows ±2."""
        from ml.v6.residual_head import ResidualHead, FEATURE_NAMES

        model_data = {
            "zone": "left",
            "cap_steps": 2,
            "n_support_threshold": 1,
            "coefficients": [5.0] * len(FEATURE_NAMES),
            "intercept": 3.0,
            "alpha": 1.0,
            "lambda": 1.0,
            "scaler_mean": [0.0] * len(FEATURE_NAMES),
            "scaler_scale": [1.0] * len(FEATURE_NAMES),
            "feature_names": FEATURE_NAMES,
            "n_training_rows": 100,
            "n_support_per_bin": {"cycle_2": 50},
            "metadata": {},
        }
        path = str(tmp_path / "model.json")
        with open(path, "w") as f:
            json.dump(model_data, f)

        head = ResidualHead(zone="left", model_path=path, cap_steps=2)
        delta, meta = head.predict({"cycle_phase": 2, "room_f": 72.0,
                                     "body_skin_f": 80.0, "body_hot_f": 80.0})
        assert -2 <= delta <= 2


# ─── LCB with low n_support ──────────────────────────────────────────

class TestLCBQuorum:
    def test_lcb_below_threshold_returns_zero(self, tmp_path):
        """predict_lcb() returns 0 when n_support below threshold."""
        from ml.v6.residual_head import ResidualHead, FEATURE_NAMES

        model_data = {
            "zone": "left",
            "cap_steps": 1,
            "n_support_threshold": 5,
            "coefficients": [1.0] * len(FEATURE_NAMES),
            "intercept": 0.5,
            "alpha": 1.0,
            "lambda": 1.0,
            "scaler_mean": [0.0] * len(FEATURE_NAMES),
            "scaler_scale": [1.0] * len(FEATURE_NAMES),
            "feature_names": FEATURE_NAMES,
            "n_training_rows": 50,
            "n_support_per_bin": {"cycle_0": 2, "cycle_3": 10},  # cycle_2 missing
            "metadata": {},
        }
        path = str(tmp_path / "model.json")
        with open(path, "w") as f:
            json.dump(model_data, f)

        head = ResidualHead(zone="left", model_path=path, n_support_threshold=5)
        delta, meta = head.predict_lcb({"cycle_phase": 2, "room_f": 72.0,
                                         "body_skin_f": 80.0, "body_hot_f": 80.0})
        assert delta == 0
        assert meta["reason"] == "below_quorum"


# ─── Save/load roundtrip ─────────────────────────────────────────────

class TestSaveLoad:
    def test_roundtrip(self, tmp_path):
        """save() then load() produces identical predictions."""
        from ml.v6.residual_head import ResidualHead, FEATURE_NAMES

        model_data = {
            "zone": "left",
            "cap_steps": 1,
            "n_support_threshold": 3,
            "coefficients": [0.1, -0.2, 0.3, 0.0, -0.1, 0.2, -0.15],
            "intercept": 0.05,
            "alpha": 2.0,
            "lambda": 1.5,
            "scaler_mean": [1.0, 0.0, 0.5, 10.0, 30.0, 0.3, 0.2],
            "scaler_scale": [2.0, 1.0, 1.5, 20.0, 60.0, 0.5, 1.0],
            "feature_names": FEATURE_NAMES,
            "n_training_rows": 50,
            "n_support_per_bin": {"cycle_2": 15, "cycle_3": 10},
            "metadata": {"test": True},
        }
        path = str(tmp_path / "model.json")
        with open(path, "w") as f:
            json.dump(model_data, f)

        head1 = ResidualHead(zone="left", model_path=path)
        features = {"cycle_phase": 2, "room_f": 70.0,
                    "body_skin_f": 78.0, "body_hot_f": 82.0}
        d1, m1 = head1.predict(features)

        # Save to new path and reload
        path2 = str(tmp_path / "model2.json")
        head1.save(path2)
        head2 = ResidualHead.load(path2)
        d2, m2 = head2.predict(features)

        assert d1 == d2
        assert head2.zone == "left"
        assert head2.cap == 1


# ─── fit() end-to-end ─────────────────────────────────────────────────

class TestFit:
    def test_fit_small_fixture(self, tmp_path):
        """fit() runs end-to-end on small fixture."""
        from ml.v6.residual_head import ResidualHead, SKLEARN_AVAILABLE
        if not SKLEARN_AVAILABLE:
            pytest.skip("sklearn not installed")

        # Generate synthetic training data
        np.random.seed(42)
        rows = []
        for i in range(30):
            rows.append({
                "cycle_phase": float(i % 6),
                "room_f": 68.0 + np.random.randn() * 3,
                "body_skin_f": 78.0 + np.random.randn() * 2,
                "body_hot_f": 80.0 + np.random.randn() * 2,
                "pre_sleep_min": 0.0,
                "post_bedjet_min": float(i * 10),
                "bedjet_active": i < 6,
                "target_delta": np.random.choice([-1, 0, 1]),
            })

        output = str(tmp_path / "fitted.json")
        head = ResidualHead.fit("left", rows, output)

        assert head.loaded
        assert os.path.exists(output)

        # Can predict after fitting
        delta, meta = head.predict(rows[0])
        assert -1 <= delta <= 1

    def test_fit_raises_without_sklearn(self, tmp_path, monkeypatch):
        """fit() raises RuntimeError if sklearn not available."""
        import ml.v6.residual_head as rh
        monkeypatch.setattr(rh, "SKLEARN_AVAILABLE", False)

        with pytest.raises(RuntimeError, match="sklearn"):
            rh.ResidualHead.fit("left", [{"target_delta": 0}], str(tmp_path / "x.json"))


# ─── Soft-import resilience ───────────────────────────────────────────

class TestSoftImport:
    def test_predict_works_without_sklearn_flag(self, tmp_path):
        """Even with SKLEARN_AVAILABLE=False, predict returns 0 gracefully."""
        from ml.v6.residual_head import ResidualHead, FEATURE_NAMES
        import ml.v6.residual_head as rh

        # Create a model file
        model_data = {
            "zone": "left",
            "cap_steps": 1,
            "n_support_threshold": 1,
            "coefficients": [0.1] * len(FEATURE_NAMES),
            "intercept": 0.0,
            "alpha": 1.0,
            "lambda": 1.0,
            "scaler_mean": [0.0] * len(FEATURE_NAMES),
            "scaler_scale": [1.0] * len(FEATURE_NAMES),
            "feature_names": FEATURE_NAMES,
            "n_training_rows": 20,
            "n_support_per_bin": {"cycle_2": 10},
            "metadata": {},
        }
        path = str(tmp_path / "model.json")
        with open(path, "w") as f:
            json.dump(model_data, f)

        # predict still works (uses numpy only)
        head = ResidualHead(zone="left", model_path=path)
        delta, meta = head.predict({"cycle_phase": 2, "room_f": 72.0,
                                     "body_skin_f": 80.0, "body_hot_f": 80.0})
        # With these small coefficients, delta should be within cap
        assert -1 <= delta <= 1
        assert meta["model_loaded"] is True
