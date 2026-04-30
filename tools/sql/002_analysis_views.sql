-- PerfectlySnug Analysis Views
-- Run as sleepsync: PGPASSWORD=sleepsync_local psql -h 192.168.0.75 -U sleepsync -d sleepdata -f 002_analysis_views.sql
--
-- These views are already applied to the database. Re-run this file to update them.

-- ============================================================
-- v_overnight_summary: Per-night stats with temp, settings, overrides
-- ============================================================
CREATE OR REPLACE VIEW v_overnight_summary AS
WITH night_bounds AS (
    SELECT
        CASE
            WHEN EXTRACT(HOUR FROM ts AT TIME ZONE 'America/New_York') >= 18
            THEN (ts AT TIME ZONE 'America/New_York')::date
            ELSE (ts AT TIME ZONE 'America/New_York')::date - 1
        END AS night_date,
        *
    FROM controller_readings
),
night_stats AS (
    SELECT
        night_date,
        zone,
        STRING_AGG(
            DISTINCT COALESCE(controller_version, 'unknown'),
            ',' ORDER BY COALESCE(controller_version, 'unknown')
        ) AS controller_version,
        MIN(ts) AS first_reading,
        MAX(ts) AS last_reading,
        EXTRACT(EPOCH FROM MAX(ts) - MIN(ts)) / 3600.0 AS duration_hours,
        COUNT(*) AS reading_count,
        ROUND(AVG(body_avg_f)::numeric, 1) AS body_avg,
        ROUND(MIN(NULLIF(body_avg_f, 0))::numeric, 1) AS body_min,
        ROUND(MAX(body_avg_f)::numeric, 1) AS body_max,
        ROUND(STDDEV(NULLIF(body_avg_f, 0))::numeric, 2) AS body_stdev,
        ROUND(AVG(room_temp_f)::numeric, 1) AS room_avg,
        ROUND(MIN(NULLIF(room_temp_f, 0))::numeric, 1) AS room_min,
        ROUND(MAX(room_temp_f)::numeric, 1) AS room_max,
        ROUND(STDDEV(NULLIF(room_temp_f, 0))::numeric, 2) AS room_stdev,
        ROUND(AVG(setting)::numeric, 1) AS avg_setting,
        MIN(setting) AS min_setting,
        MAX(setting) AS max_setting,
        COUNT(DISTINCT setting) AS distinct_settings,
        COUNT(*) FILTER (WHERE action = 'override') AS override_count,
        COUNT(*) FILTER (WHERE action = 'deadband') AS deadband_count,
        COUNT(*) FILTER (WHERE phase = 'deep') AS deep_readings,
        COUNT(*) FILTER (WHERE phase = 'rem') AS rem_readings,
        COUNT(*) FILTER (WHERE phase = 'bedtime') AS bedtime_readings,
        ARRAY_AGG(DISTINCT phase ORDER BY phase) AS phases_seen,
        ARRAY_AGG(DISTINCT action ORDER BY action) AS actions_seen
    FROM night_bounds
    GROUP BY night_date, zone
)
SELECT
    ns.*,
    nsm.total_sleep_min,
    nsm.deep_sleep_min,
    nsm.rem_sleep_min,
    nsm.core_sleep_min,
    nsm.awake_min
FROM night_stats ns
LEFT JOIN nightly_summary nsm ON ns.night_date = nsm.night_date
ORDER BY ns.night_date DESC, ns.zone;


-- ============================================================
-- v_setting_timeline: L1 setting changes with source attribution
-- ============================================================
CREATE OR REPLACE VIEW v_setting_timeline AS
WITH reading_with_prev AS (
    SELECT
        CASE
            WHEN EXTRACT(HOUR FROM ts AT TIME ZONE 'America/New_York') >= 18
            THEN (ts AT TIME ZONE 'America/New_York')::date
            ELSE (ts AT TIME ZONE 'America/New_York')::date - 1
        END AS night_date,
        ts, zone, phase, action, setting, effective, baseline,
        learned_adj, override_delta, body_avg_f, room_temp_f, setpoint_f,
        controller_version,
        LAG(setting) OVER (PARTITION BY zone ORDER BY ts) AS prev_setting,
        LAG(effective) OVER (PARTITION BY zone ORDER BY ts) AS prev_effective,
        LAG(action) OVER (PARTITION BY zone ORDER BY ts) AS prev_action
    FROM controller_readings
)
SELECT
    night_date, ts, zone, phase, action, setting, effective, prev_setting,
    setting - COALESCE(prev_setting, setting) AS setting_delta,
    CASE
        WHEN action = 'override' THEN 'user_override'
        WHEN action = 'passive' THEN 'passive_snapshot'
        WHEN action IN ('set', 'hold', 'rate_hold', 'freeze_hold', 'manual_hold') THEN 'controller_auto'
        WHEN action = 'empty_bed' THEN 'empty_bed'
        ELSE COALESCE(action, 'unknown')
    END AS change_source,
    baseline, learned_adj, override_delta,
    body_avg_f, room_temp_f, setpoint_f, controller_version
FROM reading_with_prev
WHERE setting IS DISTINCT FROM prev_setting
   OR action IS DISTINCT FROM prev_action
   OR prev_setting IS NULL
ORDER BY ts;


-- ============================================================
-- v_body_temp_stability: Hourly body temp stability ratings
-- ============================================================
CREATE OR REPLACE VIEW v_body_temp_stability AS
SELECT
    CASE
        WHEN EXTRACT(HOUR FROM ts AT TIME ZONE 'America/New_York') >= 18
        THEN (ts AT TIME ZONE 'America/New_York')::date
        ELSE (ts AT TIME ZONE 'America/New_York')::date - 1
    END AS night_date,
    zone,
    EXTRACT(HOUR FROM ts AT TIME ZONE 'America/New_York')::int AS hour_local,
    TO_CHAR(
        DATE_TRUNC('hour', ts AT TIME ZONE 'America/New_York'),
        'HH12 AM'
    ) AS hour_label,
    COUNT(*) AS readings,
    ROUND(AVG(NULLIF(body_avg_f, 0))::numeric, 1) AS avg_body_f,
    ROUND(MIN(NULLIF(body_avg_f, 0))::numeric, 1) AS min_body_f,
    ROUND(MAX(NULLIF(body_avg_f, 0))::numeric, 1) AS max_body_f,
    ROUND(STDDEV(NULLIF(body_avg_f, 0))::numeric, 2) AS stdev_body_f,
    ROUND((MAX(NULLIF(body_avg_f, 0)) - MIN(NULLIF(body_avg_f, 0)))::numeric, 1) AS range_body_f,
    CASE
        WHEN STDDEV(NULLIF(body_avg_f, 0)) IS NULL THEN 'insufficient_data'
        WHEN STDDEV(NULLIF(body_avg_f, 0)) < 1.0 THEN 'excellent'
        WHEN STDDEV(NULLIF(body_avg_f, 0)) < 2.0 THEN 'good'
        WHEN STDDEV(NULLIF(body_avg_f, 0)) < 3.0 THEN 'fair'
        ELSE 'poor'
    END AS stability_rating,
    ROUND(AVG(setting)::numeric, 1) AS avg_setting,
    ROUND(AVG(room_temp_f)::numeric, 1) AS avg_room_f
FROM controller_readings
WHERE body_avg_f > 0
GROUP BY night_date, zone,
    EXTRACT(HOUR FROM ts AT TIME ZONE 'America/New_York'),
    TO_CHAR(DATE_TRUNC('hour', ts AT TIME ZONE 'America/New_York'), 'HH12 AM')
ORDER BY night_date DESC, hour_local;


-- ============================================================
-- v_room_temp_vs_setting: Ambient compensation validation
-- ============================================================
CREATE OR REPLACE VIEW v_room_temp_vs_setting AS
WITH bucketed AS (
    SELECT
        CASE
            WHEN EXTRACT(HOUR FROM ts AT TIME ZONE 'America/New_York') >= 18
            THEN (ts AT TIME ZONE 'America/New_York')::date
            ELSE (ts AT TIME ZONE 'America/New_York')::date - 1
        END AS night_date,
        zone,
        FLOOR(room_temp_f / 2) * 2 AS room_temp_band,
        setting, effective, body_avg_f, action, controller_version
    FROM controller_readings
    WHERE room_temp_f > 0 AND room_temp_f < 120
)
SELECT
    night_date, zone, room_temp_band,
    room_temp_band || '-' || (room_temp_band + 2) || '°F' AS room_temp_range,
    COUNT(*) AS readings,
    ROUND(AVG(setting)::numeric, 1) AS avg_setting,
    ROUND(AVG(effective)::numeric, 1) AS avg_effective,
    ROUND(AVG(NULLIF(body_avg_f, 0))::numeric, 1) AS avg_body_f,
    ROUND(STDDEV(NULLIF(body_avg_f, 0))::numeric, 2) AS body_stdev,
    CASE
        WHEN STDDEV(NULLIF(body_avg_f, 0)) IS NULL THEN 'no_data'
        WHEN STDDEV(NULLIF(body_avg_f, 0)) < 1.5 THEN 'well_compensated'
        WHEN STDDEV(NULLIF(body_avg_f, 0)) < 3.0 THEN 'adequate'
        ELSE 'needs_tuning'
    END AS compensation_quality
FROM bucketed
GROUP BY night_date, zone, room_temp_band
HAVING COUNT(*) >= 2
ORDER BY night_date DESC, room_temp_band;
