-- 2026-05-04 — Evaluation framework: per-night metric columns on v6_nightly_summary.
-- See docs/proposals/2026-05-04_evaluation.md §2.
--
-- Pure additive, idempotent, reversible via sql/v6_eval_metrics_rollback.sql.
-- No existing column or constraint is dropped, modified, or re-typed.
-- Safe to run repeatedly.
--
-- Apply with:
--   PGPASSWORD=sleepsync_local psql -h 192.168.0.3 -U sleepsync -d sleepdata \
--     -f sql/v6_eval_metrics.sql
BEGIN;

ALTER TABLE v6_nightly_summary
    -- Identity / window (additive metadata)
    ADD COLUMN IF NOT EXISTS user_id                      TEXT,
    ADD COLUMN IF NOT EXISTS bed_onset_ts                 TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS wake_ts                      TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS in_bed_minutes               INTEGER,

    -- §1.1 Discomfort proxy (lower = better)
    ADD COLUMN IF NOT EXISTS adj_count_per_night          INTEGER,
    ADD COLUMN IF NOT EXISTS adj_magnitude_sum            INTEGER,
    ADD COLUMN IF NOT EXISTS adj_weighted_score           DOUBLE PRECISION,

    -- §1.2 Stability (controller-driven only)
    ADD COLUMN IF NOT EXISTS oscillation_count            INTEGER,
    ADD COLUMN IF NOT EXISTS overcorrection_rate          DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS setting_total_variation      INTEGER,

    -- §1.3 Responsiveness
    ADD COLUMN IF NOT EXISTS discomfort_event_count       INTEGER,
    ADD COLUMN IF NOT EXISTS time_to_correct_median_min   DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS unaddressed_discomfort_min   INTEGER,

    -- §1.4 Comfort outcomes (target = 80 °F per BODY_FB_TARGET_F)
    ADD COLUMN IF NOT EXISTS body_in_target_band_pct      DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS cold_minutes                 INTEGER,
    ADD COLUMN IF NOT EXISTS warm_minutes                 INTEGER,

    -- Audit trail
    ADD COLUMN IF NOT EXISTS metrics_target_f             DOUBLE PRECISION DEFAULT 80.0,
    ADD COLUMN IF NOT EXISTS metrics_computed_at          TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS metrics_source_commit        TEXT,
    ADD COLUMN IF NOT EXISTS metrics_schema_version       TEXT;

CREATE INDEX IF NOT EXISTS idx_v6_nightly_user_night
    ON v6_nightly_summary (user_id, night DESC);
CREATE INDEX IF NOT EXISTS idx_v6_nightly_ver_night
    ON v6_nightly_summary (controller_version, night DESC);

COMMIT;
