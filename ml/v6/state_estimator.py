"""PerfectlySnug — Latent comfort-state estimator (v6).

Pure-Python, no AppDaemon dependency, no PG dependency. The live controller
glue (P3b) and the offline replay tool (`tools/replay_state.py`) both call
into this module.

Spec: docs/proposals/2026-05-04_state_estimation.md (§3.2 rule cascade).

Seven states (per zone):
    OFF_BED, AWAKE_IN_BED, SETTLING, STABLE_SLEEP,
    RESTLESS, WAKE_TRANSITION, DISTURBANCE

Plus two degraded fallback states for §6:
    OCCUPIED_AWAKE, OCCUPIED_QUIET

Each `estimate_state` call returns (state, confidence, trigger) where:
    state      ∈ STATE_NAMES
    confidence ∈ [0.0, 1.0]
    trigger    short string explaining which rule fired (for logs)

Time-of-night appears only as a weak ≥5h necessary-condition prior on
WAKE_TRANSITION (Rule 5), and as a tiebreaker in the degraded fallback. No
cycle index, no CYCLE_SETTINGS lookup, no night_progress feature.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

# ── State labels ─────────────────────────────────────────────────────
STATE_OFF_BED = "OFF_BED"
STATE_AWAKE_IN_BED = "AWAKE_IN_BED"
STATE_SETTLING = "SETTLING"
STATE_STABLE_SLEEP = "STABLE_SLEEP"
STATE_RESTLESS = "RESTLESS"
STATE_WAKE_TRANSITION = "WAKE_TRANSITION"
STATE_DISTURBANCE = "DISTURBANCE"

# Degraded-mode collapsed labels (§6).
STATE_OCCUPIED_AWAKE = "OCCUPIED_AWAKE"
STATE_OCCUPIED_QUIET = "OCCUPIED_QUIET"

STATE_NAMES = (
    STATE_OFF_BED, STATE_AWAKE_IN_BED, STATE_SETTLING, STATE_STABLE_SLEEP,
    STATE_RESTLESS, STATE_WAKE_TRANSITION, STATE_DISTURBANCE,
    STATE_OCCUPIED_AWAKE, STATE_OCCUPIED_QUIET,
)

# ── Tunable constants (spec §3.3) ────────────────────────────────────
OFF_BED_DEBOUNCE_S = 120
AWAKE_RECENT_S = 600
DISTURBANCE_DELTA = 8.0
BODY_TREND_FLAT_F_PER_15M = 0.30
BODY_TREND_RISE_F_PER_15M = 0.30
LATE_SESSION_S = 5 * 3600

BODY_VALID_DELTA_F = 6.0
BODY_VALID_WARMUP_S = 600

# Movement-degraded fallback constants (§5.2 / §6.1).
DEGRADED_LATE_S = 90 * 60
DEGRADED_BODY_RAMPING_F_PER_15M = 0.50

# Confidence cap during any degraded path (§5.2 / §6).
DEGRADED_CONFIDENCE_CAP = 0.5


# ── Inputs ───────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Percentiles:
    """Per-user rolling percentiles of movement features (spec §2.6)."""
    movement_p25: float = 0.05      # bootstrap defaults from §3.4
    movement_p75: float = 0.20
    movement_p90: float = 0.50
    movement_var_p75: float = 0.10
    movement_var_p90: float = 0.30


@dataclass(frozen=True)
class Features:
    """Per-tick feature snapshot (spec §2)."""
    # Movement (None = unavailable / stale > 90s)
    movement_rms_5min: Optional[float] = None
    movement_rms_15min: Optional[float] = None
    movement_variance_15min: Optional[float] = None
    movement_max_delta_60s: Optional[float] = None

    # Presence
    presence_binary: Optional[bool] = None      # None ⇒ unknown / stale
    seconds_since_presence_change: Optional[float] = None

    # Body
    body_avg_f: Optional[float] = None
    body_trend_15min: Optional[float] = None    # °F per 15 min slope
    # body_sensor_validity is computed in __post_init__ if room_temp_f is set,
    # but callers may also supply it directly.
    body_sensor_validity_override: Optional[bool] = None

    # Room
    room_temp_f: Optional[float] = None

    # Control history
    setting_recent_change_30min: int = 0

    @property
    def movement_available(self) -> bool:
        return (self.movement_rms_5min is not None
                and self.movement_rms_15min is not None)

    @property
    def body_sensor_validity(self) -> bool:
        if self.body_sensor_validity_override is not None:
            return self.body_sensor_validity_override
        if (self.body_avg_f is None or self.room_temp_f is None
                or self.seconds_since_presence_change is None):
            return False
        delta_ok = (self.body_avg_f - self.room_temp_f) >= BODY_VALID_DELTA_F
        warmup_ok = self.seconds_since_presence_change >= BODY_VALID_WARMUP_S
        return bool(delta_ok and warmup_ok)


# ── Outputs ──────────────────────────────────────────────────────────
@dataclass(frozen=True)
class LatentState:
    state: str
    confidence: float
    trigger: str
    degraded: Optional[str] = None  # None | 'movement' | 'body_validity' | 'both'

    def asdict(self) -> dict:
        return {
            "state": self.state,
            "confidence": round(self.confidence, 4),
            "trigger": self.trigger,
            "degraded": self.degraded,
        }


# ── Inference ────────────────────────────────────────────────────────
def estimate_state(
    features: Features,
    prev_state: Optional[str] = None,
    percentiles: Percentiles = Percentiles(),
) -> LatentState:
    """Pure state estimator. See spec §3.2 for the rule cascade."""
    f = features
    p = percentiles
    body_valid = f.body_sensor_validity

    # Determine degradation modes BEFORE any rule fires (spec §6.3).
    movement_degraded = not f.movement_available
    # body_validity_degraded only matters when occupied (presence True);
    # otherwise OFF_BED owns the call.
    body_degraded = (f.presence_binary is True and not body_valid)

    if movement_degraded and body_degraded:
        return LatentState(
            state=STATE_OCCUPIED_AWAKE, confidence=0.3,
            trigger="degraded:both", degraded="both")

    if movement_degraded:
        s = _degraded_movement(f, prev_state)
        return LatentState(state=s.state, confidence=s.confidence,
                           trigger=s.trigger, degraded="movement")

    # Rule 1 — OFF_BED (presence false; subsumes movement-degraded above).
    if f.presence_binary is False or f.presence_binary is None:
        # presence None (unknown) is fail-closed to OFF_BED per §2.2 staleness rule.
        if f.presence_binary is None:
            return LatentState(state=STATE_OFF_BED, confidence=0.5,
                               trigger="presence_unknown_fail_closed")
        if (f.seconds_since_presence_change is not None
                and f.seconds_since_presence_change >= OFF_BED_DEBOUNCE_S):
            return LatentState(state=STATE_OFF_BED, confidence=1.0,
                               trigger="presence_false_debounced")
        return LatentState(state=STATE_OFF_BED, confidence=0.7,
                           trigger="presence_false_recent_transition")

    # From here: presence_binary == True and movement features available.

    # Rule 2 — AWAKE_IN_BED (recent entry OR sustained high movement).
    recently_arrived = (f.seconds_since_presence_change is not None
                        and f.seconds_since_presence_change < AWAKE_RECENT_S)
    movement_high = (f.movement_rms_5min is not None
                     and f.movement_rms_5min > p.movement_p75)
    if recently_arrived or movement_high:
        conf = 0.9 if body_valid else 0.6
        trigger = ("awake_recent_arrival" if recently_arrived
                   else "awake_movement_high")
        deg = "body_validity" if body_degraded else None
        return LatentState(state=STATE_AWAKE_IN_BED, confidence=conf,
                           trigger=trigger, degraded=deg)

    # Rule 3 — DISTURBANCE (single-tick max-delta spike, no presence change,
    # variance not elevated, only valid out of STABLE_SLEEP / SETTLING).
    if (f.movement_max_delta_60s is not None
            and f.movement_max_delta_60s > DISTURBANCE_DELTA
            and f.movement_rms_15min is not None
            and f.movement_rms_15min < p.movement_p75
            and prev_state in (STATE_STABLE_SLEEP, STATE_SETTLING)):
        return LatentState(state=STATE_DISTURBANCE, confidence=0.5,
                           trigger=f"max_delta({f.movement_max_delta_60s:.1f}>{DISTURBANCE_DELTA})")

    # Rule 4 — RESTLESS (variance spike inside an otherwise stable period).
    if (prev_state == STATE_STABLE_SLEEP
            and f.movement_variance_15min is not None
            and f.movement_variance_15min > p.movement_var_p90
            and f.movement_rms_15min is not None
            and f.movement_rms_15min > p.movement_p75):
        return LatentState(state=STATE_RESTLESS, confidence=0.7,
                           trigger=f"variance({f.movement_variance_15min:.2f}>p90 {p.movement_var_p90:.2f})")

    # Rule 5 — WAKE_TRANSITION (rising movement variance + rising body trend,
    # late session).  Body-trend-dependent rules are SUPPRESSED when body is
    # invalid (§6.2).
    if (prev_state == STATE_STABLE_SLEEP
            and body_valid
            and f.movement_variance_15min is not None
            and f.movement_variance_15min > p.movement_var_p75
            and f.body_trend_15min is not None
            and f.body_trend_15min > BODY_TREND_RISE_F_PER_15M
            and f.seconds_since_presence_change is not None
            and f.seconds_since_presence_change > LATE_SESSION_S):
        return LatentState(state=STATE_WAKE_TRANSITION, confidence=0.6,
                           trigger=(f"wake_late+var+body_trend"
                                    f"({f.body_trend_15min:.2f})"))

    # Rule 6 — STABLE_SLEEP (low movement, body in flat trend, body valid).
    if (body_valid
            and f.movement_rms_15min is not None
            and f.movement_rms_15min < p.movement_p25
            and f.body_trend_15min is not None
            and abs(f.body_trend_15min) < BODY_TREND_FLAT_F_PER_15M):
        return LatentState(state=STATE_STABLE_SLEEP, confidence=0.9,
                           trigger=(f"stable_mrms15({f.movement_rms_15min:.3f}"
                                    f"<p25 {p.movement_p25:.3f})"))

    # Rule 7 — SETTLING (decreasing movement, body warming or flat).
    decreasing = (f.movement_rms_5min is not None and f.movement_rms_15min is not None
                  and f.movement_rms_5min < f.movement_rms_15min)
    body_ok_for_settling = ((f.body_trend_15min or 0.0) >= -0.10)
    if (decreasing
            and f.movement_rms_15min is not None
            and f.movement_rms_15min < p.movement_p75
            and body_ok_for_settling):
        conf = 0.7 if body_valid else 0.5
        deg = "body_validity" if body_degraded else None
        return LatentState(state=STATE_SETTLING, confidence=conf,
                           trigger="settling_movement_decreasing", degraded=deg)

    # Default — low-confidence SETTLING.
    deg = "body_validity" if body_degraded else None
    return LatentState(state=STATE_SETTLING, confidence=0.4,
                       trigger="default_low_confidence", degraded=deg)


def _degraded_movement(f: Features, prev_state: Optional[str]) -> LatentState:
    """Movement-stale fallback. Collapsed set: OFF_BED / OCCUPIED_AWAKE / OCCUPIED_QUIET."""
    if f.presence_binary is not True:
        return LatentState(state=STATE_OFF_BED, confidence=DEGRADED_CONFIDENCE_CAP,
                           trigger="degraded:movement+presence_false")
    if (f.seconds_since_presence_change is not None
            and f.seconds_since_presence_change < DEGRADED_LATE_S):
        return LatentState(state=STATE_OCCUPIED_AWAKE, confidence=DEGRADED_CONFIDENCE_CAP,
                           trigger="degraded:movement+early_session")
    if (f.body_trend_15min is not None
            and f.body_trend_15min > DEGRADED_BODY_RAMPING_F_PER_15M):
        return LatentState(state=STATE_OCCUPIED_AWAKE, confidence=DEGRADED_CONFIDENCE_CAP,
                           trigger="degraded:movement+body_ramping")
    return LatentState(state=STATE_OCCUPIED_QUIET, confidence=DEGRADED_CONFIDENCE_CAP,
                       trigger="degraded:movement+late_quiet")


# ── Replay glue (consumed by tools/replay_state.py) ───────────────────
def replay_iter(rows, percentiles: Percentiles = Percentiles()):
    """Iterate a stream of Features dicts (chronological), yielding LatentState.

    `rows` is an iterable of (ts, Features) — the caller is responsible for
    feature construction from PG. We carry `prev_state` through the loop.
    """
    prev_state: Optional[str] = None
    for ts, features in rows:
        latent = estimate_state(features, prev_state=prev_state,
                                percentiles=percentiles)
        # DISTURBANCE doesn't update prev_state per §1.1 (transient).
        if latent.state != STATE_DISTURBANCE:
            prev_state = latent.state
        yield ts, features, latent
