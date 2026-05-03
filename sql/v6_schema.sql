-- Round 1A v6 schema migration. Pure additive. Reversible via DROP COLUMN/TABLE.
BEGIN;

-- 1) Add v6 fields to controller_readings (NULL by default; v5.2 won't touch them)
ALTER TABLE controller_readings
    ADD COLUMN IF NOT EXISTS regime VARCHAR(32),
    ADD COLUMN IF NOT EXISTS regime_reason VARCHAR(96),
    ADD COLUMN IF NOT EXISTS residual INTEGER,
    ADD COLUMN IF NOT EXISTS residual_n_support INTEGER,
    ADD COLUMN IF NOT EXISTS residual_lcb DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS divergence_steps INTEGER,
    ADD COLUMN IF NOT EXISTS plant_predicted_setpoint_f DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS bedjet_active BOOLEAN,
    ADD COLUMN IF NOT EXISTS movement_density_15m DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS post_bedjet_min DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS mins_since_onset DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS l_active_dial INTEGER,
    ADD COLUMN IF NOT EXISTS three_level_off BOOLEAN,
    ADD COLUMN IF NOT EXISTS right_rail_engaged BOOLEAN,
    ADD COLUMN IF NOT EXISTS actual_blower_pct_typed INTEGER;

-- 2) Indexes for common v6 queries
CREATE INDEX IF NOT EXISTS idx_controller_readings_regime
    ON controller_readings (regime) WHERE regime IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_controller_readings_zone_ts
    ON controller_readings (zone, ts DESC);

-- 3) New table for high-resolution movement aggregates
CREATE TABLE IF NOT EXISTS controller_pressure_movement (
    id BIGSERIAL PRIMARY KEY,
    zone VARCHAR(8) NOT NULL CHECK (zone IN ('left', 'right')),
    ts TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    abs_delta_sum_60s DOUBLE PRECISION,
    max_delta_60s DOUBLE PRECISION,
    sample_count INTEGER,
    occupied BOOLEAN
);
CREATE INDEX IF NOT EXISTS idx_pressure_movement_zone_ts
    ON controller_pressure_movement (zone, ts DESC);

-- 4) New table for v6 nightly summaries (separate from existing nightly_summary)
CREATE TABLE IF NOT EXISTS v6_nightly_summary (
    id BIGSERIAL PRIMARY KEY,
    night DATE NOT NULL,
    zone VARCHAR(8) NOT NULL,
    controller_version VARCHAR(64),
    regime_histogram JSONB,
    override_count INTEGER,
    minutes_above_86f INTEGER,
    minutes_above_84f INTEGER,
    minutes_below_72f INTEGER,
    rail_engagements INTEGER,
    fallback_events INTEGER,
    divergence_guard_activations INTEGER,
    proxy_minutes_score_ge_05 DOUBLE PRECISION,
    notes JSONB,
    UNIQUE (night, zone, controller_version)
);

COMMIT;
