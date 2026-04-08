-- Migration 001: Schema updates for PerfectlySnug controller v4
-- Run as postgres: sudo -u postgres psql -d sleepdata -f 001_v4_schema.sql

BEGIN;

-- Add controller_version column to distinguish v3 vs v4 data
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'controller_readings' AND column_name = 'controller_version'
    ) THEN
        ALTER TABLE controller_readings ADD COLUMN controller_version text DEFAULT 'v3';
        COMMENT ON COLUMN controller_readings.controller_version
            IS 'Controller version that generated this reading (v3, v4)';
        RAISE NOTICE 'Added controller_version column';
    ELSE
        RAISE NOTICE 'controller_version column already exists';
    END IF;
END $$;

-- Composite index for efficient time-range queries per zone
CREATE INDEX IF NOT EXISTS idx_readings_ts_zone
    ON controller_readings (ts, zone);

-- Zone-first composite for zone-scoped lookups
CREATE INDEX IF NOT EXISTS idx_readings_zone_ts
    ON controller_readings (zone, ts);

-- Version filter index
CREATE INDEX IF NOT EXISTS idx_readings_version
    ON controller_readings (controller_version);

-- Grant full access to sleepsync for the new column
GRANT ALL ON controller_readings TO sleepsync;
GRANT USAGE, SELECT ON SEQUENCE controller_readings_id_seq TO sleepsync;

COMMIT;

-- Verify
\d controller_readings
