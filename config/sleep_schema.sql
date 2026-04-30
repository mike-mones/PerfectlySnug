-- Sleep segments: individual sleep stages from Apple Health / SleepSync
CREATE TABLE IF NOT EXISTS sleep_segments (
    id              BIGSERIAL PRIMARY KEY,
    night_date      DATE NOT NULL,
    start_ts        TIMESTAMPTZ NOT NULL,
    end_ts          TIMESTAMPTZ NOT NULL,
    stage           TEXT NOT NULL,
    duration_min    REAL NOT NULL,
    source          TEXT,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

-- Nightly summary: one row per night with topper settings and environmental data
CREATE TABLE IF NOT EXISTS nightly_summary (
    id              BIGSERIAL PRIMARY KEY,
    night_date      DATE NOT NULL UNIQUE,
    bedtime_ts      TIMESTAMPTZ,
    wake_ts         TIMESTAMPTZ,
    duration_hours  REAL,
    bedtime_setting INTEGER,
    sleep_setting   INTEGER,
    wake_setting    INTEGER,
    avg_ambient_f   REAL,
    min_ambient_f   REAL,
    max_ambient_f   REAL,
    avg_room_f      REAL,
    min_room_f      REAL,
    max_room_f      REAL,
    avg_body_f      REAL,
    override_count  INTEGER DEFAULT 0,
    manual_mode     BOOLEAN DEFAULT FALSE,
    controller_ver  TEXT DEFAULT 'v3',
    total_sleep_min REAL,
    deep_sleep_min  REAL,
    rem_sleep_min   REAL,
    core_sleep_min  REAL,
    awake_min       REAL,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

-- Unique constraint for upserts (ON CONFLICT in app.py)
CREATE UNIQUE INDEX IF NOT EXISTS idx_segments_upsert
    ON sleep_segments (night_date, stage, start_ts, COALESCE(source, ''));

-- General health metrics (HRV, resting HR, SpO2, etc.)
CREATE TABLE IF NOT EXISTS health_metrics (
    id              BIGSERIAL PRIMARY KEY,
    ts              TIMESTAMPTZ NOT NULL,
    metric_name     TEXT NOT NULL,
    value           REAL,
    value_min       REAL,
    value_max       REAL,
    units           TEXT,
    source          TEXT,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

-- Unique constraint for metric upserts
CREATE UNIQUE INDEX IF NOT EXISTS idx_metrics_upsert
    ON health_metrics (ts, metric_name, COALESCE(source, ''));

CREATE INDEX IF NOT EXISTS idx_segments_night ON sleep_segments(night_date);
CREATE INDEX IF NOT EXISTS idx_segments_stage ON sleep_segments(stage);
CREATE INDEX IF NOT EXISTS idx_segments_start ON sleep_segments(start_ts);
CREATE INDEX IF NOT EXISTS idx_summary_night ON nightly_summary(night_date);
CREATE INDEX IF NOT EXISTS idx_metrics_ts ON health_metrics(ts);
CREATE INDEX IF NOT EXISTS idx_metrics_name ON health_metrics(metric_name);

-- Controller readings: per-cycle (5-min) data from the live controller/logger
CREATE TABLE IF NOT EXISTS controller_readings (
    id              BIGSERIAL PRIMARY KEY,
    ts              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    zone            TEXT NOT NULL DEFAULT 'left',
    phase           TEXT,
    elapsed_min     REAL,
    body_right_f    REAL,
    body_center_f   REAL,
    body_left_f     REAL,
    body_avg_f      REAL,
    ambient_f       REAL,
    room_temp_f     REAL,
    setpoint_f      REAL,
    setting         INTEGER,
    effective       INTEGER,
    baseline        INTEGER,
    learned_adj     INTEGER,
    action          TEXT,
    override_delta  INTEGER,
    controller_version TEXT DEFAULT 'v3',
    notes           TEXT,
    bed_left_pressure_pct REAL,
    bed_right_pressure_pct REAL,
    bed_left_calibrated_pressure_pct REAL,
    bed_right_calibrated_pressure_pct REAL,
    bed_left_unoccupied_pressure_pct REAL,
    bed_right_unoccupied_pressure_pct REAL,
    bed_left_occupied_pressure_pct REAL,
    bed_right_occupied_pressure_pct REAL,
    bed_left_trigger_pressure_pct REAL,
    bed_right_trigger_pressure_pct REAL,
    bed_occupied_left BOOLEAN,
    bed_occupied_right BOOLEAN,
    bed_occupied_either BOOLEAN,
    bed_occupied_both BOOLEAN
);

CREATE INDEX IF NOT EXISTS idx_readings_ts ON controller_readings(ts);
CREATE INDEX IF NOT EXISTS idx_readings_phase ON controller_readings(phase);

GRANT ALL ON ALL TABLES IN SCHEMA public TO sleepsync;
GRANT ALL ON ALL SEQUENCES IN SCHEMA public TO sleepsync;
