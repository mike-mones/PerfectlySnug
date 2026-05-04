"""v6 latent state-estimator shadow logger (AppDaemon app).

Subscribes to bed-presence pressure, occupancy, body, and room sensors,
maintains in-process rolling buffers, and on a 60s tick computes the
Features for ml.v6.state_estimator.estimate_state and writes one row per
zone to PG `controller_state_shadow`.

PURELY OBSERVATIONAL — no control side effects, no commands sent to the
topper. Two-key armed via `input_boolean.snug_v6_state_shadow_logging`.

Spec:   docs/proposals/2026-05-04_state_estimation.md
Schema: sql/v7_state_shadow.sql

Add to apps.yaml:
    v6_state_shadow:
      module: v6_state_shadow
      class: V6StateShadow
      postgres_host: 192.168.0.3
"""
from __future__ import annotations

import os
import sys
import time
from collections import deque
from typing import Optional

import hassapi as hass

# Make ml.v6.state_estimator importable when running inside AppDaemon.
# AppDaemon's apps directory layout is /addon_configs/.../apps; the
# PerfectlySnug ml/v6 module is shipped alongside via scp deploy, expected
# at /addon_configs/.../apps/ml/v6/state_estimator.py (mirrors local repo).
HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

try:
    from ml.v6.state_estimator import (
        Features, Percentiles, estimate_state,
    )
    ESTIMATOR_AVAILABLE = True
except Exception as _e:  # pragma: no cover - defensive
    ESTIMATOR_AVAILABLE = False
    _IMPORT_ERR = repr(_e)

# ── Entities (mirror v6_pressure_logger + sleep_controller_v5) ───────
PRESSURE_ENTITIES = {
    "left":  "sensor.bed_presence_2bcab8_left_pressure",
    "right": "sensor.bed_presence_2bcab8_right_pressure",
}
OCCUPIED_ENTITIES = {
    "left":  "binary_sensor.bed_presence_2bcab8_bed_occupied_left",
    "right": "binary_sensor.bed_presence_2bcab8_bed_occupied_right",
}
BODY_LEFT_ENTITIES = {
    "left":  "sensor.smart_topper_left_side_body_sensor_left",
    "right": "sensor.smart_topper_right_side_body_sensor_left",
}
ROOM_ENTITY = "sensor.bedroom_temperature_sensor_temperature"

SHADOW_FLAG = "input_boolean.snug_v6_state_shadow_logging"

# Buffers
PRESSURE_WINDOW_SEC = 15 * 60       # 15-min deque per spec §2.1
BODY_WINDOW_SEC = 15 * 60           # 15-min trend window
TICK_SEC = 60

PG_HOST_DEFAULT = "192.168.0.3"
PG_PORT = 5432
PG_DB = "sleepdata"
PG_USER = "sleepsync"
PG_PASS = "sleepsync_local"

ESTIMATOR_VERSION = "p3a-v1"


class V6StateShadow(hass.Hass):
    """Per-tick state estimator that writes shadow rows to PG."""

    def initialize(self):
        self._pg_host = getattr(self, "args", {}).get(
            "postgres_host", PG_HOST_DEFAULT,
        )
        self._pg_conn = None

        # Per-zone deque of (monotonic_ts, value).
        self._pressure: dict[str, deque] = {
            "left": deque(), "right": deque(),
        }
        # Per-zone deque of (monotonic_ts, body_left_f).
        self._body: dict[str, deque] = {
            "left": deque(), "right": deque(),
        }
        # Last room temp reading.
        self._room_temp_f: Optional[float] = None

        # Track presence transitions per zone: monotonic ts of last change,
        # plus the most recently observed binary value.
        self._presence_change_ts: dict[str, Optional[float]] = {
            "left": None, "right": None,
        }
        self._presence_value: dict[str, Optional[bool]] = {
            "left": None, "right": None,
        }

        if not ESTIMATOR_AVAILABLE:
            self.log(f"v6_state_shadow: estimator import failed: {_IMPORT_ERR}; "
                     "logger will tick but emit 'IMPORT_FAILED' rows only",
                     level="ERROR")

        # Subscribe to sources.
        for zone, eid in PRESSURE_ENTITIES.items():
            try:
                self.listen_state(self._on_pressure, eid, zone=zone)
            except Exception as e:  # pragma: no cover
                self.log(f"v6_state_shadow: listen_state({eid}) failed: {e}",
                         level="WARNING")

        for zone, eid in OCCUPIED_ENTITIES.items():
            try:
                self.listen_state(self._on_presence, eid, zone=zone)
            except Exception as e:  # pragma: no cover
                self.log(f"v6_state_shadow: listen_state({eid}) failed: {e}",
                         level="WARNING")
            # Seed current state immediately.
            cur = self._read_state(eid)
            if cur in ("on", "off"):
                self._presence_value[zone] = (cur == "on")
                self._presence_change_ts[zone] = time.monotonic()

        for zone, eid in BODY_LEFT_ENTITIES.items():
            try:
                self.listen_state(self._on_body, eid, zone=zone)
            except Exception as e:  # pragma: no cover
                self.log(f"v6_state_shadow: listen_state({eid}) failed: {e}",
                         level="WARNING")

        try:
            self.listen_state(self._on_room, ROOM_ENTITY)
        except Exception as e:  # pragma: no cover
            self.log(f"v6_state_shadow: listen_state({ROOM_ENTITY}) failed: {e}",
                     level="WARNING")

        # Initial room temp seed.
        rt = self._coerce_float(self._read_state(ROOM_ENTITY))
        if rt is not None:
            self._room_temp_f = rt

        # Per-zone last estimated state, for prev_state passing.
        self._last_state: dict[str, Optional[str]] = {
            "left": None, "right": None,
        }

        try:
            self.run_every(self._tick, "now+60", TICK_SEC)
        except Exception as e:  # pragma: no cover
            self.log(f"v6_state_shadow: run_every failed: {e}",
                     level="WARNING")

        self.log(f"v6_state_shadow ready — 60s state estimation → "
                 f"controller_state_shadow (gated by {SHADOW_FLAG}, "
                 f"estimator={ESTIMATOR_VERSION})")

    # ── Listeners ──────────────────────────────────────────────────────
    def _on_pressure(self, entity, attribute, old, new, kwargs):
        zone = kwargs.get("zone")
        if zone not in self._pressure:
            return
        v = self._coerce_float(new)
        if v is None:
            return
        try:
            self._pressure[zone].append((time.monotonic(), v))
            self._trim(self._pressure[zone], PRESSURE_WINDOW_SEC)
        except Exception as e:  # pragma: no cover
            self.log(f"v6_state_shadow: pressure append failed: {e}",
                     level="WARNING")

    def _on_body(self, entity, attribute, old, new, kwargs):
        zone = kwargs.get("zone")
        if zone not in self._body:
            return
        v = self._coerce_float(new)
        if v is None:
            return
        try:
            self._body[zone].append((time.monotonic(), v))
            self._trim(self._body[zone], BODY_WINDOW_SEC)
        except Exception as e:  # pragma: no cover
            self.log(f"v6_state_shadow: body append failed: {e}",
                     level="WARNING")

    def _on_presence(self, entity, attribute, old, new, kwargs):
        zone = kwargs.get("zone")
        if zone not in self._presence_value:
            return
        if new not in ("on", "off"):
            return
        new_val = (new == "on")
        if self._presence_value[zone] != new_val:
            self._presence_change_ts[zone] = time.monotonic()
            self._presence_value[zone] = new_val

    def _on_room(self, entity, attribute, old, new, kwargs):
        v = self._coerce_float(new)
        if v is not None:
            self._room_temp_f = v

    # ── Tick ───────────────────────────────────────────────────────────
    def _tick(self, kwargs):
        try:
            self._tick_inner()
        except Exception as e:
            self.log(f"v6_state_shadow tick failed: {e}", level="ERROR")

    def _tick_inner(self):
        # Trim windows regardless to bound memory.
        for dq in self._pressure.values():
            self._trim(dq, PRESSURE_WINDOW_SEC)
        for dq in self._body.values():
            self._trim(dq, BODY_WINDOW_SEC)

        if self._read_state(SHADOW_FLAG) != "on":
            return
        if not ESTIMATOR_AVAILABLE:
            return

        for zone in ("left", "right"):
            try:
                self._estimate_and_write(zone)
            except Exception as e:
                self.log(f"v6_state_shadow [{zone}] estimate failed: {e}",
                         level="WARNING")

    def _estimate_and_write(self, zone: str):
        feats = self._build_features(zone)
        latent = estimate_state(
            feats,
            prev_state=self._last_state[zone],
            percentiles=Percentiles(),
        )
        # Carry prev_state across ticks (DISTURBANCE is transient — don't
        # update). Mirror replay_iter() behavior.
        if latent.state != "DISTURBANCE":
            self._last_state[zone] = latent.state

        self._insert(zone, feats, latent)

    # ── Feature construction ───────────────────────────────────────────
    def _build_features(self, zone: str) -> "Features":
        now_m = time.monotonic()

        # Movement features from pressure deque (samples within last 5/15 min).
        p_dq = list(self._pressure[zone])
        last_5 = [v for ts, v in p_dq if ts >= now_m - 5 * 60]
        last_15 = [v for ts, v in p_dq]   # already trimmed to 15min
        last_1 = [v for ts, v in p_dq if ts >= now_m - 60]

        rms5 = _rms_consecutive_deltas(last_5) if len(last_5) >= 2 else None
        rms15 = _rms_consecutive_deltas(last_15) if len(last_15) >= 2 else None
        var15 = _variance_consecutive_deltas(last_15) if len(last_15) >= 3 else None
        max60 = _max_consecutive_delta(last_1) if len(last_1) >= 2 else None

        # Staleness gate: if last sample > 90s old, mark all movement unavailable.
        if not p_dq or (now_m - p_dq[-1][0]) > 90:
            rms5 = rms15 = var15 = max60 = None

        # Body trend (OLS slope °F/15min).
        b_dq = [(ts, v) for ts, v in self._body[zone]
                if ts >= now_m - BODY_WINDOW_SEC]
        body_trend = _ols_slope_per_15m(b_dq)
        body_now = b_dq[-1][1] if b_dq else None

        # Presence + secs-since-change.
        pres = self._presence_value[zone]
        change_ts = self._presence_change_ts[zone]
        secs_since = (now_m - change_ts) if change_ts is not None else None

        return Features(
            movement_rms_5min=rms5,
            movement_rms_15min=rms15,
            movement_variance_15min=var15,
            movement_max_delta_60s=max60,
            presence_binary=pres,
            seconds_since_presence_change=secs_since,
            body_avg_f=body_now,           # body_left_f used as the body driver
            body_trend_15min=body_trend,
            room_temp_f=self._room_temp_f,
            setting_recent_change_30min=0,  # not consumed by current cascade
        )

    # ── DB ─────────────────────────────────────────────────────────────
    def _insert(self, zone: str, f: "Features", latent):
        try:
            conn = self._get_pg()
            if not conn:
                return
            cur = conn.cursor()
            try:
                cur.execute(
                    """
                    INSERT INTO controller_state_shadow (
                        zone, state, state_confidence, state_degraded, trigger,
                        movement_rms_5min, movement_rms_15min, movement_var_15min,
                        movement_max_delta_60s,
                        body_left_f, room_temp_f, body_trend_15min, body_sensor_valid,
                        presence_binary, seconds_since_presence_change,
                        estimator_version
                    ) VALUES (
                        %s, %s, %s, %s, %s,
                        %s, %s, %s, %s,
                        %s, %s, %s, %s,
                        %s, %s, %s
                    )
                    """,
                    (
                        zone, latent.state, latent.confidence, latent.degraded,
                        (latent.trigger or "")[:200],
                        f.movement_rms_5min, f.movement_rms_15min,
                        f.movement_variance_15min, f.movement_max_delta_60s,
                        f.body_avg_f, f.room_temp_f,
                        f.body_trend_15min, f.body_sensor_validity,
                        f.presence_binary, f.seconds_since_presence_change,
                        ESTIMATOR_VERSION,
                    ),
                )
                conn.commit()
            finally:
                cur.close()
        except Exception as e:
            self.log(f"v6_state_shadow PG insert failed [{zone}]: {e}",
                     level="WARNING")
            try:
                if self._pg_conn:
                    self._pg_conn.close()
            except Exception:
                pass
            self._pg_conn = None

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
            self.log(f"v6_state_shadow PG connect failed: {e}", level="WARNING")
            return None

    # ── Helpers ────────────────────────────────────────────────────────
    @staticmethod
    def _trim(dq: deque, window_sec: float):
        cutoff = time.monotonic() - window_sec
        while dq and dq[0][0] < cutoff:
            dq.popleft()

    def _read_state(self, entity_id: str) -> Optional[str]:
        try:
            return self.get_state(entity_id)
        except Exception:  # pragma: no cover
            return None

    @staticmethod
    def _coerce_float(value) -> Optional[float]:
        if value in (None, "", "unknown", "unavailable"):
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None


# ── Pure helpers (importable for tests) ──────────────────────────────
def _rms_consecutive_deltas(values: list) -> Optional[float]:
    if len(values) < 2:
        return None
    deltas = [abs(values[i] - values[i - 1]) for i in range(1, len(values))]
    sq = sum(d * d for d in deltas) / len(deltas)
    return sq ** 0.5


def _variance_consecutive_deltas(values: list) -> Optional[float]:
    if len(values) < 3:
        return None
    deltas = [abs(values[i] - values[i - 1]) for i in range(1, len(values))]
    mean = sum(deltas) / len(deltas)
    return sum((d - mean) ** 2 for d in deltas) / (len(deltas) - 1)


def _max_consecutive_delta(values: list) -> Optional[float]:
    if len(values) < 2:
        return None
    return max(abs(values[i] - values[i - 1]) for i in range(1, len(values)))


def _ols_slope_per_15m(samples) -> Optional[float]:
    """OLS slope in °F per 15 min over (monotonic_ts_seconds, value) pairs."""
    valid = [(t, v) for t, v in samples if v is not None]
    if len(valid) < 5:
        return None
    t0 = valid[0][0]
    xs = [(t - t0) / 60.0 for t, _ in valid]   # minutes
    ys = [float(v) for _, v in valid]
    n = len(xs)
    mx = sum(xs) / n
    my = sum(ys) / n
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    den = sum((x - mx) ** 2 for x in xs)
    if den == 0:
        return None
    slope_per_min = num / den
    return slope_per_min * 15.0
