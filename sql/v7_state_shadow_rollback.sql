-- Rollback for sql/v7_state_shadow.sql
BEGIN;
DROP INDEX IF EXISTS idx_state_shadow_state;
DROP INDEX IF EXISTS idx_state_shadow_zone_ts;
DROP TABLE IF EXISTS controller_state_shadow;
COMMIT;
