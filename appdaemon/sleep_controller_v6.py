"""Sleep Controller v6 — SHADOW MODE ONLY (R2A).

This is the v6 controller skeleton. In R2A it does NOT actuate. Every 5
minutes it computes the v6 plan and writes a shadow row to
`controller_readings` (controller_version='v6_shadow'). v5.2 keeps owning
the dial.

Future R3 will:
  - Add this app to apps.yaml.
  - Flip `input_boolean.snug_v6_left_live` (etc.) to enable real writes
    via SafetyActuator.

This file imports the ml/v6 modules from R1B and the SafetyActuator from
R2A. Imports are validated in `initialize()` — failure is logged but does
not crash AppDaemon.
"""
from __future__ import annotations

import os
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

import hassapi as hass

# Ensure ml.v6 is importable (project root may not be on sys.path inside
# the AppDaemon container; fall back to relative path discovery).
try:
    from ml.v6 import regime as v6_regime
    from ml.v6 import firmware_plant as v6_firmware
    from ml.v6 import right_comfort_proxy as v6_proxy
    from ml.v6 import residual_head as v6_residual
    _ML_IMPORT_ERROR = None
except ImportError as e:  # pragma: no cover - exercised only in container
    _ML_IMPORT_ERROR = e
    v6_regime = None
    v6_firmware = None
    v6_proxy = None
    v6_residual = None

try:
    from appdaemon.safety_actuator import SafetyActuator, DummySafetyActuator
except ImportError:
    # AppDaemon loads modules from `apps/` on sys.path directly without
    # the `appdaemon.` prefix.
    try:
        from safety_actuator import SafetyActuator, DummySafetyActuator  # type: ignore
    except ImportError as e:  # pragma: no cover
        SafetyActuator = None
        DummySafetyActuator = None
        _ML_IMPORT_ERROR = _ML_IMPORT_ERROR or e


# ── Constants ─────────────────────────────────────────────────────────
V6_VERSION = "v6_shadow_v1"
V6_PATCH_LEVEL = "v6_regime+plant+proxy+residual+actuator"
CONTROLLER_VERSION_TAG = "v6_shadow"

CYCLE_INTERVAL_SEC = 300

# Helper entities
E_MASTER_ARM = "input_boolean.snug_v6_enabled"
E_RESIDUAL_ENABLED = "input_boolean.snug_v6_residual_enabled"
E_SHADOW_LOGGING = "input_boolean.snug_v6_shadow_logging"
E_RAIL_ENGAGED = "input_boolean.snug_right_rail_engaged"
E_RESIDUAL_MODEL_PATH = "input_text.snug_v6_residual_model_path"
E_BEDJET = "climate.bedjet_shar"
E_SLEEP_MODE = "input_boolean.sleep_mode"
E_SLEEP_STAGE = "input_text.apple_health_sleep_stage"
E_3LEVEL_LEFT = "switch.smart_topper_left_side_3_level_mode"
E_3LEVEL_RIGHT = "switch.smart_topper_right_side_3_level_mode"
DEFAULT_ROOM_TEMP_ENTITY = "sensor.bedroom_temperature_sensor_temperature"

# Bedjet active state set
BEDJET_INACTIVE_STATES = {"off", "unavailable", "unknown", None, ""}

# Zone entity map (mirrors v5.2's ZONE_ENTITY_IDS)
ZONE_ENTITY_IDS = {
    "left": {
        "bedtime": "number.smart_topper_left_side_bedtime_temperature",
        "body_center": "sensor.smart_topper_left_side_body_sensor_center",
        "body_left": "sensor.smart_topper_left_side_body_sensor_left",
        "body_right": "sensor.smart_topper_left_side_body_sensor_right",
        "ambient": "sensor.smart_topper_left_side_ambient_temperature",
        "setpoint": "sensor.smart_topper_left_side_temperature_setpoint",
        "blower_pct": "sensor.smart_topper_left_side_blower_output",
        "occupied": "binary_sensor.bed_presence_2bcab8_bed_occupied_left",
    },
    "right": {
        "bedtime": "number.smart_topper_right_side_bedtime_temperature",
        "body_center": "sensor.smart_topper_right_side_body_sensor_center",
        "body_left": "sensor.smart_topper_right_side_body_sensor_left",
        "body_right": "sensor.smart_topper_right_side_body_sensor_right",
        "ambient": "sensor.smart_topper_right_side_ambient_temperature",
        "setpoint": "sensor.smart_topper_right_side_temperature_setpoint",
        "blower_pct": "sensor.smart_topper_right_side_blower_output",
        "occupied": "binary_sensor.bed_presence_2bcab8_bed_occupied_right",
    },
}

# Cap-table location (matches the JSON committed in R1B / fit by R1A).
_HERE = Path(__file__).resolve().parent
_CAP_TABLE_CANDIDATES = [
    _HERE.parent / "ml" / "v6" / "firmware_cap_table.json",
    Path("/config/apps/ml/v6/firmware_cap_table.json"),
]

# Postgres
PG_HOST_DEFAULT = "192.168.0.3"
PG_PORT = 5432
PG_DB = "sleepdata"
PG_USER = "sleepsync"
PG_PASS = "sleepsync_local"


class SleepControllerV6(hass.Hass):
    """v6 shadow-only controller. Logs plan rows to PG; never actuates."""

    def initialize(self):
        self.log("=" * 60)
        self.log(f"v6 Controller initializing (version={V6_VERSION})")

        # Validate ml.v6 imports
        if _ML_IMPORT_ERROR is not None:
            self.log(f"v6: ml.v6 imports failed ({_ML_IMPORT_ERROR}); "
                     "shadow logger disabled", level="ERROR")
            self._enabled = False
            return
        self._enabled = True

        self._pg_host = getattr(self, "args", {}).get(
            "postgres_host", PG_HOST_DEFAULT,
        )
        self._room_temp_entity = getattr(self, "args", {}).get(
            "room_temp_entity", DEFAULT_ROOM_TEMP_ENTITY,
        )
        self._pg_conn = None

        # Initialize ml stack
        self._regime_config = v6_regime.DEFAULT_CONFIG
        self._plant = v6_firmware.FirmwarePlant(
            cap_table_path=self._find_cap_table(),
        )
        self._residual_heads = {
            "left": v6_residual.ResidualHead(zone="left"),
            "right": v6_residual.ResidualHead(zone="right"),
        }

        # Construct safety actuators in dry_run — even though we never call
        # write() in shadow mode, having them in place mirrors the R3 wiring.
        if SafetyActuator is not None:
            self.safety_actuator = {
                "left": SafetyActuator(self, "left", dry_run=True),
                "right": SafetyActuator(self, "right", dry_run=True),
            }
        else:
            self.safety_actuator = {
                "left": None, "right": None,
            }

        # Per-zone bed-onset timestamps (epoch seconds, monotonic-ish via
        # datetime.now). Filled by listeners.
        self._bed_onset_ts = {"left": None, "right": None}
        self._bedjet_off_ts: Optional[float] = None  # epoch seconds

        # Hook listeners (best-effort)
        self._wire_listeners()

        # Periodic shadow tick
        try:
            self.run_every(self._control_loop, "now+30", CYCLE_INTERVAL_SEC)
        except Exception as e:  # pragma: no cover
            self.log(f"v6: run_every failed: {e}", level="ERROR")

        # Log initial helper state
        try:
            self.log(f"v6: master_arm={self._read(E_MASTER_ARM)} "
                     f"shadow_logging={self._read(E_SHADOW_LOGGING)} "
                     f"residual_enabled={self._read(E_RESIDUAL_ENABLED)} "
                     f"left_live={self._read('input_boolean.snug_v6_left_live')} "
                     f"right_live={self._read('input_boolean.snug_v6_right_live')}")
        except Exception:
            pass

        self.log(f"v6 Controller {V6_PATCH_LEVEL} ready — shadow logging only")
        self.log("=" * 60)

    # ── Listeners ──────────────────────────────────────────────────────

    def _wire_listeners(self):
        for zone in ("left", "right"):
            try:
                self.listen_state(
                    self._on_bed_onset,
                    ZONE_ENTITY_IDS[zone]["occupied"],
                    new="on", zone=zone,
                )
            except Exception:  # pragma: no cover
                pass
        try:
            self.listen_state(self._on_bedjet_change, E_BEDJET)
        except Exception:  # pragma: no cover
            pass
        try:
            self.listen_state(self._on_master_enabled_change, E_MASTER_ARM)
        except Exception:  # pragma: no cover
            pass
        try:
            self.listen_state(self._on_residual_enabled_change,
                              E_RESIDUAL_ENABLED)
        except Exception:  # pragma: no cover
            pass

    def _on_bed_onset(self, entity, attribute, old, new, kwargs):
        zone = kwargs.get("zone")
        if zone in self._bed_onset_ts and new == "on":
            self._bed_onset_ts[zone] = time.time()

    def _on_bedjet_change(self, entity, attribute, old, new, kwargs):
        # Track BedJet off transitions for post_bedjet_min calculation.
        if new in BEDJET_INACTIVE_STATES and old not in BEDJET_INACTIVE_STATES:
            self._bedjet_off_ts = time.time()

    def _on_master_enabled_change(self, entity, attribute, old, new, kwargs):
        if new == "on":
            self.log("v6 master arm enabled — actuation requires "
                     "snug_v6_<zone>_live too (shadow-only until R3 deploy)",
                     level="WARNING")
        else:
            self.log("v6 master arm disabled — pure shadow mode", level="INFO")

    def _on_residual_enabled_change(self, entity, attribute, old, new, kwargs):
        if new != "on":
            self.log("v6 residual disabled", level="INFO")
            return
        path = self._read(E_RESIDUAL_MODEL_PATH)
        if not path or not os.path.isfile(path):
            self.log(f"v6 residual model not found at {path!r}; forcing "
                     "snug_v6_residual_enabled OFF", level="WARNING")
            try:
                self.call_service("input_boolean/turn_off",
                                  entity_id=E_RESIDUAL_ENABLED)
            except Exception:  # pragma: no cover
                pass
            return
        try:
            self._residual_heads["left"] = v6_residual.ResidualHead(
                zone="left", model_path=path,
            )
            self.log(f"v6 residual model loaded from {path}", level="INFO")
        except Exception as e:  # pragma: no cover
            self.log(f"v6 residual model load failed: {e}", level="WARNING")

    # ── Control loop (shadow only) ─────────────────────────────────────

    def _control_loop(self, kwargs):
        try:
            self._control_loop_inner()
        except Exception as e:  # never crash the app
            self.log(f"v6 control loop failed: {e!r}", level="ERROR")

    def _control_loop_inner(self):
        if not getattr(self, "_enabled", False):
            return
        if self._read(E_SHADOW_LOGGING) != "on":
            return

        room_f = self._read_temperature(self._room_temp_entity)
        sleep_stage = self._read(E_SLEEP_STAGE)
        bedjet_state = self._read(E_BEDJET)
        bedjet_active = bedjet_state not in BEDJET_INACTIVE_STATES
        rail_engaged = self._read(E_RAIL_ENGAGED) == "on"
        # Always force the 3-level mode flag OFF in shadow logging — we
        # don't actuate, so this is just a *recorded* assertion.
        three_level_off = (
            self._read(E_3LEVEL_LEFT) != "on"
            and self._read(E_3LEVEL_RIGHT) != "on"
        )

        elapsed_min = self._elapsed_min_since_sleep()
        post_bedjet_min = self._post_bedjet_min(bedjet_active)

        for zone in ("left", "right"):
            try:
                self._tick_zone(
                    zone,
                    room_f=room_f, sleep_stage=sleep_stage,
                    bedjet_active=bedjet_active, rail_engaged=rail_engaged,
                    three_level_off=three_level_off,
                    elapsed_min=elapsed_min,
                    post_bedjet_min=post_bedjet_min,
                )
            except Exception as e:
                self.log(f"v6 zone tick failed ({zone}): {e!r}", level="ERROR")

    def _tick_zone(self, zone, *, room_f, sleep_stage, bedjet_active,
                   rail_engaged, three_level_off, elapsed_min,
                   post_bedjet_min):
        # Heartbeat the safety actuator at the start of every tick (shadow
        # or live) so the dead-man timer reflects controller liveness, not
        # last-successful-write. See safety_actuator.py heartbeat().
        try:
            actuator = self.safety_actuator.get(zone) if isinstance(
                self.safety_actuator, dict) else None
            if actuator is not None and hasattr(actuator, "heartbeat"):
                actuator.heartbeat()
        except Exception:  # pragma: no cover
            pass
        snap = self._read_zone_snapshot(zone)
        body_skin = snap.get("body_left")
        body_hot = snap.get("body_left")
        if (zone == "right" and post_bedjet_min is not None
                and post_bedjet_min > 60 and snap.get("body_center") is not None):
            body_hot = max(snap.get("body_left") or -999.0,
                           snap.get("body_center"))
        body_avg = snap.get("body_avg")
        bed_occupied = self._read(ZONE_ENTITY_IDS[zone]["occupied"]) == "on"
        mins_since_onset = self._mins_since_onset(zone)
        movement_density = self._movement_density_15m(zone)
        actual_setting = snap.get("setting")
        actual_setpoint_f = snap.get("setpoint")
        actual_blower = snap.get("blower_pct")

        # Regime classify
        result = v6_regime.classify(
            zone=zone,
            elapsed_min=elapsed_min if elapsed_min is not None else 0.0,
            mins_since_onset=mins_since_onset,
            post_bedjet_min=post_bedjet_min,
            sleep_stage=sleep_stage,
            bed_occupied=bed_occupied,
            room_f=room_f,
            body_skin_f=body_skin,
            body_hot_f=body_hot,
            body_avg_f=body_avg,
            override_freeze_active=False,
            right_rail_engaged=rail_engaged,
            pre_sleep_active=False,
            three_level_off=three_level_off,
            movement_density_15m=movement_density,
            config=self._regime_config,
        )
        regime_name = result["regime"]
        regime_reason = result["reason"]
        base_setting = result["base_setting"]
        target = base_setting if base_setting is not None else (actual_setting or 0)

        # Residual (LCB) — disabled by default (helper off). Best-effort.
        residual_delta = 0
        residual_n_support = None
        residual_lcb = None
        if self._read(E_RESIDUAL_ENABLED) == "on":
            head = self._residual_heads.get(zone)
            if head is not None:
                features = {
                    "cycle_phase": (elapsed_min or 0.0) / 90.0,
                    "room_temp_bin": ((room_f or 72.0) - 72.0) / 4.0,
                    "body_skin_bin": ((body_skin or 80.0) - 80.0) / 4.0,
                    "pre_sleep_min": 0.0,
                    "post_bedjet_min": post_bedjet_min or 0.0,
                    "bedjet_active": 1.0 if bedjet_active else 0.0,
                    "body_hot": ((body_hot or 80.0) - 80.0) / 4.0,
                }
                try:
                    delta, meta = head.predict_lcb(features)
                    residual_delta = int(delta)
                    residual_n_support = meta.get("n_support")
                    residual_lcb = meta.get("lcb")
                except Exception as e:  # pragma: no cover
                    self.log(f"v6 residual predict failed: {e}", level="WARNING")
        target = int(round(max(-10, min(0, (target or 0) + residual_delta))))

        # Plant prediction + divergence
        try:
            plant_predicted = self._plant.predict_setpoint_f(
                target, ambient_f=(room_f or 72.0),
            )
        except Exception as e:  # pragma: no cover
            plant_predicted = None
            self.log(f"v6 plant predict failed: {e}", level="WARNING")
        divergence_steps = None
        if (plant_predicted is not None and actual_setting is not None):
            try:
                divergence_steps = abs(int(target) - int(actual_setting))
            except (TypeError, ValueError):
                divergence_steps = None

        # SHADOW MODE: never write. R3 will replace the next line with a
        # real `self.safety_actuator[zone].write(...)` call.
        # (Intentionally NOT calling self.safety_actuator[zone].write() here.)

        self._log_shadow_row(
            zone=zone,
            elapsed_min=elapsed_min,
            snap=snap,
            room_f=room_f,
            sleep_stage=sleep_stage,
            target=target,
            actual_setting=actual_setting,
            regime=regime_name,
            regime_reason=regime_reason,
            residual=residual_delta,
            residual_n_support=residual_n_support,
            residual_lcb=residual_lcb,
            divergence_steps=divergence_steps,
            plant_predicted_setpoint_f=plant_predicted,
            bedjet_active=bedjet_active,
            movement_density_15m=movement_density,
            post_bedjet_min=post_bedjet_min,
            mins_since_onset=mins_since_onset,
            l_active_dial=actual_setting,
            three_level_off=three_level_off,
            right_rail_engaged=rail_engaged,
            actual_blower_pct_typed=(int(round(actual_blower))
                                     if actual_blower is not None else None),
            bed_occupied=bed_occupied,
        )

    # ── Sensor / context helpers ───────────────────────────────────────

    def _read(self, entity_id: str) -> Optional[str]:
        try:
            return self.get_state(entity_id)
        except Exception:  # pragma: no cover
            return None

    def _read_float(self, entity_id: str) -> Optional[float]:
        v = self._read(entity_id)
        if v in (None, "", "unknown", "unavailable"):
            return None
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    def _read_temperature(self, entity_id: str) -> Optional[float]:
        v = self._read_float(entity_id)
        if v is None:
            return None
        try:
            unit = self.get_state(entity_id, attribute="unit_of_measurement")
        except Exception:
            unit = None
        if isinstance(unit, str) and unit.strip().lower() in {"°c", "c", "celsius"}:
            return round((v * 9 / 5) + 32, 2)
        return v

    def _read_zone_snapshot(self, zone) -> dict:
        e = ZONE_ENTITY_IDS[zone]
        body_left = self._read_temperature(e["body_left"])
        body_center = self._read_temperature(e["body_center"])
        body_right = self._read_temperature(e["body_right"])
        body_vals = [v for v in (body_left, body_center, body_right) if v is not None]
        body_avg = sum(body_vals) / len(body_vals) if body_vals else None
        setting = self._read_float(e["bedtime"])
        return {
            "body_left": body_left,
            "body_center": body_center,
            "body_right": body_right,
            "body_avg": body_avg,
            "ambient": self._read_temperature(e["ambient"]),
            "setpoint": self._read_temperature(e["setpoint"]),
            "blower_pct": self._read_float(e["blower_pct"]),
            "setting": int(setting) if setting is not None else None,
        }

    def _elapsed_min_since_sleep(self) -> Optional[float]:
        """Best-effort: not gated on _is_sleeping (shadow logs always fire)."""
        # No persistent sleep_start in v6 yet; approximate via sleep_mode flip
        # via state attribute if available, else None.
        try:
            ts = self.get_state(E_SLEEP_MODE, attribute="last_changed")
        except Exception:
            ts = None
        if not ts:
            return None
        try:
            dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
            return (datetime.now(dt.tzinfo) - dt).total_seconds() / 60.0
        except Exception:
            return None

    def _mins_since_onset(self, zone: str) -> Optional[float]:
        ts = self._bed_onset_ts.get(zone)
        if ts is None:
            # Fall back to occupied entity's last_changed
            try:
                lc = self.get_state(
                    ZONE_ENTITY_IDS[zone]["occupied"],
                    attribute="last_changed",
                )
            except Exception:
                lc = None
            if not lc:
                return None
            try:
                dt = datetime.fromisoformat(str(lc).replace("Z", "+00:00"))
                return (datetime.now(dt.tzinfo) - dt).total_seconds() / 60.0
            except Exception:
                return None
        return (time.time() - ts) / 60.0

    def _post_bedjet_min(self, bedjet_active: bool) -> Optional[float]:
        if bedjet_active:
            return None
        if self._bedjet_off_ts is None:
            return None
        return (time.time() - self._bedjet_off_ts) / 60.0

    def _movement_density_15m(self, zone: str) -> Optional[float]:
        """Average abs_delta_sum_60s over last 15 minutes from PG."""
        try:
            conn = self._get_pg()
            if not conn:
                return None
            cur = conn.cursor()
            try:
                cur.execute(
                    """
                    SELECT AVG(abs_delta_sum_60s)
                      FROM controller_pressure_movement
                     WHERE zone = %s
                       AND ts > NOW() - INTERVAL '15 minutes'
                    """,
                    (zone,),
                )
                row = cur.fetchone()
                if row and row[0] is not None:
                    return float(row[0])
                return None
            finally:
                cur.close()
        except Exception as e:  # pragma: no cover
            self.log(f"v6 movement_density_15m query failed: {e}",
                     level="WARNING")
            return None

    @staticmethod
    def _find_cap_table() -> Optional[str]:
        for p in _CAP_TABLE_CANDIDATES:
            if p.is_file():
                return str(p)
        return None

    # ── PG ─────────────────────────────────────────────────────────────

    def _get_pg(self):
        if self._pg_conn is not None:
            try:
                cur = self._pg_conn.cursor()
                cur.execute("SELECT 1")
                cur.close()
                return self._pg_conn
            except Exception:
                try:
                    self._pg_conn.close()
                except Exception:
                    pass
                self._pg_conn = None
        try:
            import psycopg2
            self._pg_conn = psycopg2.connect(
                host=self._pg_host, port=PG_PORT, dbname=PG_DB,
                user=PG_USER, password=PG_PASS,
                connect_timeout=3,
                options="-c statement_timeout=3000",
            )
            return self._pg_conn
        except Exception as e:
            self.log(f"v6 Postgres connect failed: {e}", level="WARNING")
            return None

    def _log_shadow_row(self, **k):
        """Insert one shadow row into controller_readings."""
        try:
            conn = self._get_pg()
            if not conn:
                return
            cur = conn.cursor()
            try:
                snap = k["snap"]
                phase = k["sleep_stage"] or "shadow"
                notes = (
                    f"v6_shadow regime={k['regime']} "
                    f"reason={k['regime_reason']} "
                    f"target={k['target']} "
                    f"actual={k['actual_setting']}"
                )
                cur.execute(
                    """
                    INSERT INTO controller_readings (
                        ts, zone, phase, elapsed_min,
                        body_right_f, body_center_f, body_left_f, body_avg_f,
                        ambient_f, room_temp_f, setpoint_f,
                        setting, effective, action, notes, controller_version,
                        regime, regime_reason, residual, residual_n_support,
                        residual_lcb, divergence_steps,
                        plant_predicted_setpoint_f, bedjet_active,
                        movement_density_15m, post_bedjet_min, mins_since_onset,
                        l_active_dial, three_level_off, right_rail_engaged,
                        actual_blower_pct_typed
                    ) VALUES (
                        NOW(), %s, %s, %s,
                        %s, %s, %s, %s,
                        %s, %s, %s,
                        %s, %s, %s, %s, %s,
                        %s, %s, %s, %s,
                        %s, %s,
                        %s, %s,
                        %s, %s, %s,
                        %s, %s, %s,
                        %s
                    )
                    """,
                    (
                        k["zone"], phase, k["elapsed_min"],
                        snap.get("body_right"), snap.get("body_center"),
                        snap.get("body_left"), snap.get("body_avg"),
                        snap.get("ambient"), k["room_f"], snap.get("setpoint"),
                        k["target"], k["actual_setting"],
                        "shadow", notes, CONTROLLER_VERSION_TAG,
                        k["regime"], k["regime_reason"], k["residual"],
                        k["residual_n_support"], k["residual_lcb"],
                        k["divergence_steps"],
                        k["plant_predicted_setpoint_f"], k["bedjet_active"],
                        k["movement_density_15m"], k["post_bedjet_min"],
                        k["mins_since_onset"],
                        k["l_active_dial"], k["three_level_off"],
                        k["right_rail_engaged"],
                        k["actual_blower_pct_typed"],
                    ),
                )
                conn.commit()
            finally:
                cur.close()
        except Exception as e:
            self.log(f"v6 shadow row insert failed: {e}", level="WARNING")
            try:
                if self._pg_conn:
                    self._pg_conn.close()
            except Exception:
                pass
            self._pg_conn = None
