"""Firmware plant forward predictor for PerfectlySnug v6.

Implements the Stage-1 (slow leaky-max-hold setpoint generator) and
Stage-2 (Hammerstein P-controller) forward predictor from
recommendation.md §6 pseudocode and §12 (cap-vs-L_active).

This is a PREDICTOR ONLY — not an optimizer. Used for:
  (a) divergence-guard sanity (predict expected blower given target L_active)
  (b) sleep-stage-unaware rollout for BedJet residual decay model

Design ref: 2026-05-01_recommendation.md §2.1 (stack row 6), §6 step 7
            2026-05-01_opt-mpc.md (firmware plant model)
            2026-05-01_rc_synthesis.md (two-stage cascade physics)
"""

from __future__ import annotations

import json
import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)

# Empirical anchor points from agent.md / rc_synthesis:
# L1=-8 → 69°F, L1=0 → 91.4°F, L1=+5 → 95.9°F (~2.8°F per unit above 0)
# Linear interpolation between anchors; constant extrapolation beyond.
_DEFAULT_ANCHORS = [
    (-8, 69.0),
    (0, 91.4),
    (5, 95.9),
]


class FirmwarePlant:
    """Stage-1 + Stage-2 forward predictor.

    Stage 1: Leaky-max-hold setpoint generator — maps L_active integer
    to a predicted firmware target setpoint in °F using a piecewise-linear
    cap table.

    Stage 2: Hammerstein P-controller approximation — predicts blower
    output percent given setting, ambient, and body temperatures.

    Loaded from cap table fit by tools/firmware_cap_fit.py. If cap table
    is missing, falls back to empirical anchors from agent.md.

    Design ref: rc_synthesis.md §"TL;DR — what RC actually does"
    """

    def __init__(self, cap_table_path: Optional[str] = None):
        """Initialize plant model.

        Args:
            cap_table_path: Path to JSON cap table from firmware_cap_fit.py.
                If None or file missing, uses empirical anchors.
        """
        self._anchors = list(_DEFAULT_ANCHORS)
        self._cap_table_loaded = False

        if cap_table_path and os.path.isfile(cap_table_path):
            try:
                with open(cap_table_path, "r") as f:
                    data = json.load(f)
                if "anchors" in data and len(data["anchors"]) >= 2:
                    self._anchors = [
                        (pt["setting"], pt["setpoint_f"])
                        for pt in data["anchors"]
                    ]
                    self._cap_table_loaded = True
                    logger.info("FirmwarePlant: loaded cap table from %s", cap_table_path)
                else:
                    logger.warning(
                        "FirmwarePlant: cap table at %s has invalid format, using anchors",
                        cap_table_path,
                    )
            except (json.JSONDecodeError, KeyError, TypeError) as e:
                logger.warning(
                    "FirmwarePlant: failed to load cap table (%s), using anchors", e
                )
        elif cap_table_path:
            logger.warning(
                "FirmwarePlant: cap table not found at %s, using empirical anchors",
                cap_table_path,
            )

        # Sort anchors by setting for interpolation
        self._anchors.sort(key=lambda x: x[0])

    @property
    def cap_table_loaded(self) -> bool:
        """Whether the cap table was successfully loaded from file."""
        return self._cap_table_loaded

    def predict_setpoint_f(self, setting: int, ambient_f: float = 72.0) -> float:
        """Predict firmware target setpoint in °F for a given L_active setting.

        Uses piecewise-linear interpolation between anchor points.
        Constant extrapolation beyond the range.

        Args:
            setting: L_active integer (typically -10 to +5)
            ambient_f: ambient/room temperature (unused in Stage-1 cap model
                       but kept for API completeness)

        Returns:
            Predicted setpoint in °F.
        """
        return self._interpolate(setting)

    def predict_blower_pct(
        self, setting: int, ambient_f: float, body_f: float
    ) -> float:
        """Predict blower output 0-100 using Stage-2 P-controller approximation.

        Implements the simplified Stage-2 model from rc_synthesis:
            target = 46.8 + 19.1*(body_max - setpoint) - 1.45*(body_avg - ambient)
            blower = 0 if target < 16.4 else clip(max(13.2, target), 0, 100)

        Simplification: body_max ≈ body_f, body_avg ≈ body_f (single sensor input).

        Args:
            setting: L_active integer
            ambient_f: room temperature in °F
            body_f: body temperature in °F (used as both body_max and body_avg)

        Returns:
            Predicted blower output percentage (0-100).
        """
        setpoint = self._interpolate(setting)
        error = body_f - setpoint
        ambient_delta = body_f - ambient_f

        # Stage-2 P-controller (rc_synthesis coefficients)
        target = 46.8 + 19.1 * error + 1.4 * error - 1.45 * ambient_delta
        # Note: rate terms omitted (static prediction, no dT/dt)

        # Hammerstein output nonlinearity
        if target < 16.4:
            return 0.0
        return max(0.0, min(100.0, max(13.2, target)))

    def step_one_minute(
        self, state: dict, setting: int, ambient_f: float, body_f: float
    ) -> dict:
        """Advance plant state one minute. Returns new state dict.

        Simulates the leaky-max-hold dynamics of Stage-1:
          setpoint(t) = max(body_f, setpoint(t-1) - leak_rate)
          capped at cap(setting)

        Args:
            state: dict with at least 'setpoint_f' and 'elapsed_sec'
            setting: current L_active integer
            ambient_f: room temperature in °F
            body_f: body temperature in °F

        Returns:
            New state dict with updated setpoint_f, blower_pct, elapsed_sec.
        """
        # Stage-1: leaky max-hold
        cap = self._interpolate(setting)
        prev_setpoint = state.get("setpoint_f", body_f)

        # Leak rate: 0.002°F per 10s → 0.012°F per minute
        leak_per_min = 0.012
        leaked = prev_setpoint - leak_per_min

        # Max-hold with cap
        new_setpoint = min(cap, max(body_f, leaked))

        # Stage-2: blower prediction
        blower = self.predict_blower_pct(setting, ambient_f, body_f)

        elapsed = state.get("elapsed_sec", 0) + 60

        return {
            "setpoint_f": new_setpoint,
            "blower_pct": blower,
            "elapsed_sec": elapsed,
            "setting": setting,
            "body_f": body_f,
            "ambient_f": ambient_f,
        }

    def _interpolate(self, setting: int) -> float:
        """Piecewise-linear interpolation with constant extrapolation."""
        anchors = self._anchors

        # Below lowest anchor: constant
        if setting <= anchors[0][0]:
            return anchors[0][1]

        # Above highest anchor: constant
        if setting >= anchors[-1][0]:
            return anchors[-1][1]

        # Linear interpolation between surrounding anchors
        for i in range(len(anchors) - 1):
            s0, t0 = anchors[i]
            s1, t1 = anchors[i + 1]
            if s0 <= setting <= s1:
                frac = (setting - s0) / (s1 - s0)
                return t0 + frac * (t1 - t0)

        # Should not reach here
        return anchors[-1][1]
