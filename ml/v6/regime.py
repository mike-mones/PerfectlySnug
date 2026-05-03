"""Deterministic regime classifier for PerfectlySnug v6.

Implements the first-match priority regime classification per
recommendation.md §3 (per-zone policies) and §6 (pseudocode).

The classifier is a pure function with no I/O — it maps observable state
to a regime label, base setting, and downstream advice. The controller
uses this to select the per-regime rule that computes the final target.

Design ref: 2026-05-01_recommendation.md §3, §6, §8
            2026-05-01_opt-hybrid.md §1 (regime definitions)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass(frozen=True)
class RegimeConfig:
    """Tunable parameters for the regime classifier.

    Initial values from recommendation.md §8. All temperatures in °F.
    """

    # Initial-bed cooling window (minutes)
    initial_bed_cooling_min: int = 30
    initial_bed_cold_room_min: int = 15  # shrink when room < 66°F

    # Cold-room compensation thresholds
    cold_room_threshold_f: float = 70.0
    cold_room_body_skin_delta_f: float = 5.0  # body_skin - room >= this

    # Wake-cool trigger
    wake_cool_elapsed_min: int = 240

    # BedJet warm window (minutes after BedJet activation)
    bedjet_warm_window_min: int = 30

    # Right proactive hot threshold
    right_proactive_hot_f: float = 84.0

    # Body FB gains (§8 table)
    body_fb_kp_cold_left: float = 1.25
    body_fb_kp_hot_right: float = 0.50
    body_fb_max_delta_left: int = 5
    body_fb_max_delta_right: int = 4

    # Room reference
    room_blower_reference_f: float = 72.0

    # Cold-room comp caps
    cold_room_comp_cap_left: int = -3
    cold_room_comp_cap_right: int = -5

    # Movement proxy gains
    movement_kproxy_left: float = 1.0
    movement_kproxy_right: float = 2.0

    # Residual LCB
    residual_lcb_k: float = 1.0

    # Max divergence steps per regime
    max_divergence_steps: dict = field(default_factory=lambda: {
        "NORMAL_COOL": 3,
        "COLD_ROOM_COMP": 4,
        "WAKE_COOL": 2,
        "PRE_BED": 1,
        "INITIAL_COOL": 1,
        "BEDJET_WARM": 1,
        "SAFETY_YIELD": 1,
        "OVERRIDE": 1,
        "UNOCCUPIED": 1,
    })

    # Cycle baselines (v5.2 fallback per §3)
    cycle_baseline_left: list = field(
        default_factory=lambda: [-10, -10, -7, -5, -5, -6]
    )
    cycle_baseline_right: list = field(
        default_factory=lambda: [-8, -7, -6, -5, -5, -5]
    )


DEFAULT_CONFIG = RegimeConfig()


def classify(
    zone: str,
    *,
    elapsed_min: float,
    mins_since_onset: Optional[float],
    post_bedjet_min: Optional[float],
    sleep_stage: Optional[str],
    bed_occupied: Optional[bool],
    room_f: Optional[float],
    body_skin_f: Optional[float],
    body_hot_f: Optional[float],
    body_avg_f: Optional[float],
    override_freeze_active: bool,
    right_rail_engaged: bool,
    pre_sleep_active: bool,
    three_level_off: bool,
    movement_density_15m: Optional[float] = None,
    config: Optional[RegimeConfig] = None,
) -> dict:
    """Classify the current tick into a regime.

    Priority order (first match wins) per recommendation.md §6:
      UNOCCUPIED > PRE_BED > INITIAL_COOL > BEDJET_WARM > SAFETY_YIELD >
      OVERRIDE > COLD_ROOM_COMP > WAKE_COOL > NORMAL_COOL

    Args:
        zone: "left" or "right"
        elapsed_min: minutes since sleep session start
        mins_since_onset: minutes since bed onset (per-zone occupancy)
        post_bedjet_min: minutes since BedJet deactivated (right only)
        sleep_stage: current stage label or None
        bed_occupied: True/False/None (fail-closed: None → UNOCCUPIED)
        room_f: bedroom temperature in °F
        body_skin_f: body skin temperature (body_left_f)
        body_hot_f: max-channel body temp (max(body_left, body_center_post60) for right)
        body_avg_f: average body temperature
        override_freeze_active: True if user override freeze is active
        right_rail_engaged: True if right_overheat_safety rail is engaged
        pre_sleep_active: True if in pre-sleep state (inbed/awake before session)
        three_level_off: True if 3-level mode is confirmed OFF
        movement_density_15m: optional movement density over last 15 min
        config: RegimeConfig instance (defaults to DEFAULT_CONFIG)

    Returns:
        dict with keys: regime, reason, base_setting, advice
    """
    if config is None:
        config = DEFAULT_CONFIG

    # --- UNOCCUPIED: fail-closed per v5.2 patch ---
    if bed_occupied is False or bed_occupied is None:
        return _result("UNOCCUPIED", "bed not occupied or unknown", 0, {})

    # --- PRE_BED ---
    if pre_sleep_active:
        return _result("PRE_BED", "pre-sleep phase active", -10, {})

    # --- INITIAL_COOL ---
    if mins_since_onset is not None:
        window = config.initial_bed_cooling_min
        # Shrink window if room is cold (red-comfort §6.3 #6)
        if room_f is not None and room_f < 66.0:
            window = config.initial_bed_cold_room_min
        if 0 <= mins_since_onset <= window:
            return _result(
                "INITIAL_COOL",
                f"within {window}-min initial cooling window",
                -10,
                {"window_min": window},
            )

    # --- BEDJET_WARM (right zone only) ---
    if zone == "right" and post_bedjet_min is not None:
        if 0 < post_bedjet_min < config.bedjet_warm_window_min:
            return _result(
                "BEDJET_WARM",
                f"BedJet warm window ({post_bedjet_min:.0f} min post-activation)",
                -5,
                {"post_bedjet_min": post_bedjet_min},
            )

    # --- SAFETY_YIELD (right zone only) ---
    if zone == "right" and right_rail_engaged:
        return _result(
            "SAFETY_YIELD",
            "right_overheat_safety rail engaged",
            -10,
            {"rail_engaged": True},
        )

    # --- OVERRIDE ---
    if override_freeze_active:
        return _result(
            "OVERRIDE",
            "user override freeze active",
            None,  # controller respects user's last setting
            {"freeze_active": True},
        )

    # --- COLD_ROOM_COMP ---
    if (
        room_f is not None
        and body_skin_f is not None
        and room_f < config.cold_room_threshold_f
        and (body_skin_f - room_f) >= config.cold_room_body_skin_delta_f
    ):
        base = _cold_room_base(zone, config, body_skin_f, room_f)
        return _result(
            "COLD_ROOM_COMP",
            f"cold room ({room_f:.1f}°F) with body-room delta "
            f"{body_skin_f - room_f:.1f}°F",
            base,
            {"room_f": room_f, "body_skin_f": body_skin_f},
        )

    # --- WAKE_COOL ---
    if (
        sleep_stage in ("awake", "wake")
        and elapsed_min > config.wake_cool_elapsed_min
    ):
        base = _wake_cool_base(zone, config, body_hot_f)
        return _result(
            "WAKE_COOL",
            f"wake stage after {elapsed_min:.0f} min",
            base,
            {"body_hot_f": body_hot_f},
        )

    # --- NORMAL_COOL (default) ---
    base = _normal_cool_base(zone, config, elapsed_min)
    return _result(
        "NORMAL_COOL",
        "default regime",
        base,
        {"cycle_index": _cycle_index(elapsed_min)},
    )


def divergence_check(
    plan_setting: int,
    plant_predicted_setpoint_f: float,
    actual_setpoint_f: float,
) -> float:
    """Return absolute step difference for the divergence guard.

    Used by the safety actuator to detect when the plant model diverges
    from the firmware's actual behavior (recommendation.md §6 step 7).

    Args:
        plan_setting: the controller's planned L_active setting
        plant_predicted_setpoint_f: FirmwarePlant predicted setpoint in °F
        actual_setpoint_f: observed firmware setpoint in °F

    Returns:
        Absolute difference in °F between predicted and actual setpoints.
    """
    return abs(plant_predicted_setpoint_f - actual_setpoint_f)


# ─── Internal helpers ─────────────────────────────────────────────────

def _result(regime: str, reason: str, base_setting: Optional[int], advice: dict) -> dict:
    return {
        "regime": regime,
        "reason": reason,
        "base_setting": base_setting,
        "advice": advice,
    }


def _cycle_index(elapsed_min: float) -> int:
    """0-based cycle index (90-min cycles)."""
    if elapsed_min < 0:
        return 0
    return min(int(elapsed_min // 90), 5)


def _cold_room_base(zone: str, config: RegimeConfig, body_skin_f: float, room_f: float) -> int:
    """Compute COLD_ROOM_COMP base setting per §3 table.

    Left: capped warmer than v5.2 cycle baseline by +2 to +3 if body_skin
    within 1°F of room (cold-cluster). Cap at config.cold_room_comp_cap_left.
    Right: same direction but capped at +1.
    """
    if zone == "left":
        # Body proximity to room indicates cold-cluster
        body_room_gap = body_skin_f - room_f
        if body_room_gap <= 6.0:  # within ~1°F above threshold
            warm_boost = 3
        else:
            warm_boost = 2
        # Base from cycle baseline mid-night (-7 typical), add warm boost
        base = -7 + warm_boost  # yields -5 or -4
        return max(-10, min(config.cold_room_comp_cap_left, base))
    else:
        # Right zone: same direction, capped at +1 from baseline
        base = -6 + 1  # right mid-night baseline -6, +1 cap
        return max(-10, min(config.cold_room_comp_cap_right, base))


def _wake_cool_base(zone: str, config: RegimeConfig, body_hot_f: Optional[float]) -> int:
    """Compute WAKE_COOL base setting per §3 table and default #6.

    Left: -2 (slight cool-bias for wake comfort).
    Right: cool-bias if body_hot > 84°F else 0.
    """
    if zone == "left":
        return -2
    else:
        # Right: cool-bias only when body_hot exceeds threshold
        if body_hot_f is not None and body_hot_f > config.right_proactive_hot_f:
            return -2
        return 0


def _normal_cool_base(zone: str, config: RegimeConfig, elapsed_min: float) -> int:
    """Compute NORMAL_COOL base from zone-specific cycle baseline.

    Uses v5.2 CYCLE_SETTINGS for left, adjusted baseline for right.
    """
    idx = _cycle_index(elapsed_min)
    if zone == "left":
        return config.cycle_baseline_left[idx]
    else:
        return config.cycle_baseline_right[idx]
