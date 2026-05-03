"""v6 high-resolution bed-pressure movement aggregator (AppDaemon app).

Subscribes to the bed-presence pressure sensors and writes 60-second
aggregates to PG table `controller_pressure_movement`. Used by the v6
controller's `movement_density_15m` feature (right-comfort proxy + regime
classifier).

Behavior
--------
- Per-zone state-change subscription on the raw pressure sensor value.
- Each new reading appends `(ts, value)` to a sliding 60s window.
- Every 60s, computes:
      abs_delta_sum_60s = sum(|v[i] - v[i-1]|)
      max_delta_60s     = max(|v[i] - v[i-1]|)
      sample_count      = number of readings in the window
      occupied          = current binary_sensor occupied_<zone> state
  and inserts one row per zone (skipping zones with sample_count == 0).
- Gated on `input_boolean.snug_v6_shadow_logging`. If off, ticks no-op.

DB connection: lazy psycopg2 connection identical pattern to v5.2's
`_get_pg()` (3s timeout, statement_timeout=3000ms, close-on-error).

Add to apps.yaml:
    v6_pressure_logger:
      module: v6_pressure_logger
      class: V6PressureLogger
"""
from __future__ import annotations

import time
from collections import deque
from typing import Optional

import hassapi as hass

# ── Entities (per task spec — these are the high-resolution pressure
#    sensors Sensor sub-component exposes; distinct from the v5.2
#    `_left_pressure`/`_right_pressure` which are the calibrated rollups). ─
PRESSURE_ENTITIES = {
    "left":  "sensor.bed_presence_2bcab8_left_pressure",
    "right": "sensor.bed_presence_2bcab8_right_pressure",
}
OCCUPIED_ENTITIES = {
    "left":  "binary_sensor.bed_presence_2bcab8_bed_occupied_left",
    "right": "binary_sensor.bed_presence_2bcab8_bed_occupied_right",
}
SHADOW_FLAG = "input_boolean.snug_v6_shadow_logging"

WINDOW_SEC = 60.0
TICK_SEC = 60

# Postgres
PG_HOST_DEFAULT = "192.168.0.3"
PG_PORT = 5432
PG_DB = "sleepdata"
PG_USER = "sleepsync"
PG_PASS = "sleepsync_local"


class V6PressureLogger(hass.Hass):
    """Subscribes to pressure sensors; emits 60s movement aggregates to PG."""

    def initialize(self):
        self._pg_host = getattr(self, "args", {}).get(
            "postgres_host", PG_HOST_DEFAULT,
        )
        self._pg_conn = None
        # Per-zone deque of (monotonic_ts, value) tuples.
        self._readings: dict[str, deque] = {
            "left": deque(),
            "right": deque(),
        }

        for zone, eid in PRESSURE_ENTITIES.items():
            try:
                self.listen_state(self._on_pressure, eid, zone=zone)
            except Exception as e:  # pragma: no cover
                self.log(f"v6_pressure_logger: listen_state({eid}) failed: {e}",
                         level="WARNING")

        try:
            self.run_every(self._tick, "now+60", TICK_SEC)
        except Exception as e:  # pragma: no cover
            self.log(f"v6_pressure_logger: run_every failed: {e}",
                     level="WARNING")

        self.log("v6_pressure_logger ready — 60s movement aggregates → "
                 "controller_pressure_movement (gated by "
                 f"{SHADOW_FLAG})")

    # ── Listeners ───────────────────────────────────────────────────────

    def _on_pressure(self, entity, attribute, old, new, kwargs):
        zone = kwargs.get("zone")
        if zone not in self._readings:
            return
        val = self._coerce_float(new)
        if val is None:
            return
        try:
            self._readings[zone].append((time.monotonic(), val))
            self._trim(zone)
        except Exception as e:  # pragma: no cover
            self.log(f"v6_pressure_logger: append failed for {zone}: {e}",
                     level="WARNING")

    # ── Tick ────────────────────────────────────────────────────────────

    def _tick(self, kwargs):
        try:
            self._tick_inner()
        except Exception as e:  # never crash AppDaemon
            self.log(f"v6_pressure_logger tick failed: {e}", level="ERROR")

    def _tick_inner(self):
        # Gate on shadow logging
        if self._read_state(SHADOW_FLAG) != "on":
            # Still trim windows so they don't grow unbounded if HA is up but
            # logging is off.
            for zone in self._readings:
                self._trim(zone)
            return

        for zone in ("left", "right"):
            self._trim(zone)
            sample_count = len(self._readings[zone])
            if sample_count == 0:
                continue
            abs_delta_sum, max_delta = self._aggregate(zone)
            occupied = self._read_state(OCCUPIED_ENTITIES[zone]) == "on"
            self._insert(zone, abs_delta_sum, max_delta, sample_count, occupied)

    # ── Aggregation ─────────────────────────────────────────────────────

    def _aggregate(self, zone: str) -> tuple[float, float]:
        readings = list(self._readings[zone])
        if len(readings) < 2:
            return 0.0, 0.0
        abs_delta_sum = 0.0
        max_delta = 0.0
        prev = readings[0][1]
        for _, v in readings[1:]:
            d = abs(v - prev)
            abs_delta_sum += d
            if d > max_delta:
                max_delta = d
            prev = v
        return abs_delta_sum, max_delta

    def _trim(self, zone: str):
        cutoff = time.monotonic() - WINDOW_SEC
        dq = self._readings[zone]
        while dq and dq[0][0] < cutoff:
            dq.popleft()

    # ── DB ──────────────────────────────────────────────────────────────

    def _insert(self, zone, abs_delta_sum, max_delta, sample_count, occupied):
        try:
            conn = self._get_pg()
            if not conn:
                return
            cur = conn.cursor()
            try:
                cur.execute(
                    """
                    INSERT INTO controller_pressure_movement
                        (zone, ts, abs_delta_sum_60s, max_delta_60s,
                         sample_count, occupied)
                    VALUES (%s, NOW(), %s, %s, %s, %s)
                    """,
                    (zone, float(abs_delta_sum), float(max_delta),
                     int(sample_count), bool(occupied)),
                )
                conn.commit()
            finally:
                cur.close()
        except Exception as e:
            self.log(f"v6_pressure_logger PG insert failed: {e}",
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
            self.log(f"v6_pressure_logger PG connect failed: {e}",
                     level="WARNING")
            return None

    # ── Helpers ─────────────────────────────────────────────────────────

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
