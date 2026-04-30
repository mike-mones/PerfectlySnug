"""Test: do body-temp / pressure variability signals correlate with discomfort?

Hypothesis: a window of high body-temp variance precedes overrides AND
distinguishes 'comfortable settings' from 'uncomfortable settings' within a
night, even when no override is fired.
"""
import sys, pathlib, subprocess, io
sys.path.insert(0, '.')
import numpy as np
import pandas as pd
from scipy import stats

# Direct PG query so we get raw pressure and body sub-zone columns
SSH_HOST = "macmini"
SQL = """
SELECT ts, body_left_f, body_center_f, body_right_f, body_avg_f,
       ambient_f, room_temp_f, setting, action, override_delta,
       bed_left_pressure_pct, bed_left_calibrated_pressure_pct,
       bed_occupied_left, elapsed_min
FROM controller_readings
WHERE controller_version='v5_rc_off' AND zone='left' AND bed_occupied_left=true
ORDER BY ts
"""
cmd = (f'PGPASSWORD=sleepsync_local psql -U sleepsync -h localhost '
       f'-d sleepdata --csv --pset=footer=off -c "{SQL}"')
res = subprocess.run(["ssh", SSH_HOST, cmd], capture_output=True, text=True, timeout=120)
rd = pd.read_csv(io.StringIO(res.stdout))
rd["ts"] = pd.to_datetime(rd["ts"], format="ISO8601", utc=True)
rd = rd.sort_values("ts").reset_index(drop=True)
rd["night"] = (rd["ts"] - pd.Timedelta(hours=6)).dt.date

print(f"Loaded {len(rd)} occupied-bed readings\n")

# 1. Per-night correlation
per_night = rd.groupby("night").agg(
    n=("ts", "size"),
    body_sd=("body_left_f", "std"),
    body_range=("body_left_f", lambda s: s.max() - s.min()),
    pressure_sd=("bed_left_pressure_pct", "std"),
    pressure_range=("bed_left_pressure_pct", lambda s: s.max() - s.min()),
    overrides=("action", lambda s: (s == "override").sum()),
    avg_setting=("setting", "mean"),
    avg_room=("room_temp_f", "mean"),
).dropna()
print("Per-night summary:")
print(per_night.round(2).to_string())

print("\nPearson correlations with override count (per-night, n=14):")
for col in ["body_sd", "body_range", "pressure_sd", "pressure_range", "avg_room"]:
    r, p = stats.pearsonr(per_night[col], per_night["overrides"])
    print(f"  {col:<16}  r={r:+.3f}  p={p:.3f}")

# 2. Predictive: rolling body 30-min sd predicts override-soon (within 30 min)
print("\n=== Predictive: body_30m_sd predicts override-in-next-30min ===")
all_rows = []
for night, g in rd.groupby("night"):
    g = g.sort_values("ts").reset_index(drop=True)
    if g["body_left_f"].notna().sum() < 10:
        continue
    g["body_30m_sd"] = g["body_left_f"].rolling(window=6, min_periods=3).std()
    g["body_30m_range"] = g["body_left_f"].rolling(window=6, min_periods=3).apply(
        lambda x: x.max() - x.min(), raw=True)
    g["pressure_30m_sd"] = g["bed_left_pressure_pct"].rolling(window=6, min_periods=3).std()
    is_override = (g["action"] == "override").values
    overrides_ahead = np.zeros(len(g), dtype=int)
    for i in range(len(g)):
        end = min(i + 7, len(g))
        if is_override[i+1:end].any():
            overrides_ahead[i] = 1
    g["override_in_next_30m"] = overrides_ahead
    all_rows.append(g)
all_df = pd.concat(all_rows, ignore_index=True)
all_df = all_df.dropna(subset=["body_30m_sd"])
print(f"N rows: {len(all_df)}")
print(f"Override-soon base rate: {all_df['override_in_next_30m'].mean():.1%}")
soon = all_df[all_df["override_in_next_30m"] == 1]
not_soon = all_df[all_df["override_in_next_30m"] == 0]
print(f"\nbody_30m_sd:   not-soon={not_soon['body_30m_sd'].mean():.2f}  soon={soon['body_30m_sd'].mean():.2f}")
print(f"body_30m_rng:  not-soon={not_soon['body_30m_range'].mean():.2f}  soon={soon['body_30m_range'].mean():.2f}")
print(f"press_30m_sd:  not-soon={not_soon['pressure_30m_sd'].mean():.3f}  soon={soon['pressure_30m_sd'].mean():.3f}")

# Mann-Whitney U (non-parametric)
from scipy.stats import mannwhitneyu
for col in ["body_30m_sd", "body_30m_range", "pressure_30m_sd"]:
    a = soon[col].dropna()
    b = not_soon[col].dropna()
    if len(a) > 5 and len(b) > 5:
        stat, p = mannwhitneyu(a, b, alternative="greater")
        print(f"  {col:<18} U-test (soon > not_soon)  p={p:.4f}")

# Quartile breakdown
print("\nOverride-soon rate by body_30m_sd quartile:")
all_df["sd_q"] = pd.qcut(all_df["body_30m_sd"], 4, duplicates="drop",
                          labels=["Q1_low", "Q2", "Q3", "Q4_high"])
print(all_df.groupby("sd_q", observed=True)["override_in_next_30m"]
      .agg(["count", "mean"]).round(3).to_string())
