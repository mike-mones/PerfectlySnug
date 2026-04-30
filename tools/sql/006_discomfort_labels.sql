-- Discomfort labels view (companion to ml/discomfort_label.py).
--
-- Coarse, SQL-native cut for ad-hoc query. Does NOT replace the python
-- pipeline — `tools/build_discomfort_corpus.py` is the source of truth
-- because it does the per-night percentile gating and the multi-signal
-- consensus correctly. This view is provided so you can grep/aggregate
-- the most-suspect minutes from the psql shell.
--
-- Run as sleepsync:
--   PGPASSWORD=sleepsync_local psql -U sleepsync -h localhost -d sleepdata \
--     -f tools/sql/006_discomfort_labels.sql

CREATE OR REPLACE VIEW v_discomfort_minutes_left AS
WITH per_night AS (
  SELECT
    CASE
      WHEN EXTRACT(HOUR FROM ts AT TIME ZONE 'America/New_York') >= 18
      THEN (ts AT TIME ZONE 'America/New_York')::date
      ELSE (ts AT TIME ZONE 'America/New_York')::date - 1
    END AS night_date,
    ts, zone, action, override_delta, setting, elapsed_min,
    body_avg_f, body_left_f, room_temp_f,
    bed_left_calibrated_pressure_pct,
    bed_occupied_left
  FROM controller_readings
  WHERE zone = 'left'
    AND controller_version LIKE 'v5%'
    AND action NOT IN ('empty_bed', 'passive')
),
windowed AS (
  SELECT
    *,
    STDDEV(body_avg_f) OVER w_body AS body_30m_sd,
    STDDEV(bed_left_calibrated_pressure_pct) OVER w_press AS press_5m_sd,
    PERCENTILE_DISC(0.75) WITHIN GROUP (ORDER BY body_avg_f) OVER w_night
      AS body_p75_night
  FROM per_night
  WINDOW
    w_body  AS (PARTITION BY night_date
                ORDER BY ts
                RANGE BETWEEN INTERVAL '30 minutes' PRECEDING AND CURRENT ROW),
    w_press AS (PARTITION BY night_date
                ORDER BY ts
                RANGE BETWEEN INTERVAL '5 minutes' PRECEDING AND CURRENT ROW),
    w_night AS (PARTITION BY night_date)
)
SELECT
  night_date, ts, setting, elapsed_min, body_avg_f, room_temp_f,
  body_30m_sd, press_5m_sd, bed_occupied_left,
  (action = 'override') AS is_override,
  (body_30m_sd IS NOT NULL
     AND body_30m_sd > body_p75_night * 0.5)  AS sig_body_sd_q4_approx,
  CASE
    WHEN action = 'override' THEN 'override'
    WHEN bed_occupied_left = false THEN 'empty'
    ELSE 'silent_or_proxy_pending_python'
  END AS coarse_source
FROM windowed
ORDER BY ts;

COMMENT ON VIEW v_discomfort_minutes_left IS
  'Coarse minute-level discomfort signals; the authoritative pipeline is '
  'PerfectlySnug/ml/discomfort_label.py + tools/build_discomfort_corpus.py.';
