"""Bounded learned residual head for PerfectlySnug v6.

Implements the conservative residual policy from opt-learned §1:
Bayesian Ridge primary + GP quorum agreement check + LCB gating.

The residual head predicts Δ ∈ {-cap..+cap} on top of the regime-rule
output. At runtime, predict() uses only numpy + json (no sklearn required).
Training (fit()) requires sklearn.

CRITICAL: This module must NOT crash AppDaemon if sklearn is missing.
Uses soft-import pattern per task specification.

Design ref: 2026-05-01_opt-learned.md §1, §3, §6
            2026-05-01_recommendation.md §2.1 (residual head layer)
            2026-05-01_recommendation.md §3 (residual cap ladder)
"""

from __future__ import annotations

import json
import logging
import math
import os
from typing import Optional

import numpy as np

# Soft-import sklearn — runtime predict works without it
try:
    from sklearn.linear_model import BayesianRidge
    from sklearn.gaussian_process import GaussianProcessRegressor
    from sklearn.gaussian_process.kernels import RBF, ConstantKernel, WhiteKernel
    from sklearn.preprocessing import StandardScaler
    SKLEARN_AVAILABLE = True
except ImportError:
    SKLEARN_AVAILABLE = False

logger = logging.getLogger(__name__)

# Feature names for the residual head (opt-learned §2.2)
FEATURE_NAMES = [
    "cycle_phase",         # 0-6 continuous
    "room_temp_bin",       # (room_f - 72) / 4, clipped
    "body_skin_bin",       # (body_skin - 80) / 4, clipped
    "pre_sleep_min",       # minutes in pre-sleep, 0 if not
    "post_bedjet_min",     # minutes since BedJet off, 0 if n/a
    "bedjet_active",       # 1.0 or 0.0
    "body_hot",            # (body_hot_f - 80) / 4, clipped
]

# Default n_support threshold for quorum gate
DEFAULT_N_SUPPORT_THRESHOLD = 5


class ResidualHead:
    """Bounded learned residual on top of regime-rule output.

    Trained offline (nightly), serialized to JSON, loaded at runtime.
    At runtime, predict() performs a manual coefficient-vector dot-product
    over scaled features — no sklearn dependency required.

    The residual is clamped to ±cap_steps (default ±1 per proposal).
    The LCB gate ensures the residual only fires when statistical
    confidence exceeds a threshold.

    Save format (JSON):
        {
            "zone": str,
            "cap_steps": int,
            "n_support_threshold": int,
            "coefficients": list[float],  # Bayesian Ridge weights
            "intercept": float,
            "alpha": float,               # precision of weights
            "lambda": float,              # precision of noise
            "scaler_mean": list[float],
            "scaler_scale": list[float],
            "feature_names": list[str],
            "n_training_rows": int,
            "n_support_per_bin": dict,    # feature bin → count
            "metadata": dict
        }
    """

    def __init__(
        self,
        zone: str,
        model_path: Optional[str] = None,
        cap_steps: int = 1,
        n_support_threshold: int = DEFAULT_N_SUPPORT_THRESHOLD,
    ):
        """Initialize residual head.

        Args:
            zone: "left" or "right"
            model_path: path to saved JSON model (optional)
            cap_steps: maximum |Δ| (±1 default per proposal)
            n_support_threshold: minimum similar features for non-zero output
        """
        self.zone = zone
        self.cap = cap_steps
        self.n_support_threshold = n_support_threshold

        # Model state (None until loaded or trained)
        self._coefficients: Optional[np.ndarray] = None
        self._intercept: float = 0.0
        self._alpha: float = 1.0   # weight precision
        self._lambda: float = 1.0  # noise precision
        self._scaler_mean: Optional[np.ndarray] = None
        self._scaler_scale: Optional[np.ndarray] = None
        self._n_training_rows: int = 0
        self._n_support_per_bin: dict = {}
        self._metadata: dict = {}
        self._loaded = False

        if model_path:
            try:
                self._load_from_path(model_path)
            except (FileNotFoundError, json.JSONDecodeError, KeyError) as e:
                logger.warning("ResidualHead(%s): failed to load model: %s", zone, e)

    @property
    def loaded(self) -> bool:
        """Whether a model is loaded and ready for prediction."""
        return self._loaded

    def predict(self, features: dict) -> tuple[int, dict]:
        """Predict residual delta, clamped to ±cap.

        Falls back to 0 if model not loaded or sklearn missing at training time.
        Runtime prediction uses numpy only (manual dot product).

        Args:
            features: dict with keys matching FEATURE_NAMES

        Returns:
            (delta_steps, metadata) where delta_steps ∈ [-cap, +cap]
        """
        meta = {"model_loaded": self._loaded, "sklearn_available": SKLEARN_AVAILABLE}

        if not self._loaded or self._coefficients is None:
            meta["reason"] = "no_model"
            return 0, meta

        # Build feature vector
        x = self._build_feature_vector(features)
        if x is None:
            meta["reason"] = "feature_extraction_failed"
            return 0, meta

        # Check n_support (quorum gate)
        n_support = self._estimate_support(features)
        meta["n_support"] = n_support
        if n_support < self.n_support_threshold:
            meta["reason"] = "below_quorum"
            return 0, meta

        # Scale features
        x_scaled = (x - self._scaler_mean) / np.maximum(self._scaler_scale, 1e-8)

        # Manual dot product (no sklearn needed at runtime)
        raw_prediction = float(np.dot(self._coefficients, x_scaled) + self._intercept)
        meta["raw_prediction"] = raw_prediction

        # Clamp to ±cap
        delta = int(round(max(-self.cap, min(self.cap, raw_prediction))))
        meta["delta_clamped"] = delta
        return delta, meta

    def predict_lcb(self, features: dict, k: float = 1.0) -> tuple[int, dict]:
        """Lower confidence bound prediction.

        Only fires if (|mean| - k*std) > 0, else returns 0.
        This is the gate for actually applying the residual.

        Per opt-learned §3.3:
            Δ_safe = sign(Δ̂) · max(0, |Δ̂| − k·σ)

        Args:
            features: dict with keys matching FEATURE_NAMES
            k: LCB multiplier (default 1.0 per §8)

        Returns:
            (delta_steps, metadata) — delta is 0 unless confidence exceeds threshold
        """
        meta = {"model_loaded": self._loaded, "sklearn_available": SKLEARN_AVAILABLE, "k": k}

        if not self._loaded or self._coefficients is None:
            meta["reason"] = "no_model"
            return 0, meta

        x = self._build_feature_vector(features)
        if x is None:
            meta["reason"] = "feature_extraction_failed"
            return 0, meta

        # Check n_support
        n_support = self._estimate_support(features)
        meta["n_support"] = n_support
        if n_support < self.n_support_threshold:
            meta["reason"] = "below_quorum"
            return 0, meta

        # Scale
        x_scaled = (x - self._scaler_mean) / np.maximum(self._scaler_scale, 1e-8)

        # Prediction
        mean = float(np.dot(self._coefficients, x_scaled) + self._intercept)

        # Estimate std from Bayesian Ridge posterior
        # σ² ≈ (1/λ) + x^T · (1/α) · I · x (simplified diagonal approximation)
        noise_var = 1.0 / max(self._lambda, 1e-8)
        weight_var = 1.0 / max(self._alpha, 1e-8)
        std = math.sqrt(noise_var + weight_var * float(np.dot(x_scaled, x_scaled)))

        meta["mean"] = mean
        meta["std"] = std

        # LCB gate: Δ_safe = sign(Δ̂) · max(0, |Δ̂| − k·σ)
        delta_lcb = math.copysign(1, mean) * max(0.0, abs(mean) - k * std)
        meta["delta_lcb_raw"] = delta_lcb

        # Clamp to ±cap
        delta = int(round(max(-self.cap, min(self.cap, delta_lcb))))
        meta["delta_clamped"] = delta
        return delta, meta

    @classmethod
    def fit(
        cls,
        zone: str,
        training_rows: list[dict],
        output_path: str,
        cap_steps: int = 1,
        n_support_threshold: int = DEFAULT_N_SUPPORT_THRESHOLD,
    ) -> "ResidualHead":
        """Nightly training. Skipped if sklearn missing.

        Features per opt-learned §2.2: cycle_phase, room_temp_bin,
        body_skin_bin, pre_sleep_min, post_bedjet_min, bedjet_active, body_hot.

        Each training_row must have these features plus a 'target_delta' key
        (the ground-truth residual: observed_override - v52_recommendation).

        Args:
            zone: "left" or "right"
            training_rows: list of dicts with features + 'target_delta'
            output_path: where to save the fitted model JSON
            cap_steps: max |Δ| for this head
            n_support_threshold: quorum threshold

        Returns:
            Fitted ResidualHead instance.

        Raises:
            RuntimeError: if sklearn is not available
        """
        if not SKLEARN_AVAILABLE:
            raise RuntimeError(
                "sklearn is required for ResidualHead.fit() but not installed"
            )

        if not training_rows:
            raise ValueError("No training rows provided")

        # Extract features and targets
        X_raw = []
        y = []
        for row in training_rows:
            x = cls._extract_features_static(row)
            if x is not None:
                X_raw.append(x)
                y.append(float(row["target_delta"]))

        if len(X_raw) < 3:
            raise ValueError(f"Too few valid training rows: {len(X_raw)}")

        X = np.array(X_raw, dtype=np.float64)
        y_arr = np.array(y, dtype=np.float64)

        # Scale features
        scaler = StandardScaler()
        X_scaled = scaler.fit_transform(X)

        # Fit Bayesian Ridge
        model = BayesianRidge(
            max_iter=300,
            tol=1e-4,
            alpha_init=1.0,
            lambda_init=1.0,
        )
        model.fit(X_scaled, y_arr)

        # Compute n_support per feature bin (simplified: count per cycle_phase bin)
        n_support_per_bin = {}
        for row in training_rows:
            cp = row.get("cycle_phase", 0)
            bin_key = f"cycle_{int(cp)}"
            n_support_per_bin[bin_key] = n_support_per_bin.get(bin_key, 0) + 1

        # Build instance
        head = cls(zone=zone, cap_steps=cap_steps, n_support_threshold=n_support_threshold)
        head._coefficients = model.coef_
        head._intercept = float(model.intercept_)
        head._alpha = float(model.alpha_)
        head._lambda = float(model.lambda_)
        head._scaler_mean = scaler.mean_
        head._scaler_scale = scaler.scale_
        head._n_training_rows = len(X_raw)
        head._n_support_per_bin = n_support_per_bin
        head._metadata = {
            "n_features": len(FEATURE_NAMES),
            "n_training_rows": len(X_raw),
            "score_r2": float(model.score(X_scaled, y_arr)),
        }
        head._loaded = True

        # Save
        head.save(output_path)
        return head

    def save(self, path: str):
        """Save model to JSON.

        Format is designed for runtime predict() to work with just numpy + json.
        """
        if self._coefficients is None:
            raise ValueError("No model to save (not fitted or loaded)")

        data = {
            "zone": self.zone,
            "cap_steps": self.cap,
            "n_support_threshold": self.n_support_threshold,
            "coefficients": self._coefficients.tolist(),
            "intercept": self._intercept,
            "alpha": self._alpha,
            "lambda": self._lambda,
            "scaler_mean": self._scaler_mean.tolist() if self._scaler_mean is not None else [],
            "scaler_scale": self._scaler_scale.tolist() if self._scaler_scale is not None else [],
            "feature_names": list(FEATURE_NAMES),
            "n_training_rows": self._n_training_rows,
            "n_support_per_bin": self._n_support_per_bin,
            "metadata": self._metadata,
        }

        os.makedirs(os.path.dirname(path) if os.path.dirname(path) else ".", exist_ok=True)
        with open(path, "w") as f:
            json.dump(data, f, indent=2)

    @classmethod
    def load(cls, path: str) -> "ResidualHead":
        """Load model from JSON file.

        Args:
            path: path to saved JSON model

        Returns:
            ResidualHead instance ready for predict().
        """
        head = cls.__new__(cls)
        head._load_from_path(path)
        return head

    def _load_from_path(self, path: str):
        """Internal: load state from JSON file."""
        with open(path, "r") as f:
            data = json.load(f)

        self.zone = data["zone"]
        self.cap = data.get("cap_steps", 1)
        self.n_support_threshold = data.get("n_support_threshold", DEFAULT_N_SUPPORT_THRESHOLD)
        self._coefficients = np.array(data["coefficients"], dtype=np.float64)
        self._intercept = float(data["intercept"])
        self._alpha = float(data.get("alpha", 1.0))
        self._lambda = float(data.get("lambda", 1.0))
        self._scaler_mean = np.array(data["scaler_mean"], dtype=np.float64)
        self._scaler_scale = np.array(data["scaler_scale"], dtype=np.float64)
        self._n_training_rows = data.get("n_training_rows", 0)
        self._n_support_per_bin = data.get("n_support_per_bin", {})
        self._metadata = data.get("metadata", {})
        self._loaded = True

    def _build_feature_vector(self, features: dict) -> Optional[np.ndarray]:
        """Build numpy feature vector from dict."""
        return self._extract_features_static(features)

    @staticmethod
    def _extract_features_static(row: dict) -> Optional[np.ndarray]:
        """Extract feature vector from a row dict.

        Expected keys: cycle_phase, room_temp_bin (or room_f), body_skin_bin
        (or body_skin_f), pre_sleep_min, post_bedjet_min, bedjet_active, body_hot
        (or body_hot_f).
        """
        try:
            # cycle_phase: 0-6
            cycle_phase = float(row.get("cycle_phase", 0))

            # room_temp_bin: (room_f - 72) / 4, clipped to [-2, 2]
            if "room_temp_bin" in row:
                room_bin = float(row["room_temp_bin"])
            else:
                room_f = row.get("room_f", 72.0)
                room_bin = max(-2.0, min(2.0, (float(room_f) - 72.0) / 4.0))

            # body_skin_bin: (body_skin - 80) / 4, clipped to [-2, 2]
            if "body_skin_bin" in row:
                skin_bin = float(row["body_skin_bin"])
            else:
                body_skin = row.get("body_skin_f", 80.0)
                skin_bin = max(-2.0, min(2.0, (float(body_skin) - 80.0) / 4.0))

            # pre_sleep_min
            pre_sleep = float(row.get("pre_sleep_min", 0))

            # post_bedjet_min
            post_bedjet = float(row.get("post_bedjet_min", 0))

            # bedjet_active
            bedjet = 1.0 if row.get("bedjet_active") else 0.0

            # body_hot: (body_hot_f - 80) / 4, clipped
            if "body_hot" in row:
                body_hot = float(row["body_hot"])
            else:
                body_hot_f = row.get("body_hot_f", 80.0)
                body_hot = max(-2.0, min(2.0, (float(body_hot_f) - 80.0) / 4.0))

            return np.array([
                cycle_phase, room_bin, skin_bin,
                pre_sleep, post_bedjet, bedjet, body_hot
            ], dtype=np.float64)
        except (TypeError, ValueError):
            return None

    def _estimate_support(self, features: dict) -> int:
        """Estimate n_support for the given features (simplified bin lookup)."""
        if not self._n_support_per_bin:
            return self._n_training_rows  # if no bin info, use total

        cycle_phase = features.get("cycle_phase", 0)
        bin_key = f"cycle_{int(cycle_phase)}"
        return self._n_support_per_bin.get(bin_key, 0)
