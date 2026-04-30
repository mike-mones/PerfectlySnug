"""
Right-zone overheat safety rail (standalone AppDaemon app).

Purpose: the wife's right zone has NO automated controller (v5 is left-zone
only). This app provides a *safety-only* response — it does not try to
optimize comfort, it just prevents sustained extreme overheating.

Behavior
--------
Every 60 seconds, while the right side of the bed is occupied:

  1. Read body_sensor_LEFT on the right zone (the sensor closest to her
     skin; see "Sensor selection" below).
  2. If the reading is ≥ OVERHEAT_HARD_F for OVERHEAT_HARD_STREAK consecutive
     polls AND the right-zone setpoint is not already at OR below the
     emergency value, snapshot the current setpoint and force the right-zone
     bedtime_temperature to RAIL_FORCE_SETTING (-10).
  3. Once engaged, stay engaged with hysteresis: release only when the body
     reading drops below OVERHEAT_RELEASE_F.
  4. On release, restore the snapshotted setpoint so we don't permanently
     override her manual choice.

Gated by `input_boolean.snug_right_overheat_rail_enabled`.

Sensor selection (2026-04-30 update)
------------------------------------
This app originally read body_sensor_CENTER on the right side, with a 88°F
engage threshold. Analysis on 14 nights of post-BedJet-window data showed:

    body_center_f (right zone): p50=86.1  p95=95.5  → would engage 22% of
                                                     occupied minutes
    body_left_f   (right zone): p50=79.7  p95=86.5  p99=88.7  → 1.5%

The center sensor is dominated by warm-sheet/blanket heat, not skin
temperature. The left sensor (closest to her body since she sleeps on the
right side of the bed and her body lies on the left edge of her zone)
gives a true skin-contact signal, with statistics statistically
indistinguishable from the user's left zone (p50=79.3, p95=84.7, p99=86.6
on body_left_f).

Switching to body_sensor_LEFT keeps the 88°F engage threshold (her own p99
on body_left_f) and reduces false engagements 15× while still catching
real overheat events. See _archive/right_zone_rollout_2026-04-30.md.

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

OVERHEAT_HARD_F = 88.0       # body_left_f ≥ this for STREAK polls → engage.
                             # 88°F is her p99 on the skin-side sensor — i.e.
                             # natural overheat threshold, ~2σ above her p50.
OVERHEAT_HARD_STREAK = 2     # 2 consecutive polls (~2 min)
OVERHEAT_RELEASE_F = 84.0    # release engagement when body drops below
RAIL_FORCE_SETTING = -10     # value forced on bedtime_temperature
POLL_INTERVAL_SEC = 60

# BedJet warm-blanket window. The wife runs a BedJet on heat for the first
# ~30 min of sleep to pre-warm the sheets while the topper cools. The BedJet
# blows hot air across the right-zone body sensors and inflates readings to
# 90-99°F. Engaging the rail during this window would force the topper to
# max-cool against an intentional heating cycle, which the user explicitly
# does not want. So we SUPPRESS engagement (do not even count toward the
# streak) for the first BEDJET_SUPPRESS_MIN minutes after right-bed
# occupancy onset. After the window we operate normally.
BEDJET_SUPPRESS_MIN = 30.0

E_RAIL_FLAG = "input_boolean.snug_right_overheat_rail_enabled"
E_BEDTIME_R = "number.smart_topper_right_side_bedtime_temperature"
# Skin-contact sensor (left of right zone, where her body lies). Was previously
# the center sensor; see "Sensor selection" in the module docstring.
E_BODY_LEFT_R = "sensor.smart_topper_right_side_body_sensor_left"
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
            "occupied_since": None,      # ISO ts of latest off→on right-bed transition
            "last_occupied": False,
        }
        self._load_state()

        self.run_every(self._tick, "now", POLL_INTERVAL_SEC)
        self.log("Right-zone overheat safety rail ready "
                 f"(engage ≥{OVERHEAT_HARD_F}°F x{OVERHEAT_HARD_STREAK}, "
                 f"release <{OVERHEAT_RELEASE_F}°F, force={RAIL_FORCE_SETTING}, "
                 f"bedjet_suppress={BEDJET_SUPPRESS_MIN:.0f}min)")

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
            self._state["last_occupied"] = False
            self._state["occupied_since"] = None
            return

        # Gate 2: bed occupied? Don't fight the firmware when she's not in bed.
        occupied = self._read_str(E_OCCUPIED_R) == "on"
        if not occupied:
            if self._state["engaged"]:
                self._release(reason="bed_unoccupied")
            self._state["last_occupied"] = False
            self._state["occupied_since"] = None
            self._save_state()
            return

        # Track right-bed occupancy onset for the BedJet suppression window.
        now = datetime.now()
        if not self._state.get("last_occupied"):
            self._state["occupied_since"] = now.isoformat()
            self._state["last_occupied"] = True

        body = self._read_float(E_BODY_LEFT_R)
        if body is None:
            # Don't update streak on a missing read — neither engage nor release.
            self._save_state()
            return

        # Gate 3: BedJet suppression. For the first BEDJET_SUPPRESS_MIN minutes
        # after right-bed occupancy onset, the BedJet's heated airflow inflates
        # the body sensor (commonly 90-99°F). Do NOT count toward the streak,
        # do NOT engage. Already-engaged state is preserved (the operator
        # might have turned the rail on while a previous engagement was active),
        # but a fresh occupancy session starts engaged=False because release
        # already fired on the bed-empty transition.
        in_bedjet_window = False
        occ_since = self._state.get("occupied_since")
        if occ_since:
            try:
                occ_dt = datetime.fromisoformat(occ_since)
                mins = (now - occ_dt).total_seconds() / 60.0
                in_bedjet_window = mins <= BEDJET_SUPPRESS_MIN
            except (TypeError, ValueError):
                in_bedjet_window = False

        if in_bedjet_window:
            # Hold streak at 0 so any post-window readings start fresh.
            if body >= OVERHEAT_HARD_F:
                self.log(f"BedJet window ({BEDJET_SUPPRESS_MIN:.0f}min): "
                         f"suppressing body={body:.1f}°F (no streak, no engage)",
                         level="INFO")
            self._state["streak"] = 0
            self._save_state()
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
