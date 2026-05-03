-- Round 1A v6 schema EMERGENCY rollback. Inverse of v6_schema.sql.
-- Drops every column/table/index added by the migration.
-- Safe even if migration was only partially applied (uses IF EXISTS).
BEGIN;

DROP TABLE IF EXISTS v6_nightly_summary;

DROP INDEX IF EXISTS idx_pressure_movement_zone_ts;
DROP TABLE IF EXISTS controller_pressure_movement;

DROP INDEX IF EXISTS idx_controller_readings_zone_ts;
DROP INDEX IF EXISTS idx_controller_readings_regime;

ALTER TABLE controller_readings
    DROP COLUMN IF EXISTS actual_blower_pct_typed,
    DROP COLUMN IF EXISTS right_rail_engaged,
    DROP COLUMN IF EXISTS three_level_off,
    DROP COLUMN IF EXISTS l_active_dial,
    DROP COLUMN IF EXISTS mins_since_onset,
    DROP COLUMN IF EXISTS post_bedjet_min,
    DROP COLUMN IF EXISTS movement_density_15m,
    DROP COLUMN IF EXISTS bedjet_active,
    DROP COLUMN IF EXISTS plant_predicted_setpoint_f,
    DROP COLUMN IF EXISTS divergence_steps,
    DROP COLUMN IF EXISTS residual_lcb,
    DROP COLUMN IF EXISTS residual_n_support,
    DROP COLUMN IF EXISTS residual,
    DROP COLUMN IF EXISTS regime_reason,
    DROP COLUMN IF EXISTS regime;

COMMIT;
