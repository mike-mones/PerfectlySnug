"""RC Deep Physics: System Identification on PerfectlySnug topper RC firmware.

Approach: black-box system ID with control-theory lens.
"""
import json, numpy as np, pandas as pd
from pathlib import Path
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from scipy.optimize import minimize, differential_evolution
from scipy.signal import correlate
from sklearn.linear_model import Ridge, LinearRegression
from sklearn.metrics import mean_absolute_error, r2_score

DATA = "/tmp/snug_v2.json"
RC_ON_START = pd.Timestamp("2026-04-17 11:39:24", tz="UTC")
RC_ON_END   = pd.Timestamp("2026-04-30 20:35:30", tz="UTC")

def load():
    raw = json.load(open(DATA))
    series = {}
    for grp in raw:
        eid = grp[0]["entity_id"]
        rows = []
        for r in grp:
            try:
                v = float(r["state"])
            except Exception:
                v = np.nan
            t = pd.Timestamp(r.get("last_updated") or r["last_changed"])
            rows.append((t, v))
        s = pd.DataFrame(rows, columns=["t", eid]).set_index("t").sort_index()
        s = s[~s.index.duplicated(keep="last")]
        series[eid] = s[eid]
    # also load running/responsive_cooling switches as on/off
    for grp in raw:
        eid = grp[0]["entity_id"]
        if eid.startswith("switch."):
            rows = []
            for r in grp:
                v = 1.0 if r.get("state") == "on" else 0.0
                t = pd.Timestamp(r.get("last_updated") or r["last_changed"])
                rows.append((t, v))
            s = pd.DataFrame(rows, columns=["t", eid]).set_index("t").sort_index()
            s = s[~s.index.duplicated(keep="last")]
            series[eid] = s[eid]
    return series

def build_grid(series, freq="10s"):
    # Resample everything to common grid
    grid = pd.date_range(RC_ON_START, RC_ON_END, freq=freq, tz="UTC")
    df = pd.DataFrame(index=grid)
    for k, s in series.items():
        s2 = s.reindex(s.index.union(grid)).sort_index()
        if k.startswith("switch."):
            s2 = s2.ffill().fillna(0.0)
        else:
            s2 = s2.ffill().bfill()
        df[k] = s2.reindex(grid)
    # Filter RC-on AND running
    rc = df["switch.smart_topper_right_side_responsive_cooling"]
    run = df["switch.smart_topper_right_side_running"]
    mask = (rc > 0.5) & (run > 0.5)
    df = df[mask].copy()
    # rename short
    df = df.rename(columns={
        "sensor.smart_topper_right_side_blower_output": "blower",
        "sensor.smart_topper_right_side_body_sensor_left":   "body_l",
        "sensor.smart_topper_right_side_body_sensor_center": "body_c",
        "sensor.smart_topper_right_side_body_sensor_right":  "body_r",
        "sensor.smart_topper_right_side_ambient_temperature":"ambient",
        "sensor.smart_topper_right_side_temperature_setpoint":"setpoint",
        "number.smart_topper_right_side_bedtime_temperature":"sp_user",
        "sensor.bed_presence_2bcab8_right_pressure":"pressure",
    })
    df["body_max"] = df[["body_l","body_c","body_r"]].max(axis=1)
    df["body_avg"] = df[["body_l","body_c","body_r"]].mean(axis=1)
    return df

if __name__ == "__main__":
    series = load()
    df = build_grid(series, "10s")
    print("Filtered samples:", len(df))
    print(df[["blower","body_l","body_c","body_r","body_max","body_avg","ambient","setpoint","sp_user","pressure"]].describe())
    df.to_pickle("/tmp/rc_grid.pkl")
    print("saved /tmp/rc_grid.pkl")

# =============================================================================
# Stage 2 controller (P + rate FF + ambient FF + Hammerstein output)
# =============================================================================
def controller(body_max, body_avg, ambient, setpoint, params, dt=10.0):
    Kp_max, Kp_avg, Kff_amb, Krise_max, Krise_avg, bias, off_thresh, min_on = params
    err_max = body_max - setpoint
    err_avg = body_avg - setpoint
    err_amb = body_avg - ambient
    d_max = np.r_[0, np.diff(body_max)] / dt
    d_avg = np.r_[0, np.diff(body_avg)] / dt
    target = (bias + Kp_max*err_max + Kp_avg*err_avg + Kff_amb*err_amb
              + Krise_max*np.maximum(0, d_max)*60.0
              + Krise_avg*np.maximum(0, d_avg)*60.0)
    return np.where(target < off_thresh, 0.0,
                    np.clip(np.maximum(min_on, target), 0.0, 100.0))

# Stage 1 setpoint generator (leaky max-hold with cap)
def gen_setpoint(body_max, cap, leak_per_step):
    out = np.empty_like(body_max)
    out[0] = min(body_max[0], cap)
    for i in range(1, len(body_max)):
        out[i] = min(cap, max(body_max[i], out[i-1] - leak_per_step))
    return out

# Identified parameters (5-fold TS-CV mean)
PARAMS_2026_05_01 = dict(
    Kp_max=19.094, Kp_avg=1.439, Kff_amb=-1.452,
    Krise_max=0.956, Krise_avg=0.460,
    bias=46.767, off_thresh=16.394, min_on=13.238,
)
SETPOINT_LEAK_PER_10S = 0.002  # °F per 10 s
CAP_RULE = lambda sp_user: 0.337*sp_user + 88.59  # approximate; °F
