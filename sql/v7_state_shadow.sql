-- v7 state-estimator shadow table — additive, reversible.
-- Spec: docs/proposals/2026-05-04_state_estimation.md §7.1
--
-- Deviation from spec: we use a dedicated `controller_state_shadow` table
-- (one row per zone per 60s tick) instead of ALTERing `controller_readings`.
-- Rationale:
--   - The live v5.2 controller writes to `controller_readings` at ~5min
--     cadence. The shadow logger ticks at 60s. UPDATE-by-key on a row the
--     controller wrote would be racy and would slow the controller's hot
--     path. INSERTing into a separate table is lock-free and lets the
--     shadow run at finer cadence than the live controller.
--   - When P3b is later promoted to in-controller state (P4+), the live
--     controller will start writing `controller_readings.state` directly
--     and this table can be deprecated.
--
-- Apply:    psql -f sql/v7_state_shadow.sql
-- Rollback: psql -f sql/v7_state_shadow_rollback.sql

BEGIN;

CREATE TABLE IF NOT EXISTS controller_state_shadow (
    id BIGSERIAL PRIMARY KEY,
    ts TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    zone VARCHAR(8) NOT NULL CHECK (zone IN ('left', 'right')),
    state VARCHAR(20) NOT NULL,
    state_confidence DOUBLE PRECISION,
    state_degraded VARCHAR(20),  -- NULL | 'movement' | 'body_validity' | 'both'
    trigger TEXT,
    -- Movement features (NULL if degraded='movement' or 'both')
    movement_rms_5min DOUBLE PRECISION,
    movement_rms_15min DOUBLE PRECISION,
    movement_var_15min DOUBLE PRECISION,
    movement_max_delta_60s DOUBLE PRECISION,
    -- Body / room features
    body_left_f DOUBLE PRECISION,
    room_temp_f DOUBLE PRECISION,
    body_trend_15min DOUBLE PRECISION,
    body_sensor_valid BOOLEAN,
    -- Presence
    presence_binary BOOLEAN,
    seconds_since_presence_change DOUBLE PRECISION,
    -- Audit
    estimator_version VARCHAR(32) NOT NULL DEFAULT 'p3a-v1',
    notes TEXT
);

CREATE INDEX IF NOT EXISTS idx_state_shadow_zone_ts
    ON controller_state_shadow (zone, ts DESC);
CREATE INDEX IF NOT EXISTS idx_state_shadow_state
    ON controller_state_shadow (state) WHERE state IS NOT NULL;

COMMIT;
