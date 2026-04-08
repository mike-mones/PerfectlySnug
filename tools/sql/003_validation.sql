-- PerfectlySnug Data Validation Queries
-- Run: PGPASSWORD=sleepsync_local psql -h 192.168.0.75 -U sleepsync -d sleepdata -f 003_validation.sql

-- ============================================================
-- 1. GAP DETECTION: Find missing 5-min intervals
-- ============================================================
\echo '=== Gap Detection (intervals > 10 min) ==='
WITH ordered AS (
    SELECT ts, zone,
           LAG(ts) OVER (PARTITION BY zone ORDER BY ts) AS prev_ts
    FROM controller_readings
),
gaps AS (
    SELECT ts, zone, prev_ts,
           EXTRACT(EPOCH FROM ts - prev_ts) / 60.0 AS gap_minutes
    FROM ordered
    WHERE prev_ts IS NOT NULL
)
SELECT zone, prev_ts AS gap_start, ts AS gap_end,
       ROUND(gap_minutes::numeric, 1) AS gap_minutes
FROM gaps WHERE gap_minutes > 10
ORDER BY gap_minutes DESC LIMIT 20;

\echo '=== Gap Summary ==='
WITH ordered AS (
    SELECT ts, zone,
           LAG(ts) OVER (PARTITION BY zone ORDER BY ts) AS prev_ts
    FROM controller_readings
),
gaps AS (
    SELECT zone, EXTRACT(EPOCH FROM ts - prev_ts) / 60.0 AS gap_minutes
    FROM ordered WHERE prev_ts IS NOT NULL
)
SELECT zone, COUNT(*) AS total_intervals,
       ROUND(AVG(gap_minutes)::numeric, 1) AS avg_gap_min,
       ROUND(MAX(gap_minutes)::numeric, 1) AS max_gap_min,
       COUNT(*) FILTER (WHERE gap_minutes > 10) AS gaps_over_10min,
       COUNT(*) FILTER (WHERE gap_minutes > 30) AS gaps_over_30min
FROM gaps GROUP BY zone;

-- ============================================================
-- 2. ANOMALOUS VALUES
-- ============================================================
\echo '=== Anomalous Body Temps (>100°F or <50°F) ==='
SELECT ts, zone, body_avg_f, body_center_f, phase, action
FROM controller_readings
WHERE body_avg_f > 100 OR (body_avg_f < 50 AND body_avg_f > 0)
   OR body_center_f > 100 OR (body_center_f < 50 AND body_center_f > 0)
ORDER BY ts;

\echo '=== Zero/Null Body Temp Count ==='
SELECT COUNT(*) AS zero_readings,
       ROUND(COUNT(*)::numeric / NULLIF((SELECT COUNT(*) FROM controller_readings), 0) * 100, 1) AS pct
FROM controller_readings WHERE body_avg_f = 0 OR body_avg_f IS NULL;

-- ============================================================
-- 3. NIGHTLY SUMMARY vs RAW READINGS
-- ============================================================
\echo '=== Nightly Summary vs Raw Data ==='
WITH raw_stats AS (
    SELECT
        CASE WHEN EXTRACT(HOUR FROM ts AT TIME ZONE 'America/New_York') >= 18
             THEN (ts AT TIME ZONE 'America/New_York')::date
             ELSE (ts AT TIME ZONE 'America/New_York')::date - 1
        END AS night_date,
        ROUND(AVG(NULLIF(body_avg_f, 0))::numeric, 1) AS raw_body_avg,
        COUNT(*) AS raw_count
    FROM controller_readings GROUP BY night_date
)
SELECT ns.night_date, ns.avg_body_f AS summary_avg, rs.raw_body_avg,
       ROUND((ns.avg_body_f - rs.raw_body_avg)::numeric, 2) AS diff,
       rs.raw_count
FROM nightly_summary ns
LEFT JOIN raw_stats rs ON ns.night_date = rs.night_date
ORDER BY ns.night_date;

-- ============================================================
-- 4. ROOM TEMP DISTRIBUTION
-- ============================================================
\echo '=== Room Temp Distribution ==='
SELECT FLOOR(room_temp_f / 2) * 2 || '-' || (FLOOR(room_temp_f / 2) * 2 + 2) || '°F' AS range,
       COUNT(*) AS readings,
       ROUND(COUNT(*)::numeric / SUM(COUNT(*)) OVER () * 100, 1) AS pct
FROM controller_readings
WHERE room_temp_f > 0 AND room_temp_f < 120
GROUP BY FLOOR(room_temp_f / 2)
ORDER BY FLOOR(room_temp_f / 2);
