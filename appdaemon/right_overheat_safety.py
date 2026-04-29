"""
Right-zone overheat safety rail (standalone AppDaemon app).

Purpose: the wife's right zone has NO automated controller (v5 is left-zone
only). Across 20 nights of logged data her body sensor has crossed 90°F in
four sustained stretches, peaking at 98.9°F across 80 minutes on 2026-04-24.
This app provides a *safety-only* response — it does not try to optimize
comfort, it just prevents sustained extreme overheating.

Behavior
--------
Every 60 seconds, while the right side of the bed is occupied:

  1. Read body_sensor_center on the right side.
  2. If the reading is ≥ OVERHEAT_HARD_F for OVERHEAT_HARD_STREAK consecutive
     polls AND the right-zone setpoint is not already at OR below the
     emergency value, snapshot the current setpoint and force the right-zone
     bedtime_temperature to RAIL_FORCE_SETTING (-10).
  3. Once engaged, stay engaged with hysteresis: release only when the body
     reading drops below OVERHEAT_RELEASE_F.
  4. On release, restore the snapshotted setpoint so we don't permanently
     override her manual choice.

Gated by `input_boolean.snug_right_overheat_rail_enabled`. Default ON
(unlike the left-zone rail which defaults OFF) because she has demonstrated
overheat events and zero historical readings below the engagement threshold
that would falsely trigger.

This app is intentionally narrow:
  - It has no notion of cycle baselines, room temperature compensation,
    or ML smart_baseline. The right zone has too few overrides to defend
    those choices for someone whose body trajectory is structurally different
    from the user's.
  - It never relaxes any setting cooler than -10. It only forces ≤ -10 in
    one direction (toward more cooling) and restores to the prior value on
    release.
  - Wrapped in broad try/except in the periodic callback so it can never
    break HA / AppDaemon if a sensor reads NaN or the API is temporarily
    unreachable.

Add to apps.yaml:
    right_overheat_safety:
      module: right_overheat_safety
      class: RightOverheatSafety
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Optional

import hassapi as hass

# ── Constants ────────────────────────────────────────────────────────────

OVERHEAT_HARD_F = 88.0       # body ≥ this for STREAK polls → engage
OVERHEAT_HARD_STREAK = 2     # 2 consecutive polls (~2 min)
OVERHEAT_RELEASE_F = 84.0    # release engagement when body drops below
RAIL_FORCE_SETTING = -10     # value forced on bedtime_temperature
POLL_INTERVAL_SEC = 60

E_RAIL_FLAG = "input_boolean.snug_right_overheat_rail_enabled"
E_BEDTIME_R = "number.smart_topper_right_side_bedtime_temperature"
E_BODY_CENTER_R = "sensor.smart_topper_right_side_body_sensor_center"
E_OCCUPIED_R = "binary_sensor.bed_presence_2bcab8_bed_occupied_right"

# State persistence (mirrors v5's pattern — primary state is in-memory)
_container = Path("/config/apps")
_host = Path("/addon_configs/a0d7b954_appdaemon/apps")
STATE_DIR = _container if _container.exists() else _host
STATE_FILE = STATE_DIR / "right_overheat_safety_state.json"


class RightOverheatSafety(hass.Hass):
    """Single-purpose AppDaemon app: prevent sustained right-zone overheat."""

    def initialize(self):
        self._state = {
            "engaged": False,
            "streak": 0,
            "snapshot_setting": None,    # what the setpoint was before we engaged
            "engaged_at": None,
            "released_at": None,
            "engage_count_session": 0,
        }
        self._load_state()

        self.run_every(self._tick, "now", POLL_INTERVAL_SEC)
        self.log("Right-zone overheat safety rail ready "
                 f"(engage ≥{OVERHEAT_HARD_F}°F x{OVERHEAT_HARD_STREAK}, "
                 f"release <{OVERHEAT_RELEASE_F}°F, force={RAIL_FORCE_SETTING})")

    # ── Helpers ──────────────────────────────────────────────────────────

    def _read_float(self, entity_id: str) -> Optional[float]:
        try:
            v = self.get_state(entity_id)
            if v in (None, "unknown", "unavailable", ""):
                return None
            return float(v)
        except (TypeError, ValueError):
            return None

    def _read_str(self, entity_id: str) -> Optional[str]:
        try:
            return self.get_state(entity_id)
        except Exception:  # pragma: no cover
            return None

    def _save_state(self) -> None:
        try:
            STATE_FILE.write_text(json.dumps(self._state, default=str))
        except Exception as e:  # pragma: no cover
            self.log(f"State save failed: {e}", level="WARNING")

    def _load_state(self) -> None:
        try:
            if STATE_FILE.exists():
                self._state.update(json.loads(STATE_FILE.read_text()))
        except Exception as e:  # pragma: no cover
            self.log(f"State load failed: {e}", level="WARNING")

    # ── Main tick ────────────────────────────────────────────────────────

    def _tick(self, kwargs):
        try:
            self._tick_inner()
        except Exception as e:  # never let this break HA
            self.log(f"Right-zone safety tick failed: {e}", level="ERROR")

    def _tick_inner(self) -> None:
        # Gate 1: rail enabled?
        if self._read_str(E_RAIL_FLAG) != "on":
            # If we're disabled mid-engagement, release immediately.
            if self._state["engaged"]:
                self._release(reason="rail_disabled")
            return

        # Gate 2: bed occupied? Don't fight the firmware when she's not in bed.
        if self._read_str(E_OCCUPIED_R) != "on":
            if self._state["engaged"]:
                self._release(reason="bed_unoccupied")
            return

        body = self._read_float(E_BODY_CENTER_R)
        if body is None:
            # Don't update streak on a missing read — neither engage nor release.
            return

        already_engaged = self._state["engaged"]
        if body >= OVERHEAT_HARD_F:
            self._state["streak"] = self._state.get("streak", 0) + 1
        elif body < (OVERHEAT_RELEASE_F if already_engaged else OVERHEAT_HARD_F):
            if already_engaged:
                self._release(reason=f"body_cooled_to_{body:.1f}")
            self._state["streak"] = 0

        if not self._state["engaged"] and self._state["streak"] >= OVERHEAT_HARD_STREAK:
            self._engage(body=body)

        self._save_state()

    # ── Actions ──────────────────────────────────────────────────────────

    def _engage(self, body: float) -> None:
        current = self._read_float(E_BEDTIME_R)
        if current is not None and current <= RAIL_FORCE_SETTING:
            # Already at or beyond the force value — nothing to do, but mark engaged
            # so we restore to None (i.e. leave alone) on release.
            self._state["engaged"] = True
            self._state["snapshot_setting"] = None
            self._state["engaged_at"] = datetime.now().isoformat()
            self.log(f"Right rail engaged (body={body:.1f}°F) — already at "
                     f"{current}, no setpoint change", level="WARNING")
            return

        self._state["snapshot_setting"] = current
        self._state["engaged"] = True
        self._state["engaged_at"] = datetime.now().isoformat()
        self._state["engage_count_session"] = self._state.get("engage_count_session", 0) + 1

        self.call_service("number/set_value", entity_id=E_BEDTIME_R,
                          value=RAIL_FORCE_SETTING)
        self.log(f"Right rail ENGAGED: body={body:.1f}°F, "
                 f"prev_setpoint={current}, forced={RAIL_FORCE_SETTING}",
                 level="WARNING")

    def _release(self, reason: str) -> None:
        snapshot = self._state.get("snapshot_setting")
        self._state["engaged"] = False
        self._state["streak"] = 0
        self._state["released_at"] = datetime.now().isoformat()

        if snapshot is not None:
            self.call_service("number/set_value", entity_id=E_BEDTIME_R,
                              value=int(snapshot))
            self.log(f"Right rail RELEASED ({reason}): "
                     f"restored setpoint to {int(snapshot)}",
                     level="WARNING")
        else:
            self.log(f"Right rail RELEASED ({reason}): no snapshot to restore",
                     level="WARNING")
        self._state["snapshot_setting"] = None
        self._save_state()
