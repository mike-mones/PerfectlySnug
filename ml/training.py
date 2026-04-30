"""
Training + LONO-CV evaluation for the ML controller (Phase 1).

Produces three things:
  1. Leave-One-Night-Out cross-validated metrics for the LightGBM model
  2. Comparison metrics for the v5 baseline (what actually happened) and
     a "dumb baseline" (cycle baseline only, no ML)
  3. A final model trained on ALL nights, saved for Phase 2 deployment
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd
import lightgbm as lgb

from .features import (
    FEATURE_COLUMNS, CATEGORICAL, OFFSET_CLAMP, SETTING_CLAMP,
    cycle_baseline,
)


LGB_PARAMS = dict(
    objective="regression",
    metric="rmse",
    num_leaves=15,            # small leaves -> robust on ~2k rows
    learning_rate=0.05,
    min_data_in_leaf=10,
    feature_fraction=0.9,
    bagging_fraction=0.9,
    bagging_freq=5,
    lambda_l2=1.0,
    verbose=-1,
)
NUM_BOOST_ROUND = 200
EARLY_STOP = 25


# ── User preference (ground truth) timeline ─────────────────────────────

def user_pref_timeline(night: pd.DataFrame) -> np.ndarray:
    """Return the user's preferred L1 setting at each row of `night`.

    Mirrors `tools/backtest_v5.py::get_user_preference_timeline`.
    Before any override: assume the active setting was acceptable.
    From an override onward: the override value is the preference until
    the next override.
    """
    n = len(night)
    pref = np.full(n, np.nan, dtype=float)
    actions = night["action"].to_numpy()
    settings = night["setting"].astype("float").to_numpy()
    ovr_pos = np.where(actions == "override")[0]

    if len(ovr_pos) == 0:
        return settings.copy()

    first = ovr_pos[0]
    pref[:first] = settings[:first]
    for j, op in enumerate(ovr_pos):
        end = ovr_pos[j + 1] if j + 1 < len(ovr_pos) else n
        pref[op:end] = settings[op]
    return pref


# ── Metrics ─────────────────────────────────────────────────────────────

@dataclass
class NightMetrics:
    night_id: int
    date: str
    rows: int
    overrides: int
    comfort_rate_v5: float          # actual v5 (what controller wrote = `effective`)
    comfort_rate_dumb: float        # cycle-baseline only
    comfort_rate_smart: float       # cycle + room-comp baseline (no ML)
    comfort_rate_ml: float          # ML model on this held-out night
    # Restricted to readings within 60 min of an override (strong preference)
    comfort_strong_v5: float
    comfort_strong_smart: float
    comfort_strong_ml: float
    rmse_setting_ml: float          # vs user preference
    bias_ml: float                  # mean(prediction - preference)
    pred_overrides_v5: int
    pred_overrides_dumb: int
    pred_overrides_ml: int


def _comfort(predicted: np.ndarray, pref: np.ndarray) -> tuple[float, int]:
    """Return (comfort_rate, predicted_override_count).

    Comfort: |predicted - pref| <= 1 (matches v5 backtester)
    Predicted overrides: |predicted - pref| > 1
    """
    valid = ~np.isnan(pref) & ~np.isnan(predicted)
    if not valid.any():
        return 0.0, 0
    dev = predicted[valid] - pref[valid]
    comfort = float((np.abs(dev) <= 1).mean())
    pred_ovr = int((np.abs(dev) > 1).sum())
    return comfort, pred_ovr


def _strong_pref_mask(night: pd.DataFrame, window_min: float = 60.0) -> np.ndarray:
    """Mask of readings within `window_min` of an override event.

    Pre-override readings have a preference == the v5-active setting, which
    is biased: the user *might* have been comfortable, or might just not have
    intervened yet. Restricting to readings near an override gives a more
    honest comfort comparison because the preference label is grounded in
    explicit user feedback.
    """
    actions = night["action"].to_numpy()
    ts_min = night["ts"].values.astype("datetime64[ns]").astype("int64") / 60_000_000_000
    ovr_t = ts_min[actions == "override"]
    if len(ovr_t) == 0:
        return np.zeros(len(night), dtype=bool)
    return np.abs(ts_min[:, None] - ovr_t[None, :]).min(axis=1) <= window_min


def _clip_setting(arr: np.ndarray) -> np.ndarray:
    return np.clip(np.round(arr), SETTING_CLAMP[0], SETTING_CLAMP[1])


def predict_settings(model: lgb.Booster, X: pd.DataFrame,
                     baselines: np.ndarray) -> np.ndarray:
    raw_offset = model.predict(X)
    offset = np.clip(raw_offset, OFFSET_CLAMP[0], OFFSET_CLAMP[1])
    return _clip_setting(baselines + offset)


# ── Training ────────────────────────────────────────────────────────────

def train_one(train_df: pd.DataFrame,
              valid_df: Optional[pd.DataFrame] = None) -> lgb.Booster:
    Xtr = train_df[FEATURE_COLUMNS]
    ytr = train_df["label_offset"].to_numpy(dtype=float)
    wtr = train_df["sample_weight"].to_numpy(dtype=float)
    train_set = lgb.Dataset(Xtr, label=ytr, weight=wtr,
                            categorical_feature=CATEGORICAL,
                            free_raw_data=False)
    valid_sets = [train_set]
    valid_names = ["train"]
    callbacks = [lgb.log_evaluation(0)]
    if valid_df is not None and len(valid_df) > 0:
        Xv = valid_df[FEATURE_COLUMNS]
        yv = valid_df["label_offset"].to_numpy(dtype=float)
        wv = valid_df["sample_weight"].to_numpy(dtype=float)
        valid_set = lgb.Dataset(Xv, label=yv, weight=wv,
                                categorical_feature=CATEGORICAL,
                                reference=train_set, free_raw_data=False)
        valid_sets.append(valid_set)
        valid_names.append("valid")
        callbacks.append(lgb.early_stopping(EARLY_STOP, verbose=False))
    booster = lgb.train(
        LGB_PARAMS, train_set,
        num_boost_round=NUM_BOOST_ROUND,
        valid_sets=valid_sets, valid_names=valid_names,
        callbacks=callbacks,
    )
    return booster


# ── LONO-CV ─────────────────────────────────────────────────────────────

def leave_one_night_out(features: pd.DataFrame, labels: pd.DataFrame
                        ) -> tuple[pd.DataFrame, dict]:
    """
    Run LONO-CV.

    `features` is the full feature frame (unfiltered – may contain rows
    that didn't make it into the labelled set, which is fine for
    evaluating the model's predictions on every reading).

    `labels` is the labelled subset (one row per usable example).

    Returns:
      per_night_df : DataFrame of NightMetrics
      summary      : dict of aggregated metrics
    """
    night_ids = sorted(features["night_id"].unique())
    rows = []

    for held in night_ids:
        train_lbl = labels[labels["night_id"] != held]
        if train_lbl.empty:
            continue
        booster = train_one(train_lbl)

        held_feats = features[features["night_id"] == held].reset_index(drop=True)
        if held_feats.empty:
            continue

        pref = user_pref_timeline(held_feats)
        cb = held_feats["cycle_baseline"].to_numpy(dtype=float)
        sb = held_feats["smart_baseline"].to_numpy(dtype=float)

        # ML predictions -> final settings (anchored to smart baseline)
        Xh = held_feats[FEATURE_COLUMNS]
        ml_settings = predict_settings(booster, Xh, sb)

        # Dumb baseline = cycle baseline only (no room comp, no learning)
        dumb_settings = _clip_setting(cb)

        # Smart baseline alone (cycle + room comp, no ML) — the new gate
        smart_settings = _clip_setting(sb)

        # v5 actual = what the controller wrote (`effective`, fallback `setting`)
        v5_actual = held_feats["effective"].astype("float").fillna(
            held_feats["setting"].astype("float")
        ).to_numpy()

        c_v5, o_v5 = _comfort(v5_actual, pref)
        c_dumb, o_dumb = _comfort(dumb_settings, pref)
        c_smart, o_smart = _comfort(smart_settings, pref)
        c_ml, o_ml = _comfort(ml_settings, pref)

        strong = _strong_pref_mask(held_feats)
        if strong.any():
            cs_v5, _ = _comfort(v5_actual[strong], pref[strong])
            cs_smart, _ = _comfort(smart_settings[strong], pref[strong])
            cs_ml, _ = _comfort(ml_settings[strong], pref[strong])
        else:
            cs_v5 = cs_smart = cs_ml = float("nan")

        valid = ~np.isnan(pref)
        rmse = float(np.sqrt(np.mean((ml_settings[valid] - pref[valid]) ** 2))) if valid.any() else float("nan")
        bias = float(np.mean(ml_settings[valid] - pref[valid])) if valid.any() else float("nan")

        rows.append(NightMetrics(
            night_id=int(held),
            date=str(held_feats["ts"].iloc[0].date()),
            rows=int(len(held_feats)),
            overrides=int((held_feats["action"] == "override").sum()),
            comfort_rate_v5=c_v5,
            comfort_rate_dumb=c_dumb,
            comfort_rate_smart=c_smart,
            comfort_rate_ml=c_ml,
            comfort_strong_v5=cs_v5,
            comfort_strong_smart=cs_smart,
            comfort_strong_ml=cs_ml,
            rmse_setting_ml=rmse,
            bias_ml=bias,
            pred_overrides_v5=o_v5,
            pred_overrides_dumb=o_dumb,
            pred_overrides_ml=o_ml,
        ))

    per_night = pd.DataFrame([asdict(r) for r in rows])

    summary = {
        "n_nights": len(per_night),
        "comfort_v5_mean": float(per_night["comfort_rate_v5"].mean()),
        "comfort_dumb_mean": float(per_night["comfort_rate_dumb"].mean()),
        "comfort_smart_mean": float(per_night["comfort_rate_smart"].mean()),
        "comfort_ml_mean": float(per_night["comfort_rate_ml"].mean()),
        "comfort_v5_std": float(per_night["comfort_rate_v5"].std()),
        "comfort_smart_std": float(per_night["comfort_rate_smart"].std()),
        "comfort_ml_std": float(per_night["comfort_rate_ml"].std()),
        "rmse_ml_mean": float(per_night["rmse_setting_ml"].mean()),
        "bias_ml_mean": float(per_night["bias_ml"].mean()),
        "ml_beats_v5_nights": int((per_night["comfort_rate_ml"] > per_night["comfort_rate_v5"]).sum()),
        "ml_beats_smart_nights": int((per_night["comfort_rate_ml"] > per_night["comfort_rate_smart"]).sum()),
        "smart_beats_v5_nights": int((per_night["comfort_rate_smart"] > per_night["comfort_rate_v5"]).sum()),
        "ml_pred_overrides_total": int(per_night["pred_overrides_ml"].sum()),
        "v5_pred_overrides_total": int(per_night["pred_overrides_v5"].sum()),
        "dumb_pred_overrides_total": int(per_night["pred_overrides_dumb"].sum()),
        "comfort_strong_v5_mean": float(per_night["comfort_strong_v5"].mean()),
        "comfort_strong_smart_mean": float(per_night["comfort_strong_smart"].mean()),
        "comfort_strong_ml_mean": float(per_night["comfort_strong_ml"].mean()),
        "ml_beats_v5_strong_nights": int(
            (per_night["comfort_strong_ml"] > per_night["comfort_strong_v5"]).sum()),
        "smart_beats_v5_strong_nights": int(
            (per_night["comfort_strong_smart"] > per_night["comfort_strong_v5"]).sum()),
    }
    return per_night, summary


def train_final_model(labels: pd.DataFrame) -> lgb.Booster:
    return train_one(labels)


def feature_importance(booster: lgb.Booster, top: int = 15) -> pd.DataFrame:
    fi = pd.DataFrame({
        "feature": booster.feature_name(),
        "gain": booster.feature_importance(importance_type="gain"),
        "split": booster.feature_importance(importance_type="split"),
    })
    fi["gain_pct"] = 100 * fi["gain"] / max(fi["gain"].sum(), 1)
    return fi.sort_values("gain", ascending=False).head(top).reset_index(drop=True)
