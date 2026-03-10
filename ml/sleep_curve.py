"""
Science-based sleep temperature curve, seeded with personal preferences.

Sleep thermophysiology (well-established):
  - Sleep onset: Core body temp drops 1-2°F. Bed cooling accelerates this.
  - Deep sleep (N3, 30-120 min): Temp at nadir. Moderate cooling sufficient.
  - REM cycles (90-min intervals): Thermoregulation impaired. Overcooling
    causes awakening. REM duration increases toward morning.
  - Pre-wake: Core temp rises naturally. Gentle warming aids wake.

The SHAPE of the ideal curve is known. What varies per person:
  1. Baseline offset (warm vs cold sleeper)
  2. Amplitude (how aggressively to cool/warm)
  3. Phase timing (when sleep stages occur)
  4. Thermal response (how body reacts to setting changes)

This module maps the science to Perfectly Snug's 3-level (L1/L2/L3) system.

References:
  - Kräuchi K. (2007) The thermophysiological cascade leading to sleep initiation
  - Okamoto-Mizuno K, Mizuno K. (2012) Effects of thermal environment on sleep
  - Harding EC et al. (2019) The Temperature Dependence of Sleep
"""

from dataclasses import dataclass, field
from datetime import datetime, time


# ── Sleeper Profile ──────────────────────────────────────────────────────

@dataclass
class SleeperProfile:
    """Personal thermal preference profile."""

    # Subjective warmth: "hot", "warm", "neutral", "cool", "cold"
    warmth: str = "warm"

    # Preferred bedtime setting on -10 to +10 scale
    preferred_bedtime: int = -7

    # Does the user wake up cold mid-night? (L2 needs to be warmer)
    wakes_cold_midnight: bool = True

    # Typical bedtime and wake time (24h, local time)
    bedtime_hour: int = 23      # 11 PM
    bedtime_minute: int = 0
    wake_hour: int = 7          # 7 AM
    wake_minute: int = 0

    # L1/L3 durations in minutes (from topper schedule)
    l1_duration_min: int = 60   # Bedtime phase
    l3_duration_min: int = 30   # Wake phase

    # Foot warmer preference (0-3)
    foot_warmer: int = 0


# ── Science-Based Curve Parameters ──────────────────────────────────────

# Temperature drop from bedtime (L1) to mid-sleep (L2), on the -10 to +10 scale.
# Based on sleep science: mid-sleep needs LESS cooling than sleep onset.
# Positive = warmer direction (less cooling).

WARMTH_L2_OFFSET = {
    # (warmth_type, wakes_cold): L2 offset from L1 (positive = warmer)
    ("hot", False):   3,    # Hot sleeper, no cold waking → moderate warmup
    ("hot", True):    4,    # Hot sleeper + cold waking → bigger warmup
    ("warm", False):  3,    # Warm sleeper → moderate warmup
    ("warm", True):   4,    # Warm + cold waking → bigger warmup
    ("neutral", False): 2,  # Neutral → mild warmup
    ("neutral", True):  3,  # Neutral + cold waking → moderate warmup
    ("cool", False):  2,    # Cool sleeper → small warmup
    ("cool", True):   3,    # Cool + cold waking → moderate warmup
    ("cold", False):  1,    # Cold sleeper → barely warm up
    ("cold", True):   2,    # Cold + cold waking → small warmup
}

WARMTH_L3_OFFSET = {
    # L3 offset from L2 (positive = warmer).
    # Pre-wake warming should be gentle — body is already warming naturally.
    ("hot", False):   1,
    ("hot", True):    2,
    ("warm", False):  2,
    ("warm", True):   2,
    ("neutral", False): 2,
    ("neutral", True):  3,
    ("cool", False):  3,
    ("cool", True):   3,
    ("cold", False):  3,
    ("cold", True):   4,
}


# ── Curve Computation ───────────────────────────────────────────────────

@dataclass
class SleepCurveRecommendation:
    """Recommended L1/L2/L3 settings and rationale."""
    l1: int                     # Bedtime temperature (-10 to +10)
    l2: int                     # Sleep temperature (-10 to +10)
    l3: int                     # Wake temperature (-10 to +10)
    l1_duration_min: int        # Duration of L1 phase
    l3_duration_min: int        # Duration of L3 phase
    foot_warmer: int            # Foot warmer level (0-3)
    rationale: list[str] = field(default_factory=list)


def compute_curve(profile: SleeperProfile) -> SleepCurveRecommendation:
    """
    Compute recommended L1/L2/L3 from a sleeper profile.

    The core idea: L1 is the user's known preference. L2 and L3 are
    calculated from sleep science offsets based on their warmth type.
    """
    warmth_key = (profile.warmth, profile.wakes_cold_midnight)
    rationale = []

    # L1: user's stated preference
    l1 = profile.preferred_bedtime
    rationale.append(
        f"L1={l1}: Your preferred bedtime cooling. "
        f"Aggressive cooling at sleep onset helps core temp drop faster."
    )

    # L2: warm up for mid-sleep (deep sleep + REM)
    l2_offset = WARMTH_L2_OFFSET.get(warmth_key, 3)
    l2 = min(10, l1 + l2_offset)
    reason = "Body temp at nadir during deep sleep — less cooling needed."
    if profile.wakes_cold_midnight:
        reason += " Extra warmup because you wake up cold mid-night."
    rationale.append(f"L2={l2}: {reason}")

    # L3: gentle warming toward wake
    l3_offset = WARMTH_L3_OFFSET.get(warmth_key, 2)
    l3 = min(10, l2 + l3_offset)
    rationale.append(
        f"L3={l3}: Pre-wake warming. Core temp rises naturally — "
        f"mild warming aids wake without overheating."
    )

    # Duration recommendations
    # Sleep science: deep sleep peaks in first 90-120 min.
    # L1 should cover sleep onset + first deep sleep cycle.
    l1_dur = profile.l1_duration_min
    l3_dur = profile.l3_duration_min

    # For warm sleepers who wake cold, a longer L1 ensures deep cooling
    # before transition. 60-90 min is ideal (covers first sleep cycle).
    if profile.warmth in ("hot", "warm") and l1_dur < 60:
        l1_dur = 60
        rationale.append(
            f"L1 duration bumped to {l1_dur}min — warm sleepers need "
            f"full first sleep cycle under deep cooling."
        )

    # L3 (wake) should be 30-60 min, aligned with final sleep cycle
    if l3_dur < 30:
        l3_dur = 30
        rationale.append(f"L3 duration bumped to {l3_dur}min for gradual wake warming.")

    return SleepCurveRecommendation(
        l1=l1,
        l2=l2,
        l3=l3,
        l1_duration_min=l1_dur,
        l3_duration_min=l3_dur,
        foot_warmer=profile.foot_warmer,
        rationale=rationale,
    )


def compute_continuous_curve(
    profile: SleeperProfile,
    total_sleep_hours: float = 8.0,
    resolution_min: int = 15,
) -> list[tuple[int, int]]:
    """
    Generate a continuous temperature curve at `resolution_min` intervals.

    Returns list of (minutes_since_bedtime, recommended_setting) tuples.
    This shows the ideal curve shape — the 3-level system approximates it.
    """
    rec = compute_curve(profile)
    total_min = int(total_sleep_hours * 60)
    l2_start = rec.l1_duration_min
    l3_start = total_min - rec.l3_duration_min

    curve = []
    for t in range(0, total_min + 1, resolution_min):
        if t <= l2_start:
            # L1 phase — use L1 value
            setting = rec.l1
        elif t >= l3_start:
            # L3 phase — use L3 value
            setting = rec.l3
        else:
            # L2 phase — gradual linear warm from L2 start to L3 start
            # This represents the body's gradual warming through the night
            progress = (t - l2_start) / (l3_start - l2_start)
            setting = rec.l2 + progress * (rec.l3 - rec.l2)
            setting = round(setting)
        curve.append((t, max(-10, min(10, setting))))

    return curve


# ── Apple Health Integration (future) ───────────────────────────────────

def adjust_for_sleep_stage(
    base_setting: int,
    sleep_stage: str,
    warmth: str = "warm",
) -> int:
    """
    Adjust temperature based on real-time sleep stage from Apple Health.

    This is the future adaptive layer on top of the base curve.
    When sleep stage data arrives, we can make intra-night adjustments.
    """
    adjustments = {
        # During REM: thermoregulation impaired, avoid overcooling
        "rem": +2,
        # During deep/N3: body at nadir, moderate cooling fine
        "deep": 0,
        # During light/N1/N2: transitional, keep current
        "core": 0,
        # Awake during night: might be too hot or cold
        "awake": -1 if warmth in ("hot", "warm") else +1,
    }
    adj = adjustments.get(sleep_stage, 0)
    return max(-10, min(10, base_setting + adj))


# ── CLI ─────────────────────────────────────────────────────────────────

def print_recommendation(profile: SleeperProfile) -> None:
    """Print a human-readable recommendation."""
    rec = compute_curve(profile)
    curve = compute_continuous_curve(profile)

    print("=" * 60)
    print("SLEEP TEMPERATURE RECOMMENDATION")
    print("=" * 60)
    print(f"\nProfile: {profile.warmth} sleeper, "
          f"bedtime pref: {profile.preferred_bedtime}, "
          f"wakes cold: {profile.wakes_cold_midnight}")
    print(f"\n  L1 (Bedtime):  {rec.l1:+d}  for {rec.l1_duration_min} min")
    print(f"  L2 (Sleep):    {rec.l2:+d}  (bulk of night)")
    print(f"  L3 (Wake):     {rec.l3:+d}  for {rec.l3_duration_min} min")
    print(f"  Foot warmer:   {rec.foot_warmer}")

    print(f"\nCurrent → Recommended changes:")
    print(f"  L1: -9 → {rec.l1:+d}  ({rec.l1 - (-9):+d} change)")
    print(f"  L2: -6 → {rec.l2:+d}  ({rec.l2 - (-6):+d} change)")
    print(f"  L3: -5 → {rec.l3:+d}  ({rec.l3 - (-5):+d} change)")

    print(f"\nRationale:")
    for r in rec.rationale:
        print(f"  • {r}")

    print(f"\nIdeal continuous curve (15-min resolution):")
    print(f"  {'Time':>6s}  {'Setting':>7s}  {'Bar'}")
    print(f"  {'─'*6}  {'─'*7}  {'─'*30}")
    for t, s in curve:
        h = t // 60
        m = t % 60
        bar_pos = s + 10  # shift to 0-20 range
        bar = "█" * bar_pos + "░" * (20 - bar_pos)
        print(f"  +{h}:{m:02d}   {s:+3d}     {bar}")

    print()


if __name__ == "__main__":
    mike = SleeperProfile(
        warmth="warm",
        preferred_bedtime=-7,
        wakes_cold_midnight=True,
        bedtime_hour=23,
        wake_hour=7,
        l1_duration_min=60,
        l3_duration_min=30,
        foot_warmer=0,
    )
    print_recommendation(mike)
