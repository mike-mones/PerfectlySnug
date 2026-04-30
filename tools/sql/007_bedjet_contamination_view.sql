-- Migration 007: BedJet contamination filter view (right zone)
--
-- Background:
--   The wife uses a BedJet on heat for the first ~30 min of sleep to pre-warm
--   the sheets while the topper cools. The BedJet inflates right-zone body
--   sensor readings to 90-99°F, which would corrupt any percentile/baseline
--   analysis of the right zone if not filtered out.
--
-- Output:
--   Adds two columns derived per row of controller_readings:
--     - minutes_since_onset:  minutes since the first occupied right-bed reading
--                             on that night (NULL for left zone or pre-onset)
--     - body_right_valid:     TRUE iff the right-zone body reading is trustworthy
--                             (i.e. body_avg_f < 88°F, OR > 30 min since onset)
--
-- Use:
--   SELECT * FROM v_body_right_valid WHERE zone='right' AND body_right_valid;
--
-- This is the SQL counterpart to ml/contamination.py. The Python module is
-- the authoritative implementation (used by controller and tests); this view
-- mirrors it for ad-hoc psql querying. Keep them in sync.
--
-- Run as sleepsync:
--   PGPASSWORD=sleepsync_local psql -U sleepsync -h localhost -d sleepdata \
--     -f tools/sql/007_bedjet_contamination_view.sql

BEGIN;

CREATE OR REPLACE VIEW v_body_right_valid AS
WITH night AS (
  SELECT *,
    CASE
      WHEN EXTRACT(HOUR FROM ts AT TIME ZONE 'America/New_York') >= 18
      THEN (ts AT TIME ZONE 'America/New_York')::date
      ELSE (ts AT TIME ZONE 'America/New_York')::date - 1
    END AS night_date
  FROM controller_readings
),
right_onset AS (
  SELECT
    night_date,
    MIN(ts) FILTER (WHERE bed_right_pressure_pct > 5) AS onset_ts
  FROM night
  WHERE zone = 'right'
  GROUP BY night_date
)
SELECT
  n.*,
  CASE
    WHEN n.zone = 'right' AND ro.onset_ts IS NOT NULL
    THEN EXTRACT(EPOCH FROM (n.ts - ro.onset_ts)) / 60.0
    ELSE NULL
  END AS minutes_since_onset,
  CASE
    -- Left zone: always trust (no BedJet)
    WHEN n.zone = 'left' THEN TRUE
    -- Right zone: missing body reading is invalid
    WHEN n.body_avg_f IS NULL THEN FALSE
    -- Right zone: any reading below the natural ceiling is fine
    WHEN n.body_avg_f < 88.0 THEN TRUE
    -- Right zone, ≥88°F: only valid if we know we're past the BedJet window
    WHEN ro.onset_ts IS NULL THEN FALSE
    WHEN EXTRACT(EPOCH FROM (n.ts - ro.onset_ts)) / 60.0 > 30.0 THEN TRUE
    ELSE FALSE
  END AS body_right_valid
FROM night n
LEFT JOIN right_onset ro USING (night_date);

COMMENT ON VIEW v_body_right_valid IS
  'Tags right-zone body readings inflated by BedJet warm-blanket use as invalid '
  '(body_right_valid=false) for the first 30 min after right-bed occupancy onset. '
  'Mirror of ml/contamination.py. Required filter for any right-zone baseline '
  'fitting or percentile analysis.';

COMMIT;
