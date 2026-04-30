-- Migration 005: add bed presence telemetry columns to controller_readings
-- Run as postgres: sudo -u postgres psql -d sleepdata -f 005_bed_presence_telemetry.sql

BEGIN;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'controller_readings' AND column_name = 'bed_left_pressure_pct'
    ) THEN
        ALTER TABLE controller_readings
            ADD COLUMN bed_left_pressure_pct real,
            ADD COLUMN bed_right_pressure_pct real,
            ADD COLUMN bed_left_calibrated_pressure_pct real,
            ADD COLUMN bed_right_calibrated_pressure_pct real,
            ADD COLUMN bed_left_unoccupied_pressure_pct real,
            ADD COLUMN bed_right_unoccupied_pressure_pct real,
            ADD COLUMN bed_left_occupied_pressure_pct real,
            ADD COLUMN bed_right_occupied_pressure_pct real,
            ADD COLUMN bed_left_trigger_pressure_pct real,
            ADD COLUMN bed_right_trigger_pressure_pct real,
            ADD COLUMN bed_occupied_left boolean,
            ADD COLUMN bed_occupied_right boolean,
            ADD COLUMN bed_occupied_either boolean,
            ADD COLUMN bed_occupied_both boolean;
        RAISE NOTICE 'Added controller_readings bed presence telemetry columns';
    ELSE
        RAISE NOTICE 'controller_readings bed presence telemetry columns already exist';
    END IF;
END $$;

GRANT ALL ON controller_readings TO sleepsync;

COMMIT;
