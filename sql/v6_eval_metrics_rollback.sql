-- 2026-05-04 — Rollback for sql/v6_eval_metrics.sql.
-- Symmetric DROP COLUMN IF EXISTS. Safe to run even if the migration was never applied.
BEGIN;

DROP INDEX IF EXISTS idx_v6_nightly_ver_night;
DROP INDEX IF EXISTS idx_v6_nightly_user_night;

ALTER TABLE v6_nightly_summary
    DROP COLUMN IF EXISTS metrics_schema_version,
    DROP COLUMN IF EXISTS metrics_source_commit,
    DROP COLUMN IF EXISTS metrics_computed_at,
    DROP COLUMN IF EXISTS metrics_target_f,
    DROP COLUMN IF EXISTS warm_minutes,
    DROP COLUMN IF EXISTS cold_minutes,
    DROP COLUMN IF EXISTS body_in_target_band_pct,
    DROP COLUMN IF EXISTS unaddressed_discomfort_min,
    DROP COLUMN IF EXISTS time_to_correct_median_min,
    DROP COLUMN IF EXISTS discomfort_event_count,
    DROP COLUMN IF EXISTS setting_total_variation,
    DROP COLUMN IF EXISTS overcorrection_rate,
    DROP COLUMN IF EXISTS oscillation_count,
    DROP COLUMN IF EXISTS adj_weighted_score,
    DROP COLUMN IF EXISTS adj_magnitude_sum,
    DROP COLUMN IF EXISTS adj_count_per_night,
    DROP COLUMN IF EXISTS in_bed_minutes,
    DROP COLUMN IF EXISTS wake_ts,
    DROP COLUMN IF EXISTS bed_onset_ts,
    DROP COLUMN IF EXISTS user_id;

COMMIT;
