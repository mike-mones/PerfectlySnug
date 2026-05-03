"""Right-zone composite comfort proxy for PerfectlySnug v6.

Implements the exact definition from recommendation.md §5. This is the
gating metric for right-zone controller quality — not override MAE
(which is structurally underpowered with n=7 overrides).

The proxy produces a score 0..1 per tick where >=0.5 indicates discomfort.
Key metrics: mean, p90, minutes_score_ge_0_5, time_too_hot_min.

Design ref: 2026-05-01_recommendation.md §5
            val-eval §"Right-zone comfort proxy"
            red-comfort §6.3
"""

from __future__ import annotations

from typing import Optional


def score(
    *,
    body_left_f: Optional[float],
    body_avg_f: Optional[float],
    room_f: Optional[float],
    movement_density_15m: Optional[float],
    override_recent: bool = False,
    time_since_override_min: Optional[float] = None,
    body_center_f: Optional[float] = None,
    post_bedjet_min: Optional[float] = None,
    sleep_stage: Optional[str] = None,
    rail_engaged: bool = False,
    zone_baseline_movement_p75: float = 0.05,
) -> float:
    """Compute composite right-zone comfort proxy score.

    Returns a score 0..1 where >=0.5 indicates discomfort (per §5 weights).

    Sub-signals (from recommendation.md §5):
        1. body_hot excursion (max-channel): 0 at 84°F, 1 at >=88°F (weight 0.30)
        2. body_cold excursion: 0 at 73°F, 1 at <=68°F (weight within body_out_of_range)
        3. 30-min thermal volatility on body_left (approximated here) (weight 0.20)
        4. movement density (weight 0.30)
        5. stage_bad flag (weight 0.10)
        6. BedJet residual suppression
        7. rail engagement (weight 0.10)

    Args:
        body_left_f: body left sensor temperature in °F (primary skin sensor)
        body_avg_f: average body temperature (used for volatility approximation)
        room_f: bedroom temperature in °F
        movement_density_15m: movement density over last 15 minutes (0..1+ scale)
        override_recent: True if an override occurred recently
        time_since_override_min: minutes since last override (lifts score if recent)
        body_center_f: body center sensor (for hot-side max-channel, post-BedJet)
        post_bedjet_min: minutes since BedJet deactivated
        sleep_stage: current sleep stage label
        rail_engaged: True if right_overheat_safety rail is engaged
        zone_baseline_movement_p75: p75 of quiet-night movement density baseline

    Returns:
        Float 0.0 to 1.0 — higher means more discomfort.
    """
    # Handle missing body_left_f gracefully
    if body_left_f is None:
        return 0.0

    # --- Sub-signal 1: body_hot excursion (max-channel per §5) ---
    body_hot = body_left_f
    if (
        post_bedjet_min is not None
        and post_bedjet_min > 60
        and body_center_f is not None
    ):
        body_hot = max(body_left_f, body_center_f)

    body_hot_excess = _clip((body_hot - 84.0) / 4.0, 0.0, 1.0)

    # --- Sub-signal 2: body_cold excursion ---
    body_cold_excess = _clip((73.0 - body_left_f) / 5.0, 0.0, 1.0)

    body_out_of_range = max(body_hot_excess, body_cold_excess)

    # --- Sub-signal 3: thermal volatility (approximation) ---
    # Full implementation uses rolling_sd(history_30m.body_left_f).
    # Here we approximate with body_avg deviation as a proxy.
    if body_avg_f is not None:
        body_sd_proxy = abs(body_left_f - body_avg_f)
    else:
        body_sd_proxy = 0.0
    body_30m_sd_excess = _clip((body_sd_proxy - 1.2) / 2.0, 0.0, 1.0)

    # --- Sub-signal 4: movement density ---
    if movement_density_15m is not None:
        denominator = max(0.05, 2.0 * zone_baseline_movement_p75)
        movement_excess = _clip(movement_density_15m / denominator, 0.0, 1.0)
    else:
        movement_excess = 0.0

    # --- Sub-signal 5: stage_bad ---
    stage_bad = 1.0 if sleep_stage in ("awake", "inbed", "unknown", None) else 0.0

    # --- Sub-signal 6: BedJet residual suppression ---
    in_bedjet = False
    if post_bedjet_min is not None and post_bedjet_min < 30:
        in_bedjet = True
    if in_bedjet:
        body_out_of_range = 0.0
        body_30m_sd_excess *= 0.3

    # --- Sub-signal 7: rail engagement ---
    rail_score = 1.0 if rail_engaged else 0.0

    # --- Weighted composite (§5 exact weights) ---
    raw_score = (
        0.30 * body_out_of_range
        + 0.20 * body_30m_sd_excess
        + 0.30 * movement_excess
        + 0.10 * stage_bad
        + 0.10 * rail_score
    )

    # Override recency boost: if override was very recent, it indicates
    # active discomfort that the proxy should reflect
    if override_recent and time_since_override_min is not None:
        if time_since_override_min <= 10:
            # Boost score toward discomfort (additive, capped)
            override_boost = _clip((10.0 - time_since_override_min) / 20.0, 0.0, 0.2)
            raw_score += override_boost

    return _clip(raw_score, 0.0, 1.0)


def minutes_score_ge_0_5(rows: list[dict]) -> int:
    """Count minutes (or 5-min ticks × 5) where the comfort score >= 0.5.

    Each row in `rows` should be a dict of kwargs suitable for score().
    Assumes 5-minute cadence per tick unless 'cadence_min' key is present.

    Args:
        rows: list of dicts with score() compatible kwargs

    Returns:
        Estimated minutes with score >= 0.5.
    """
    count = 0
    for row in rows:
        cadence = row.pop("cadence_min", 5) if "cadence_min" in row else 5
        s = score(**row)
        if s >= 0.5:
            count += cadence
    return count


def time_too_hot_min(rows: list[dict], threshold_f: float = 84.0) -> int:
    """Count minutes where body_left_f exceeds the hot threshold.

    Assumes 5-minute cadence per tick unless 'cadence_min' key is present.

    Args:
        rows: list of dicts with at least 'body_left_f' key
        threshold_f: temperature threshold in °F (default 84.0 per user default #4)

    Returns:
        Estimated minutes with body_left_f > threshold_f.
    """
    count = 0
    for row in rows:
        cadence = row.get("cadence_min", 5)
        body_left = row.get("body_left_f")
        if body_left is not None and body_left > threshold_f:
            count += cadence
    return count


def _clip(val: float, lo: float, hi: float) -> float:
    """Clip value to [lo, hi]."""
    return max(lo, min(hi, val))
