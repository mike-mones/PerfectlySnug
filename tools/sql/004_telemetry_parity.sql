-- Migration 004: telemetry parity + nightly room summary
-- Run as postgres: sudo -u postgres psql -d sleepdata -f 004_telemetry_parity.sql

BEGIN;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'controller_readings' AND column_name = 'setpoint_f'
    ) THEN
        ALTER TABLE controller_readings ADD COLUMN setpoint_f real;
        COMMENT ON COLUMN controller_readings.setpoint_f
            IS 'Firmware temperature setpoint in °F (TempSP) at sample time';
        RAISE NOTICE 'Added controller_readings.setpoint_f';
    ELSE
        RAISE NOTICE 'controller_readings.setpoint_f already exists';
    END IF;
END $$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'nightly_summary' AND column_name = 'avg_room_f'
    ) THEN
        ALTER TABLE nightly_summary
            ADD COLUMN avg_room_f real,
            ADD COLUMN min_room_f real,
            ADD COLUMN max_room_f real;
        RAISE NOTICE 'Added nightly_summary room summary columns';
    ELSE
        RAISE NOTICE 'nightly_summary room summary columns already exist';
    END IF;
END $$;

GRANT ALL ON controller_readings TO sleepsync;
GRANT ALL ON nightly_summary TO sleepsync;

COMMIT;
