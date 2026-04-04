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

CREATE INDEX IF NOT EXISTS idx_segments_night ON sleep_segments(night_date);
CREATE INDEX IF NOT EXISTS idx_segments_stage ON sleep_segments(stage);
CREATE INDEX IF NOT EXISTS idx_segments_start ON sleep_segments(start_ts);
CREATE INDEX IF NOT EXISTS idx_summary_night ON nightly_summary(night_date);

GRANT ALL ON ALL TABLES IN SCHEMA public TO sleepsync;
GRANT ALL ON ALL SEQUENCES IN SCHEMA public TO sleepsync;
