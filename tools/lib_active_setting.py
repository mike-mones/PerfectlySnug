"""L_active helper — pick the user dial (L1/L2/L3) that is actually live.

Background
----------
The PerfectlySnug firmware can run a 3-stage program when
``switch.smart_topper_<side>_3_level_mode`` is ON:

    phase = start  (length T1 minutes)  →  L1  (bedtime_temperature) active
    phase = sleep                       →  L2  (sleep_temperature)   active
    phase = wake   (length T3 minutes)  →  L3  (wake_temperature)    active

``sensor.smart_topper_<side>_run_progress`` is an integer 0..100, the percent
through the *whole* scheduled run (T1 + sleep + T3). The firmware switches
dials by run_progress, so the thresholds are::

    p1 = (T1 / total) * 100               # end of L1, start of L2
    p2 = ((total - T3) / total) * 100     # end of L2, start of L3

When 3-level mode is OFF, L1 is active for the entire run and L2/L3 are
ignored. See docs/findings/2026-05-01_data_audit_labels.md §5.

Entity reference
----------------
Per-side entity names (replace ``<side>`` with ``left_side`` or
``right_side``):

    L1: number.smart_topper_<side>_bedtime_temperature        (SETTING_L1=0)
    L2: number.smart_topper_<side>_sleep_temperature          (SETTING_L2=1)
    L3: number.smart_topper_<side>_wake_temperature           (SETTING_L3=2)
    T1: number.smart_topper_<side>_start_length_minutes       (SETTING_T1=12)
    T3: number.smart_topper_<side>_wake_length_minutes        (SETTING_T3=13)
    3-level switch: switch.smart_topper_<side>_3_level_mode
    run_progress:   sensor.smart_topper_<side>_run_progress   (SETTING=23)

All L-numbers are stored display-space (-10..+10 integer). T1/T3 are
minutes. run_progress is 0..100.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass
class ActiveSetting:
    """Result of an L_active lookup."""

    phase: str           # "start" | "sleep" | "wake" | "off"
    dial: str            # "L1" | "L2" | "L3"
    value: Optional[float]  # the L-value in display space (-10..+10), or None


def active_setting(
    *,
    run_progress: Optional[float],
    t1_min: Optional[float],
    t3_min: Optional[float],
    total_min: Optional[float],
    l1: Optional[float],
    l2: Optional[float],
    l3: Optional[float],
    three_level_mode: bool,
) -> ActiveSetting:
    """Return which dial (L1/L2/L3) is currently driving the topper.

    Parameters
    ----------
    run_progress : 0..100 percent through the run (sensor reading).
    t1_min, t3_min : start / wake phase lengths in minutes.
    total_min : full scheduled run length in minutes (T1 + sleep + T3).
        If unknown, pass ``None`` and the function falls back to assuming
        sleep is at least 1 minute and uses ``t1 + 1 + t3`` (degenerate
        but bounded). For best accuracy, derive ``total_min`` from the
        schedule (start time, end time) or from the firmware run length.
    l1, l2, l3 : current dial values (display space, -10..+10).
    three_level_mode : whether the firmware is running the 3-stage program.

    Returns
    -------
    ActiveSetting with .phase, .dial, .value.
    """
    if not three_level_mode:
        return ActiveSetting(phase="start", dial="L1", value=l1)

    if run_progress is None:
        # Assume start-of-run if unknown.
        return ActiveSetting(phase="start", dial="L1", value=l1)

    t1 = max(0.0, float(t1_min or 0))
    t3 = max(0.0, float(t3_min or 0))
    total = float(total_min) if total_min else (t1 + 1.0 + t3)
    if total <= 0:
        return ActiveSetting(phase="start", dial="L1", value=l1)

    p1 = (t1 / total) * 100.0
    p2 = ((total - t3) / total) * 100.0
    p = float(run_progress)

    if p < p1:
        return ActiveSetting(phase="start", dial="L1", value=l1)
    if p < p2:
        return ActiveSetting(phase="sleep", dial="L2", value=l2)
    return ActiveSetting(phase="wake", dial="L3", value=l3)


def active_setting_from_row(row: dict, side: str = "right_side",
                            total_min: Optional[float] = None) -> ActiveSetting:
    """Convenience: pull values from a dict keyed by HA entity_id.

    ``row`` should contain string→float values. Missing keys → None.
    Pass ``total_min`` if known (e.g. computed from schedule).
    """
    def f(key: str) -> Optional[float]:
        v = row.get(key)
        if v is None or v == "" or v == "unknown" or v == "unavailable":
            return None
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    def b(key: str) -> bool:
        v = row.get(key)
        if isinstance(v, bool):
            return v
        if isinstance(v, str):
            return v.lower() in ("on", "true", "1")
        return bool(v)

    base = f"smart_topper_{side}"
    return active_setting(
        run_progress=f(f"sensor.{base}_run_progress"),
        t1_min=f(f"number.{base}_start_length_minutes"),
        t3_min=f(f"number.{base}_wake_length_minutes"),
        total_min=total_min,
        l1=f(f"number.{base}_bedtime_temperature"),
        l2=f(f"number.{base}_sleep_temperature"),
        l3=f(f"number.{base}_wake_temperature"),
        three_level_mode=b(f"switch.{base}_3_level_mode"),
    )


__all__ = ["ActiveSetting", "active_setting", "active_setting_from_row"]
