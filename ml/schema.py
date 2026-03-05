"""
Perfectly Snug ML Temperature Controller

Schema and training pipeline for learning optimal temperature settings
from user behavior (manual overrides) and sensor data.

Data Sources:
  - InfluxDB (via HA integration): body temps, ambient, PID, blower, settings
  - HA events: manual override events with delta and timestamp
  - (Future) Apple Health via Health Auto Export: HR, HRV, wrist temp, sleep stage

Model:
  - Gradient boosted trees (LightGBM) — fast, handles mixed feature types
  - Predicts: optimal temperature setting (0-20 scale) for each zone
  - Training signal: manual overrides = "the model was wrong by this much"

Inference:
  - Called every 60 seconds by HA automation or integration
  - Input: current sensor state + time features
  - Output: recommended setting value (0-20)
"""

# ─── Feature Schema ──────────────────────────────────────
# Each training sample is a snapshot at the time of a manual override

FEATURE_SCHEMA = {
    # Sensor features (from topper, every 30s)
    "body_sensor_right_f": "float",     # TSR in °F
    "body_sensor_center_f": "float",    # TSC in °F
    "body_sensor_left_f": "float",      # TSL in °F
    "body_sensor_avg_f": "float",       # Average of TSR/TSC/TSL
    "ambient_temp_f": "float",          # TA in °F
    "temp_setpoint_f": "float",         # Current setpoint in °F
    "body_minus_ambient": "float",      # body_avg - ambient (heat differential)
    "blower_output_pct": "float",       # Blower % (0-100)
    "pid_control_output": "float",      # PID output (signed)
    "pid_p_term": "float",             # PID proportional
    "pid_i_term": "float",             # PID integral
    
    # Rate of change features (computed from last N samples)
    "body_avg_delta_5m": "float",       # Body temp change over last 5 min
    "body_avg_delta_15m": "float",      # Body temp change over last 15 min
    "ambient_delta_15m": "float",       # Ambient change over last 15 min
    
    # Time features
    "minutes_since_bedtime": "float",   # Minutes since schedule start
    "hour_of_night": "int",            # 0-23 UTC hour
    "day_of_week": "int",             # 0=Mon, 6=Sun
    "is_weekend": "bool",
    
    # Current setting
    "current_setting": "int",           # Current L1 value (0-20)
    
    # Context
    "zone": "str",                      # "left" or "right"
    "responsive_cooling_on": "bool",
    "schedule_enabled": "bool",
    "profile_3level": "bool",
    
    # (Future) Apple Watch features
    # "heart_rate": "float",
    # "hrv": "float",
    # "wrist_temp_f": "float",
    # "sleep_stage": "str",  # awake/light/deep/rem
}

# Target: what the user actually wanted
TARGET_SCHEMA = {
    "desired_setting": "int",           # The value the user manually set (0-20)
    "override_delta": "int",            # How much they changed (+/- from current)
}

# ─── Data Collection Schema (InfluxDB) ───────────────────
# These measurements flow continuously from HA → InfluxDB

INFLUXDB_MEASUREMENTS = {
    "snug_body_temp": {
        "tags": ["zone", "sensor"],     # zone=left/right, sensor=tsr/tsc/tsl
        "fields": ["value_f", "value_c"],
    },
    "snug_ambient_temp": {
        "tags": ["zone"],
        "fields": ["value_f", "value_c"],
    },
    "snug_setpoint": {
        "tags": ["zone"],
        "fields": ["value_f"],
    },
    "snug_setting": {
        "tags": ["zone", "setting"],    # setting=l1/l2/l3/foot_warmer/etc
        "fields": ["value"],
    },
    "snug_pid": {
        "tags": ["zone", "component"],  # component=output/p_term/i_term
        "fields": ["value"],
    },
    "snug_output": {
        "tags": ["zone", "component"],  # component=blower/hh_out/fh_out
        "fields": ["value"],
    },
    "snug_override": {
        "tags": ["zone", "setting"],
        "fields": ["old_value", "new_value", "delta"],
    },
}
