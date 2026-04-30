"""
Hierarchical policy for the PerfectlySnug controller.

Three-layer design (see ML_CONTROLLER_PRD.md and session notes for rationale):

  Layer 1 — Hard safety rails (this module's `apply_rails`)
      Code-reviewed predicates encoding physics/biology that must not be
      overridden by data. Examples: body sensor reads at the user's p99
      occupied tail → user is overheating, force max cool; room ≥77°F →
      apartment is hot, max cool always. Auto-tuning never touches the
      structure of these rails, but the body-temp thresholds are
      **per-zone calibrated** from each sleeper's own distribution
      because the two sleepers run very different baseline body-sensor
      temperatures (one user's p95 is 84°F, the other's is 94°F).

  Layer 2 — Fitted smart baseline (ml.features.smart_baseline)
      Per-cycle baselines + room-band adjustments + heat-on slope, fit
      from logged override events via tools/fit_baselines.py.

  Layer 3 — Online residual (future)
      LightGBM correction, deferred until ~150+ override events.
"""
from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Optional

from ml.features import smart_baseline


# ── Body-temp safety thresholds (absolute, shared across zones) ────────
#
# The body sensor reads the topper-surface temperature where the user lies,
# which equilibrates to skin/microclimate temperature under bedding. Human
# in-bed skin temp is ~89-92°F when comfortable; sustained readings above
# that indicate overheating regardless of which sleeper is on that side.
#
# We deliberately do NOT calibrate these per-zone from observed percentiles:
# that would encode an under-cooled sleeper's chronic discomfort as their
# "normal" and the rail would never fire. Comfort is a physical property,
# not a personal one. (Per-zone *cycle baselines* still exist in layer 2
# because preferred *operating* setting is personal; safety extremes are not.)
#
# Diagnostic ground truth (n=2181 right-zone occupied readings, 14 nights):
#   - Readings ≥95°F: 33, all occupied, avg setting only -5.3 (controller
#     was failing to respond — these are real overheat events).
#   - User confirmed: wife reported feeling hot during 90°F+ stretches.
# So 90°F is treated as overheat for any user.

BODY_OVERHEAT_HARD_F = 90.0    # >= this → unambiguous overheating, force -10
BODY_OVERHEAT_SOFT_F = 87.0    # >= this → ensure at least strong cooling (≤-7)
BODY_TOO_COLD_F      = 76.0    # <= this during sleep → ease off cooling
                               # (under-bedding skin temp shouldn't drop here)
BODY_COLD_GRACE_MIN  = 30.0    # ignore cold readings before this elapsed_min
                               # (entry transient: skin still cool from outside)

# Room sensor (ambient bedroom, °F) — same for both zones since ambient is shared.
ROOM_HOT_HARD_F      = 77.0
ROOM_TOO_COLD_F      = 60.0

# Setting clamp (matches L1 dynamic range of the topper).
SETTING_MIN = -10
SETTING_MAX = 0


def _is_finite(x: Optional[float]) -> bool:
    return x is not None and not (isinstance(x, float) and math.isnan(x))


# ── Layer 1 rails (each a named, individually testable predicate) ──────

def rail_body_overheat_hard(body_f: Optional[float]) -> Optional[int]:
    """Body sensor ≥90°F → overheating regardless of cycle/room. Force -10.

    90°F is above the comfortable in-bed skin/microclimate range (~89-92°F
    upper). Sustained at this level the user's autonomic system increases
    sweating and arousals. Max cooling is the only correct response.
    """
    if not _is_finite(body_f):
        return None
    if body_f >= BODY_OVERHEAT_HARD_F:
        return SETTING_MIN
    return None


def rail_body_overheat_soft(body_f: Optional[float],
                            current_smart: int) -> Optional[int]:
    """Body sensor 87-90°F → ensure at least strong cooling (≤-7)."""
    if not _is_finite(body_f):
        return None
    if body_f >= BODY_OVERHEAT_SOFT_F:
        return min(current_smart, -7)
    return None


def rail_body_too_cold(body_f: Optional[float],
                       elapsed_min: Optional[float],
                       current_smart: int) -> Optional[int]:
    """Body sensor ≤76°F after entry settling → cap cooling at -3.

    Under-bedding skin temp at 76°F means the user is genuinely cold,
    not just newly-arrived (grace period filters the entry transient).
    """
    if not _is_finite(body_f):
        return None
    if not _is_finite(elapsed_min) or elapsed_min < BODY_COLD_GRACE_MIN:
        return None
    if body_f <= BODY_TOO_COLD_F:
        return max(current_smart, -3)
    return None


def rail_room_hot_hard(room_temp_f: Optional[float]) -> Optional[int]:
    """Room ambient ≥77°F → max cool always (user-stated intuition)."""
    if not _is_finite(room_temp_f):
        return None
    if room_temp_f >= ROOM_HOT_HARD_F:
        return SETTING_MIN
    return None


def rail_room_too_cold(room_temp_f: Optional[float],
                       current_smart: int) -> Optional[int]:
    """Room ambient ≤60°F → broken HVAC night, cap cooling at -3."""
    if not _is_finite(room_temp_f):
        return None
    if room_temp_f <= ROOM_TOO_COLD_F:
        return max(current_smart, -3)
    return None


# ── Composition ────────────────────────────────────────────────────────

def apply_rails(*, smart: int, room_temp_f: Optional[float],
                body_f: Optional[float],
                elapsed_min: Optional[float]) -> tuple[int, Optional[str]]:
    """Apply layer-1 rails in priority order; return (setting, rail_name|None)."""
    for name, fn in (("body_overheat_hard", lambda: rail_body_overheat_hard(body_f)),
                     ("room_hot_hard",      lambda: rail_room_hot_hard(room_temp_f))):
        out = fn()
        if out is not None:
            return _clamp(out), name

    setting = smart
    triggered = None
    for name, fn in (
        ("body_overheat_soft",
         lambda: rail_body_overheat_soft(body_f, setting)),
        ("body_too_cold",
         lambda: rail_body_too_cold(body_f, elapsed_min, setting)),
        ("room_too_cold",
         lambda: rail_room_too_cold(room_temp_f, setting)),
    ):
        out = fn()
        if out is not None and out != setting:
            setting = out
            triggered = name
    return _clamp(setting), triggered


def _clamp(setting: int) -> int:
    return max(SETTING_MIN, min(SETTING_MAX, int(setting)))


def controller_decision(*, zone: str, elapsed_min: float,
                        room_temp_f: Optional[float],
                        body_f: Optional[float] = None
                        ) -> tuple[int, Optional[str]]:
    """Compose layer 1 (rails) on top of layer 2 (fitted smart_baseline).

    `zone` is "left" or "right"; reserved for future per-zone smart_baseline
    fitting. Body-temp rails are absolute and zone-independent (see
    BODY_OVERHEAT_HARD_F docstring).
    """
    smart = smart_baseline(elapsed_min, room_temp_f)
    return apply_rails(smart=smart, room_temp_f=room_temp_f,
                       body_f=body_f, elapsed_min=elapsed_min)

