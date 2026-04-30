"""
Feature engineering and label generation for the ML controller (Phase 1).

The model predicts a *bounded offset* from a "smart baseline" that already
encodes the parts of v5 that work (cycle schedule + room-temperature
compensation). The model only has to learn the residual:

    smart_baseline = cycle_baseline[cycle] + room_comp(room_temp)
    final_setting  = smart_baseline + model.predict(features)

Anchoring to a stronger baseline (rather than bare cycle baselines) means
the model is solving an easier problem — small corrections instead of
20-percentage-points of structural learning. The PRD §4.2 calls for
"offsets from user comfort, not wholesale overrides"; this is a faithful
implementation of that pattern.
"""
from __future__ import annotations

import math
from typing import Optional

# Optional heavy deps: only needed for build_features/build_labels (offline
# training). smart_baseline() — the only function imported by ml.policy and
# the live AppDaemon controller — works without them. Importing this module
# in environments without numpy/pandas (e.g. Home Assistant AppDaemon) must
# not raise.
try:
    import numpy as np  # type: ignore
    import pandas as pd  # type: ignore
except ImportError:
    np = None  # type: ignore
    pd = None  # type: ignore


# Cycle baselines mirror PerfectlySnug/appdaemon/sleep_controller_v5.py:55
CYCLE_BASELINES = {1: -10, 2: -9, 3: -8, 4: -7, 5: -6, 6: -5}
CYCLE_DURATION_MIN = 90
TYPICAL_NIGHT_MIN = 8 * 60

# v5 room-comp constants (sleep_controller_v5.py:85-89)
ROOM_REF_F = 68.0
ROOM_COLD_PER_F = 4.0          # blower % per °F when room < ref
ROOM_COLD_THRESH_F = 63.0
ROOM_COLD_EXTRA_PER_F = 3.0    # extra cold-comp below threshold
ROOM_HOT_PER_F = 4.0           # blower % per °F when room > ref

# L1 ↔ blower% map (sleep_controller_v5.py:70-82)
L1_TO_BLOWER = {-10: 100, -9: 87, -8: 75, -7: 65, -6: 50,
                -5: 41, -4: 33, -3: 26, -2: 20, -1: 10, 0: 0}

# Predicted offset is clamped to this range (PRD §4.2)
OFFSET_CLAMP = (-5, 5)
SETTING_CLAMP = (-10, 0)


# ── Fitted baselines (optional, produced by tools/fit_baselines.py) ──
# When ml/state/fitted_baselines.json exists, smart_baseline() uses
# data-fitted cycle settings + per-band L1 adjustments instead of v5's
# hand-picked constants. Falls back to v5 constants if file is absent
# or malformed so this module remains usable in environments without
# fitted state.
import json as _json
import os as _os

_FITTED_PATH = _os.path.join(_os.path.dirname(__file__), "state",
                             "fitted_baselines.json")
FITTED_CYCLES: Optional[dict[int, int]] = None
FITTED_ROOM_BAND_ADJ: Optional[dict[str, int]] = None
try:
    with open(_FITTED_PATH) as _fh:
        _payload = _json.load(_fh)
    FITTED_CYCLES = {int(k): int(v)
                     for k, v in _payload["cycle_baselines_fitted"].items()}
    FITTED_ROOM_BAND_ADJ = {str(k): int(v)
                            for k, v in _payload["room_comp_band_adjustments"].items()}
except (FileNotFoundError, KeyError, ValueError):
    pass


def _room_band(room_temp_f: float) -> str:
    if room_temp_f is None or math.isnan(room_temp_f):
        return "neutral"
    if room_temp_f < 67.0:
        return "cold"
    if room_temp_f < 69.0:
        return "cool"
    if room_temp_f < 72.0:
        return "neutral"
    if room_temp_f < 74.0:
        return "warm"
    return "heat_on"


# Slope above HEAT_ON_THRESHOLD: per-°F additional cooling adjustment.
# Derived from observed override data trend (74:-1, 75:-2, 77:-4 → ~-1.5/°F).
# Capped at HEAT_ON_MAX_ADJ to avoid wild extrapolation past observed range.
HEAT_ON_THRESHOLD_F = 74.0
HEAT_ON_BASE_ADJ = -1
HEAT_ON_SLOPE_PER_F = -1.5
HEAT_ON_MAX_ADJ = -6  # cap; at cycle 5 (-3) this saturates the safety floor


def _heat_on_adjustment(room_temp_f: float) -> int:
    """Continuous cooling slope above HEAT_ON_THRESHOLD_F (74°F).

    Reflects user-revealed intuition that hot rooms want progressively more
    cooling. At 76°F a late-cycle baseline of -3 plus this adjustment hits
    -8 (close to max cool); at 77°F+ it saturates at the floor.
    """
    if room_temp_f is None or math.isnan(room_temp_f):
        return 0
    if room_temp_f < HEAT_ON_THRESHOLD_F:
        return 0
    raw = HEAT_ON_BASE_ADJ + HEAT_ON_SLOPE_PER_F * (room_temp_f - HEAT_ON_THRESHOLD_F)
    return int(round(max(HEAT_ON_MAX_ADJ, raw)))


def _blower_to_l1(pct: float) -> int:
    pct = max(0, min(100, int(round(pct))))
    return min(L1_TO_BLOWER, key=lambda k: (abs(L1_TO_BLOWER[k] - pct), k))


def room_comp_blower(room_temp_f: float) -> int:
    """v5's room-comp formula in blower-% space (positive = cooler)."""
    if room_temp_f is None or math.isnan(room_temp_f):
        return 0
    if room_temp_f > ROOM_REF_F:
        return int(round((room_temp_f - ROOM_REF_F) * ROOM_HOT_PER_F))
    if room_temp_f < ROOM_REF_F:
        comp = (ROOM_REF_F - room_temp_f) * ROOM_COLD_PER_F
        if room_temp_f < ROOM_COLD_THRESH_F:
            comp += (ROOM_COLD_THRESH_F - room_temp_f) * ROOM_COLD_EXTRA_PER_F
        return -int(round(comp))
    return 0


def cycle_baseline(elapsed_min: float) -> int:
    cn = int(elapsed_min // CYCLE_DURATION_MIN) + 1
    cn = max(1, min(cn, max(CYCLE_BASELINES)))
    return CYCLE_BASELINES[cn]


def cycle_num_of(elapsed_min: float) -> int:
    return max(1, min(int(elapsed_min // CYCLE_DURATION_MIN) + 1,
                      max(CYCLE_BASELINES)))


def smart_baseline(elapsed_min: float, room_temp_f: float) -> int:
    """Cycle baseline + room compensation, mapped back to L1.

    If fitted constants are available (FITTED_CYCLES / FITTED_ROOM_BAND_ADJ
    populated from ml/state/fitted_baselines.json), uses those instead of
    v5's hand-picked constants. For room ≥ HEAT_ON_THRESHOLD_F a continuous
    cooling slope is layered on top of the band adjustment, reflecting
    user-revealed intuition that hot rooms want progressively more cooling.

    Returns an L1 setting in [-10, 0]. Deterministic, sensor-driven,
    no learning state.
    """
    if FITTED_CYCLES is not None and FITTED_ROOM_BAND_ADJ is not None:
        cn = cycle_num_of(elapsed_min)
        base = FITTED_CYCLES.get(cn, CYCLE_BASELINES[cn])
        band_adj = FITTED_ROOM_BAND_ADJ.get(_room_band(room_temp_f), 0)
        # heat_on band already includes a -1 from band_adj at the boundary;
        # the slope ADDS to that, so for the heat_on band we use the slope
        # function alone (which is calibrated to start at -1 at 74°F).
        if room_temp_f is not None and not math.isnan(room_temp_f) \
                and room_temp_f >= HEAT_ON_THRESHOLD_F:
            adj = _heat_on_adjustment(room_temp_f)
        else:
            adj = band_adj
        return max(-10, min(0, base + adj))
    base_l1 = cycle_baseline(elapsed_min)
    base_blower = L1_TO_BLOWER[base_l1]
    target = base_blower + room_comp_blower(room_temp_f)
    target = max(0, min(100, target))
    return _blower_to_l1(target)


# ── Sensor / context features ──────────────────────────────────────────

def _rolling_slope(series: pd.Series, ts: pd.Series, window_min: float) -> pd.Series:
    """Slope (units per minute) over the trailing `window_min` window.

    Implemented per-night via a small numpy loop. Series is aligned to ts.
    """
    out = np.full(len(series), np.nan, dtype=float)
    vals = series.to_numpy(dtype=float)
    times = ts.values.astype("datetime64[ns]").astype("int64") / 60_000_000_000  # ns -> minutes
    j = 0
    for i in range(len(series)):
        t_i = times[i]
        cutoff = t_i - window_min
        while j < i and times[j] < cutoff:
            j += 1
        win_t = times[j:i + 1]
        win_v = vals[j:i + 1]
        mask = ~np.isnan(win_v)
        if mask.sum() < 3:
            continue
        x = win_t[mask] - win_t[mask].mean()
        y = win_v[mask] - win_v[mask].mean()
        denom = (x * x).sum()
        if denom <= 1e-9:
            continue
        out[i] = (x * y).sum() / denom
    return pd.Series(out, index=series.index)


def _stage_at(stages: pd.DataFrame, ts) -> str:
    """Return Apple Watch sleep stage active at time ts. NA -> 'unknown'."""
    if stages.empty:
        return "unknown"
    mask = (stages["start_ts"] <= ts) & (stages["end_ts"] > ts)
    hits = stages.loc[mask, "stage"]
    if hits.empty:
        return "unknown"
    return str(hits.iloc[0])


def _join_stages(rd: pd.DataFrame, stages: pd.DataFrame) -> pd.Series:
    """Vectorised stage assignment using a sorted merge."""
    if stages.empty:
        return pd.Series(["unknown"] * len(rd), index=rd.index, dtype="object")
    s = stages.sort_values("start_ts").reset_index(drop=True)
    starts = s["start_ts"].to_numpy()
    ends = s["end_ts"].to_numpy()
    stage_arr = s["stage"].to_numpy()
    ts = rd["ts"].to_numpy()
    idx = np.searchsorted(starts, ts, side="right") - 1
    out = np.array(["unknown"] * len(rd), dtype=object)
    valid = (idx >= 0) & (idx < len(s))
    if valid.any():
        cand = idx[valid]
        in_seg = ts[valid] < ends[cand]
        out[np.where(valid)[0][in_seg]] = stage_arr[cand][in_seg]
    return pd.Series(out, index=rd.index, dtype="object")


def _stage_pct_so_far(rd: pd.DataFrame, stage_col: str) -> tuple[pd.Series, pd.Series]:
    """For each row, fraction of the night so far spent in deep / rem.

    Uses the per-row stage assignment (not segment durations) – good enough
    given the segments are coarse and the sensor cadence is regular (~30s).
    """
    out_deep = np.zeros(len(rd))
    out_rem = np.zeros(len(rd))
    for nid, g in rd.groupby("night_id"):
        stages = g[stage_col].to_numpy()
        idx = g.index.to_numpy()
        n = len(stages)
        d = (stages == "deep").astype(float)
        r = (stages == "rem").astype(float)
        cum_d = np.cumsum(d)
        cum_r = np.cumsum(r)
        denom = np.arange(1, n + 1, dtype=float)
        out_deep[idx - idx.min()] = cum_d / denom  # offset within group
        out_rem[idx - idx.min()] = cum_r / denom
    # The above uses positional indices because g.index is contiguous when
    # we feed in a freshly-reset-indexed dataframe. Caller MUST pass that.
    return pd.Series(out_deep, index=rd.index), pd.Series(out_rem, index=rd.index)


def build_features(readings: pd.DataFrame,
                   stages: pd.DataFrame,
                   health: Optional[pd.DataFrame] = None) -> pd.DataFrame:
    """Compute the full feature frame.

    Input `readings` must already have `night_id` (use data_io.assign_nights).
    Returns a new DataFrame with the original ts/night_id/setting/action plus
    every feature referenced in PRD §4.3 (excluding the policy-confounded
    ones the PRD explicitly bans).
    """
    rd = readings.sort_values(["night_id", "ts"]).reset_index(drop=True).copy()

    # Time features
    rd["cycle_num"] = rd["elapsed_min"].apply(cycle_num_of)
    rd["cycle_baseline"] = rd["elapsed_min"].apply(cycle_baseline)
    # smart baseline = v5 cycle + v5 room comp, mapped to L1
    rd["smart_baseline"] = [
        smart_baseline(em, rt) for em, rt in zip(rd["elapsed_min"], rd["room_temp_f"])
    ]
    rd["cycle_phase"] = (rd["elapsed_min"] % CYCLE_DURATION_MIN) / CYCLE_DURATION_MIN
    rd["sin_cycle"] = np.sin(2 * np.pi * rd["elapsed_min"] / CYCLE_DURATION_MIN)
    rd["cos_cycle"] = np.cos(2 * np.pi * rd["elapsed_min"] / CYCLE_DURATION_MIN)
    rd["night_progress"] = (rd["elapsed_min"] / TYPICAL_NIGHT_MIN).clip(0, 1.5)

    # Body composite
    rd["body_avg"] = rd[["body_left_f", "body_center_f", "body_right_f"]].mean(axis=1)
    rd["body_center"] = rd["body_center_f"]

    feats_per_night = []
    for nid, g in rd.groupby("night_id", sort=True):
        gi = g.copy()
        gi["body_trend_5m"] = _rolling_slope(gi["body_avg"], gi["ts"], 5)
        gi["body_trend_15m"] = _rolling_slope(gi["body_avg"], gi["ts"], 15)
        first_body = gi["body_avg"].dropna()
        body_entry = float(first_body.iloc[0]) if len(first_body) else np.nan
        gi["body_delta_from_entry"] = gi["body_avg"] - body_entry

        # 30 min rolling max body (use time-weighted: just 30 prior rows ~15 min,
        # so use a broader window via timedelta)
        gi = gi.set_index("ts")
        gi["body_max_last_30m"] = gi["body_avg"].rolling("30min").max()
        gi["recent_blower_avg"] = gi["setpoint_f"].rolling("15min").mean()
        gi["mean_setting_30m"] = gi["setting"].astype("float").rolling("30min").mean()
        gi["pressure_variability_5m"] = (
            gi["bed_left_calibrated_pressure_pct"].rolling("5min").std()
        )
        # Room features
        gi["room_trend_30m"] = _rolling_slope(
            gi["room_temp_f"].reset_index(drop=True),
            pd.Series(gi.index, index=gi.reset_index().index), 30,
        ).values  # this returns positional; assign back as np
        first_room = gi["room_temp_f"].dropna()
        room_entry = float(first_room.iloc[0]) if len(first_room) else np.nan
        gi["room_delta_from_start"] = gi["room_temp_f"] - room_entry

        # Setting duration: minutes since last setting change
        sett = gi["setting"].astype("float").to_numpy()
        ts_min = gi.index.values.astype("datetime64[ns]").astype("int64") / 60_000_000_000
        last_change = ts_min[0]
        last_val = sett[0]
        dur = np.zeros(len(sett))
        for k in range(len(sett)):
            if not np.isnan(sett[k]) and sett[k] != last_val:
                last_change = ts_min[k]
                last_val = sett[k]
            dur[k] = min(60.0, ts_min[k] - last_change)
        gi["setting_duration_min"] = dur

        gi = gi.reset_index()
        feats_per_night.append(gi)

    rd = pd.concat(feats_per_night, ignore_index=True)

    # Sleep stage (Apple Watch) — vectorised join
    rd["sleep_stage"] = _join_stages(rd, stages)

    # stage_duration_min: time in current stage (per-night)
    rd["stage_duration_min"] = 0.0
    for nid, g in rd.groupby("night_id", sort=True):
        stages_arr = g["sleep_stage"].to_numpy()
        ts_min = g["ts"].values.astype("datetime64[ns]").astype("int64") / 60_000_000_000
        cur_start = ts_min[0]
        cur_stage = stages_arr[0]
        out = np.zeros(len(g))
        for k in range(len(g)):
            if stages_arr[k] != cur_stage:
                cur_start = ts_min[k]
                cur_stage = stages_arr[k]
            out[k] = min(120.0, ts_min[k] - cur_start)
        rd.loc[g.index, "stage_duration_min"] = out

    # Cumulative deep / REM percentage (per-night, by row count)
    rd["deep_pct_so_far"] = 0.0
    rd["rem_pct_so_far"] = 0.0
    for nid, g in rd.groupby("night_id", sort=True):
        n = len(g)
        d = (g["sleep_stage"].to_numpy() == "deep").astype(float)
        r = (g["sleep_stage"].to_numpy() == "rem").astype(float)
        denom = np.arange(1, n + 1, dtype=float)
        rd.loc[g.index, "deep_pct_so_far"] = np.cumsum(d) / denom
        rd.loc[g.index, "rem_pct_so_far"] = np.cumsum(r) / denom

    # Encode sleep_stage as ordered category (LightGBM accepts categorical)
    rd["sleep_stage"] = rd["sleep_stage"].astype("category")

    # Pressure / occupancy
    rd["pressure_left"] = rd["bed_left_calibrated_pressure_pct"]
    rd["occupied_both"] = rd["bed_occupied_both"].fillna(False).astype(int)

    return rd


# ── Labels ─────────────────────────────────────────────────────────────

# Weights from PRD §4.2.
# Empirically tested cutting no-override weights 5x (0.10/0.05) to address
# the 8:1 noise:signal mass; LONO comfort barely moved (73.5% → 73.0%),
# so the bottleneck is sample size, not weight balance. Restored to PRD.
W_OVERRIDE = 3.0
W_POST_OVERRIDE = 2.0
W_PRE_OVERRIDE = 1.0
W_NO_OVERRIDE = 0.5
W_FAR_NO_OVERRIDE = 0.25

POST_WINDOW_MIN = (5, 15)   # 5..15 min AFTER override
PRE_WINDOW_MIN = (5, 10)    # 5..10 min BEFORE override
FAR_THRESHOLD_MIN = 30      # no-override beyond 30 min from any override


def build_labels(rd: pd.DataFrame) -> pd.DataFrame:
    """Generate (label, weight) per row, where label = setting_target - cycle_baseline.

    Returns the input DataFrame with three new columns:
      - target_setting:  the L1 setting we want the model to ultimately produce
      - label_offset:    target_setting - cycle_baseline (clamped to OFFSET_CLAMP)
      - sample_weight:   per PRD §4.2

    Rows with no usable label are dropped (e.g. before a night has any
    valid setting data).
    """
    out = rd.copy()
    out["target_setting"] = np.nan
    out["sample_weight"] = 0.0

    for nid, g in out.groupby("night_id", sort=True):
        idx = g.index
        ts_min = g["ts"].values.astype("datetime64[ns]").astype("int64") / 60_000_000_000
        actions = g["action"].to_numpy()
        settings = g["setting"].astype("float").to_numpy()

        ovr_mask = actions == "override"
        ovr_positions = np.where(ovr_mask)[0]
        ovr_times = ts_min[ovr_positions]
        ovr_settings = settings[ovr_positions]

        target = np.full(len(g), np.nan, dtype=float)
        weight = np.zeros(len(g), dtype=float)

        # 1) Overrides themselves (3x)
        for k in ovr_positions:
            if not np.isnan(settings[k]):
                target[k] = settings[k]
                weight[k] = W_OVERRIDE

        # 2) Post-override stabilisation window (2x): override value persists
        #    for 5..15 min after each override.
        for op, otime, oval in zip(ovr_positions, ovr_times, ovr_settings):
            if np.isnan(oval):
                continue
            for k in range(op + 1, len(g)):
                dt = ts_min[k] - otime
                if dt > POST_WINDOW_MIN[1]:
                    break
                if dt < POST_WINDOW_MIN[0]:
                    continue
                # Don't overwrite a stronger label
                if weight[k] >= W_POST_OVERRIDE:
                    continue
                target[k] = oval
                weight[k] = W_POST_OVERRIDE

        # 3) Pre-override discomfort window (1x): the override value is the
        #    *correct* label (what the user wanted) – this teaches the model
        #    to predict that target slightly before the override fires.
        for op, otime, oval in zip(ovr_positions, ovr_times, ovr_settings):
            if np.isnan(oval):
                continue
            for k in range(op - 1, -1, -1):
                dt = otime - ts_min[k]
                if dt > PRE_WINDOW_MIN[1]:
                    break
                if dt < PRE_WINDOW_MIN[0]:
                    continue
                if weight[k] >= W_PRE_OVERRIDE:
                    continue
                target[k] = oval
                weight[k] = W_PRE_OVERRIDE

        # 4) No-override readings (0.5x); 0.25x if >30min from nearest override
        if len(ovr_times):
            dist = np.abs(ts_min[:, None] - ovr_times[None, :]).min(axis=1)
        else:
            dist = np.full(len(g), np.inf)
        for k in range(len(g)):
            if weight[k] > 0:
                continue
            if np.isnan(settings[k]):
                continue
            target[k] = settings[k]
            weight[k] = W_NO_OVERRIDE if dist[k] <= FAR_THRESHOLD_MIN else W_FAR_NO_OVERRIDE

        out.loc[idx, "target_setting"] = target
        out.loc[idx, "sample_weight"] = weight

    out = out.dropna(subset=["target_setting"]).reset_index(drop=True)
    out["label_offset"] = (out["target_setting"] - out["smart_baseline"]).clip(*OFFSET_CLAMP)
    return out


# ── Feature column manifest ────────────────────────────────────────────

# Excludes policy-confounded fields (current_setting, override_count_tonight,
# last_override_elapsed) per PRD §4.3. The recent-output features
# (`mean_setting_30m`, `setting_duration_min`, `recent_blower_avg`) were
# kept after a controlled experiment: dropping them reduced ML LONO-CV
# comfort from 73.5% to 65.4%, so they encode genuine thermal-inertia
# information rather than just policy mimicry.
FEATURE_COLUMNS = [
    "elapsed_min", "cycle_num", "cycle_phase", "sin_cycle", "cos_cycle",
    "night_progress",
    "body_avg", "body_center",
    "body_trend_5m", "body_trend_15m",
    "body_delta_from_entry", "body_max_last_30m",
    "room_temp_f", "room_trend_30m", "room_delta_from_start", "ambient_f",
    "setting_duration_min", "mean_setting_30m", "recent_blower_avg",
    "sleep_stage", "stage_duration_min",
    "deep_pct_so_far", "rem_pct_so_far",
    "pressure_left", "pressure_variability_5m", "occupied_both",
]
CATEGORICAL = ["sleep_stage"]
