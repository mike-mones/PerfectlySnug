#!/usr/bin/env python3
"""Honest offline evaluator for v6 PerfectlySnug controller proposals.

Every candidate policy must implement Policy.decide(state, history) -> int, where
state contains only fields observable at the current 5-minute controller cycle and
history contains only earlier states/decisions in the same zone-night replay.

CLI examples:
    .venv/bin/python tools/v6_eval.py --policy baseline --json
    .venv/bin/python tools/v6_eval.py --policy baseline --out ml/state/v6_eval_baseline.json
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Protocol

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]

DB_DEFAULTS = {
    "host": "192.168.0.3",
    "port": 5432,
    "dbname": "sleepdata",
    "user": "sleepsync",
    "password": "sleepsync_local",
}

L1_TO_BLOWER_PCT = {
    -10: 100, -9: 87, -8: 75, -7: 65, -6: 50, -5: 41,
    -4: 33, -3: 26, -2: 20, -1: 10, 0: 0,
}

# Counterfactual body-temperature dynamics: deliberately simple and labelled as
# an assumption.  A 1 blower-point reduction warms the surface-sensor steady
# state by 0.03°F; the bed approaches that steady state with a 45-min time
# constant.  This is used only for replay/case-test directionality, not as a
# training label.
CF_SURFACE_F_PER_BLOWER_POINT = -0.03
CF_TAU_MIN = 45.0

FORBIDDEN_STATE_FIELDS = {
    "override_delta", "action", "notes", "future_override", "user_pref",
    "revealed_pref", "is_final_test", "night_outcome", "next_setting",
}

CASE_DEFS = {
    "A": {
        "description": "2026-05-01 LEFT 01:37-02:05 too-cold override cluster (-10 -> -3)",
        "zone": "left",
        "start": "2026-05-01 01:20:00-04:00",
        "end": "2026-05-01 02:15:00-04:00",
        "eval_start": "2026-05-01 01:37:00-04:00",
        "eval_end": "2026-05-01 02:05:59-04:00",
        "pass": "median_setting >= -5, >=20 minutes at setting >= -6, and median counterfactual surface >= +1.0°F vs observed",
    },
    "B": {
        "description": "2026-05-01 RIGHT 03:25 under-cooled override (-4 -> -5)",
        "zone": "right",
        "start": "2026-05-01 03:00:00-04:00",
        "end": "2026-05-01 03:50:00-04:00",
        "eval_start": "2026-05-01 03:10:00-04:00",
        "eval_end": "2026-05-01 03:40:00-04:00",
        "pass": "setting <= -5 for >=15 minutes and median setting <= -5 around 03:25",
    },
    "C": {
        "description": "2026-04-30 morning: cold mid-night, slightly warm in morning",
        "zone": "left",
        "start": "2026-04-30 04:00:00-04:00",
        "end": "2026-04-30 07:20:00-04:00",
        "warm_start": "2026-04-30 04:15:00-04:00",
        "warm_end": "2026-04-30 04:40:00-04:00",
        "cool_start": "2026-04-30 06:40:00-04:00",
        "cool_end": "2026-04-30 07:10:00-04:00",
        "pass": "mid-night median setting >= -3 and morning median setting <= -4",
    },
}


class Policy(Protocol):
    """Candidate policy interface required by v6 proposals."""

    name: str

    def decide(self, state: dict[str, Any], history: list[dict[str, Any]]) -> int:
        """Return active L setting in [-10, 0] using no future data."""


@dataclass
class EvalSplits:
    cv_folds: list[dict[str, Any]]
    final_test_nights: dict[str, list[str]]
    holdout_n_recent: int = 3


@dataclass
class CaseResult:
    case_id: str
    passed: bool
    details: dict[str, Any]


@dataclass
class EvalResult:
    policy_name: str
    generated_at: str
    primary: dict[str, Any]
    secondary: dict[str, Any]
    right_comfort_proxy: dict[str, Any]
    splits: dict[str, Any]
    power: dict[str, Any]
    cases: dict[str, Any]
    rows: dict[str, int]

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2, default=str, allow_nan=False)


class V52BaselinePolicy:
    """Observed deployed-v5.2 wrapper baseline.

    For baseline scoring we use the setting that the deployed controller or
    firmware actually had active immediately before the candidate decision
    point.  This is the apples-to-apples comparator all proposals must beat.
    It intentionally does not read override labels, future rows, or notes.

    The formula mirror remains below for documentation/reuse, but decide()
    returns current_setting so --policy baseline reproduces observed v5.2.
    """

    name = "v5.2_reference"
    left_cycles = {1: -10, 2: -10, 3: -7, 4: -5, 5: -5, 6: -6}
    right_cycles = {1: -8, 2: -7, 3: -6, 4: -5, 5: -5, 6: -5}

    def decide(self, state: dict[str, Any], history: list[dict[str, Any]]) -> int:
        if state.get("current_setting") is not None:
            return clamp_setting(state["current_setting"])
        zone = state.get("zone", "left")
        elapsed = fnum(state.get("elapsed_min"), 0.0)
        cycle = cycle_of(elapsed)
        stage = str(state.get("sleep_stage") or "").lower().strip()
        mins_since_start = minutes_since_zone_start(state, history)
        if stage in {"inbed", "awake"} or 0 <= mins_since_start <= 30:
            return -10
        if zone == "right":
            return self._right(state, cycle)
        return self._left(state, cycle, stage)

    def _left(self, state: dict[str, Any], cycle: int, stage: str) -> int:
        base = self.left_cycles.get(cycle, self.left_cycles[max(self.left_cycles)])
        # Only map Apple stages that v5 maps explicitly; 'asleep' and cycle_N do
        # not override the cycle baseline.
        stage_map = {"deep": -10, "core": -8, "rem": -6, "awake": -5, "inbed": -9}
        if stage in stage_map:
            base = stage_map[stage]
        body_left = fnum(state.get("body_left_f"), None)
        if body_left is not None:
            delta = body_left - 80.0
            if delta < 0:
                base = clamp_setting(base + int(round(min(-1.25 * delta, 5))))
        target_blower = blower_for(base) + left_room_comp(fnum(state.get("room_temp_f"), None))
        body_avg = fnum(state.get("body_avg_f"), None)
        if body_avg is not None and body_avg > 85.0:
            current = inum(state.get("current_setting"), base)
            target_blower = max(target_blower, blower_for(max(-10, current - 1)))
        return setting_for_blower(target_blower)

    def _right(self, state: dict[str, Any], cycle: int) -> int:
        base = self.right_cycles.get(cycle, self.right_cycles[max(self.right_cycles)])
        body = fnum(state.get("body_left_f"), None)
        correction = 0
        if body is not None:
            delta = body - 80.0
            if delta > 0:
                correction = -int(round(min(0.5 * delta, 4)))
            elif delta < 0:
                correction = int(round(min(-0.3 * delta, 4)))
        proposed = clamp_setting(base + correction)
        target_blower = blower_for(proposed) + right_room_comp(fnum(state.get("room_temp_f"), None))
        return setting_for_blower(target_blower)


def fnum(value: Any, default: float | None = np.nan) -> float | None:
    try:
        if value is None or pd.isna(value):
            return default
        return float(value)
    except Exception:
        return default


def inum(value: Any, default: int = 0) -> int:
    try:
        if value is None or pd.isna(value):
            return default
        return int(round(float(value)))
    except Exception:
        return default


def clamp_setting(v: float | int) -> int:
    return max(-10, min(0, int(round(float(v)))))


def blower_for(setting: int | float) -> int:
    return L1_TO_BLOWER_PCT[clamp_setting(setting)]


def setting_for_blower(blower_pct: float | int) -> int:
    b = max(0, min(100, int(round(float(blower_pct)))))
    return min(L1_TO_BLOWER_PCT, key=lambda s: (abs(L1_TO_BLOWER_PCT[s] - b), s))


def cycle_of(elapsed_min: float) -> int:
    return max(1, min(6, int(float(elapsed_min or 0) // 90) + 1))


def left_room_comp(room_temp: float | None) -> int:
    if room_temp is None or pd.isna(room_temp):
        return 0
    if room_temp > 72.0:
        return round((room_temp - 72.0) * 4.0)
    comp = (72.0 - room_temp) * 4.0
    if room_temp < 63.0:
        comp += (63.0 - room_temp) * 3.0
    return -round(comp)


def right_room_comp(room_temp: float | None) -> int:
    if room_temp is None or pd.isna(room_temp):
        return 0
    if room_temp > 72.0:
        return round((room_temp - 72.0) * 4.0)
    return 0


def minutes_since_zone_start(state: dict[str, Any], history: list[dict[str, Any]]) -> float:
    if not history:
        return 0.0
    return max(0.0, fnum(state.get("elapsed_min"), 0.0) - fnum(history[0].get("elapsed_min"), 0.0))


def connect_db():
    import psycopg2

    cfg = DB_DEFAULTS.copy()
    for key in list(cfg):
        env = os.environ.get(f"SLEEPDATA_{key.upper()}")
        if env:
            cfg[key] = env
    return psycopg2.connect(**cfg)


def load_data(db_conn=None) -> pd.DataFrame:
    close = False
    if db_conn is None:
        db_conn = connect_db()
        close = True
    sql = """
        SELECT ts, zone, phase, elapsed_min,
               body_right_f, body_center_f, body_left_f, body_avg_f,
               ambient_f, room_temp_f, setpoint_f,
               setting, effective, baseline, learned_adj,
               action, override_delta, controller_version, notes,
               bed_left_calibrated_pressure_pct, bed_right_calibrated_pressure_pct,
               bed_occupied_left, bed_occupied_right, bed_occupied_either, bed_occupied_both
        FROM controller_readings
        WHERE zone IN ('left','right')
          AND (action IS NULL OR action <> 'empty_bed')
        ORDER BY ts, zone
    """
    try:
        df = pd.read_sql_query(sql, db_conn)
    finally:
        if close:
            db_conn.close()
    if df.empty:
        return df
    df["ts"] = pd.to_datetime(df["ts"], utc=True).dt.tz_convert("America/New_York")
    numeric = [c for c in df.columns if c.endswith("_f") or c.endswith("_pct")]
    numeric += ["elapsed_min", "setting", "effective", "baseline", "learned_adj", "override_delta"]
    for c in numeric:
        if c in df:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    # Match sleep_controller_v5._night_date_for(): timestamps before 18:00
    # belong to the previous sleep night.
    df["night"] = (df["ts"] - pd.Timedelta(hours=18)).dt.date.astype(str)
    df["sleep_stage"] = df["phase"].where(~df["phase"].astype(str).str.match(r"cycle_\d+", na=False), "unknown")
    return df.reset_index(drop=True)


def build_splits(df: pd.DataFrame, final_nights: int = 3, min_train_nights: int = 5) -> EvalSplits:
    folds: list[dict[str, Any]] = []
    final: dict[str, list[str]] = {}
    for zone, zdf in df.groupby("zone"):
        nights = sorted(zdf["night"].dropna().unique().tolist())
        final[zone] = nights[-final_nights:] if len(nights) > final_nights else nights[-1:]
        cv_nights = [n for n in nights if n not in set(final[zone])]
        for i in range(min_train_nights, len(cv_nights)):
            folds.append({
                "zone": zone,
                "train_nights": cv_nights[:i],
                "test_nights": [cv_nights[i]],
                "kind": "walk_forward",
            })
    return EvalSplits(cv_folds=folds, final_test_nights=final, holdout_n_recent=final_nights)


def state_from_row(row: pd.Series) -> dict[str, Any]:
    # Explicit anti-leakage whitelist: no action, override_delta, notes, or future labels.
    s = {
        "ts": row["ts"].isoformat(),
        "zone": row["zone"],
        "night": row["night"],
        "elapsed_min": fnum(row.get("elapsed_min"), 0.0),
        "cycle": cycle_of(fnum(row.get("elapsed_min"), 0.0)),
        "sleep_stage": row.get("sleep_stage") or "unknown",
        "body_left_f": fnum(row.get("body_left_f"), None),
        "body_center_f": fnum(row.get("body_center_f"), None),
        "body_right_f": fnum(row.get("body_right_f"), None),
        "body_avg_f": fnum(row.get("body_avg_f"), None),
        "room_temp_f": fnum(row.get("room_temp_f"), None),
        "ambient_f": fnum(row.get("ambient_f"), None),
        "setpoint_f": fnum(row.get("setpoint_f"), None),
        "current_setting": inum(row.get("effective"), inum(row.get("setting"), 0)),
        "bed_occupied_left": bool(row.get("bed_occupied_left")) if pd.notna(row.get("bed_occupied_left")) else None,
        "bed_occupied_right": bool(row.get("bed_occupied_right")) if pd.notna(row.get("bed_occupied_right")) else None,
        "bed_pressure_left_pct": fnum(row.get("bed_left_calibrated_pressure_pct"), None),
        "bed_pressure_right_pct": fnum(row.get("bed_right_calibrated_pressure_pct"), None),
    }
    assert not (set(s) & FORBIDDEN_STATE_FIELDS)
    return s


def replay(policy: Policy, df: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for (zone, night), g in df.sort_values("ts").groupby(["zone", "night"], sort=True):
        history: list[dict[str, Any]] = []
        cf_delta = 0.0
        prev_ts = None
        for _, row in g.iterrows():
            state = state_from_row(row)
            pred = clamp_setting(policy.decide(state, history.copy()))
            actual = inum(row.get("effective"), inum(row.get("setting"), pred))
            dt_min = 5.0 if prev_ts is None else max(0.5, min(30.0, (row["ts"] - prev_ts).total_seconds() / 60.0))
            ss_delta = CF_SURFACE_F_PER_BLOWER_POINT * (blower_for(pred) - blower_for(actual))
            alpha = 1.0 - math.exp(-dt_min / CF_TAU_MIN)
            cf_delta = cf_delta + alpha * (ss_delta - cf_delta)
            prev_ts = row["ts"]
            rec = row.to_dict()
            rec.update({
                "pred_setting": pred,
                "observed_effective": actual,
                "pred_blower_pct": blower_for(pred),
                "observed_blower_pct": blower_for(actual),
                "cf_surface_delta_f": cf_delta,
            })
            rows.append(rec)
            hist_entry = state.copy()
            hist_entry["decision"] = pred
            history.append(hist_entry)
    return pd.DataFrame(rows)


def override_frame(rp: pd.DataFrame) -> pd.DataFrame:
    ov = rp[(rp["action"] == "override") & rp["override_delta"].notna() & rp["setting"].notna()].copy()
    if ov.empty:
        ov["human_setting"] = []
        return ov
    # In current v5 logs, setting is the new manual value; effective is the pre-override controller value.
    ov["human_setting"] = ov["setting"].clip(-10, 0)
    ov["controller_before"] = ov["effective"].where(ov["effective"].notna(), ov["setting"] - ov["override_delta"])
    ov["pred_abs_err"] = (ov["pred_setting"] - ov["human_setting"]).abs()
    ov["baseline_abs_err"] = (ov["controller_before"] - ov["human_setting"]).abs()
    ov["pred_hit_1"] = ov["pred_abs_err"] <= 1
    ov["would_preclude"] = ov["pred_abs_err"] <= np.maximum(0, ov["baseline_abs_err"] - 2)
    return ov


def add_comfort_proxies(rp: pd.DataFrame) -> pd.DataFrame:
    out = rp.sort_values("ts").copy()
    out["dt_min"] = out.groupby(["zone", "night"])["ts"].diff().dt.total_seconds().div(60).clip(0.5, 30).fillna(5.0)
    out["body_30m_sd"] = out.groupby(["zone", "night"])["body_left_f"].transform(lambda s: s.rolling(6, min_periods=3).std())
    pressure_col = np.where(out["zone"].eq("right"), out["bed_right_calibrated_pressure_pct"], out["bed_left_calibrated_pressure_pct"])
    out["pressure_active"] = pd.to_numeric(pd.Series(pressure_col, index=out.index), errors="coerce")
    out["pressure_abs_delta"] = out.groupby(["zone", "night"])["pressure_active"].diff().abs().fillna(0)
    stage = out["sleep_stage"].astype(str).str.lower()
    out["stage_bad"] = stage.isin(["awake", "unknown", "inbed"]).astype(float)

    body = out["body_left_f"]
    pred = out["pred_setting"]
    out["too_cold_proxy"] = ((body < 76.0) & (pred <= -7)).astype(float)
    out["too_hot_proxy"] = ((body > np.where(out["zone"].eq("right"), 86.0, 84.0)) & (pred >= -5)).astype(float)
    out["spurious_override_pred"] = ((out["pred_setting"] - out["observed_effective"]).abs() >= 3)
    out["spurious_override_pred"] &= ~out["action"].eq("override")

    # Right-zone composite: override-absence trap guard.  Higher is worse.
    right_body = out["body_left_f"]
    body_range = np.maximum((right_body - 86.0) / 6.0, (73.0 - right_body) / 5.0).clip(0, 1)
    body_sd = ((out["body_30m_sd"] - 1.2) / 2.0).clip(0, 1).fillna(0)
    rest = (out["pressure_abs_delta"] / 8.0).clip(0, 1).fillna(0)
    out["right_comfort_proxy"] = 0.35 * body_range + 0.25 * body_sd + 0.20 * out["stage_bad"] + 0.20 * rest
    return out


def summarize_metrics(rp: pd.DataFrame, splits: EvalSplits) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]]:
    ov = override_frame(rp)
    primary: dict[str, Any] = {"overall": {}, "by_zone": {}, "final_test": {}}
    if not ov.empty:
        primary["overall"] = summarize_override_errors(ov)
        for zone, z in ov.groupby("zone"):
            primary["by_zone"][zone] = summarize_override_errors(z)
        final_nights = [(z, n) for z, ns in splits.final_test_nights.items() for n in ns]
        mask = pd.Series(False, index=ov.index)
        for z, n in final_nights:
            mask |= ((ov["zone"] == z) & (ov["night"] == n))
        primary["final_test"] = summarize_override_errors(ov[mask])

    secondary = {
        "time_too_cold_min": minutes_sum(rp, "too_cold_proxy"),
        "time_too_hot_min": minutes_sum(rp, "too_hot_proxy"),
        "spurious_override_predicted_count": count_runs(rp, "spurious_override_pred"),
        "override_precluded_count": int(ov["would_preclude"].sum()) if not ov.empty else 0,
        "override_precluded_rate": float(ov["would_preclude"].mean()) if not ov.empty else None,
    }
    for zone, z in rp.groupby("zone"):
        secondary[f"{zone}_time_too_cold_min"] = minutes_sum(z, "too_cold_proxy")
        secondary[f"{zone}_time_too_hot_min"] = minutes_sum(z, "too_hot_proxy")

    r = rp[rp["zone"] == "right"]
    right_proxy = {
        "n_rows": int(len(r)),
        "mean": safe_float(r["right_comfort_proxy"].mean()),
        "p90": safe_float(r["right_comfort_proxy"].quantile(0.90)) if len(r) else None,
        "minutes_score_ge_0_5": safe_float(r.loc[r["right_comfort_proxy"] >= 0.5, "dt_min"].sum()) if len(r) else 0.0,
        "definition": "0.35*body_out_of_range(73..86F) + 0.25*body_30m_sd_excess(>1.2F) + 0.20*awake/unknown + 0.20*pressure_abs_delta/8",
    }
    split_summary = {
        "n_cv_folds": len(splits.cv_folds),
        "final_test_nights": splits.final_test_nights,
        "holdout_n_recent": splits.holdout_n_recent,
    }
    return primary, secondary, right_proxy, split_summary


def summarize_override_errors(ov: pd.DataFrame) -> dict[str, Any]:
    if ov.empty:
        return {"n": 0, "mae": None, "hit_rate_abs_le_1": None, "bias": None, "baseline_mae_observed": None}
    return {
        "n": int(len(ov)),
        "mae": safe_float(ov["pred_abs_err"].mean()),
        "hit_rate_abs_le_1": safe_float(ov["pred_hit_1"].mean()),
        "bias": safe_float((ov["pred_setting"] - ov["human_setting"]).mean()),
        "baseline_mae_observed": safe_float(ov["baseline_abs_err"].mean()),
        "pred_better_than_observed_count": int((ov["pred_abs_err"] < ov["baseline_abs_err"]).sum()),
        "pred_worse_than_observed_count": int((ov["pred_abs_err"] > ov["baseline_abs_err"]).sum()),
    }


def safe_float(x: Any) -> float | None:
    try:
        if pd.isna(x):
            return None
        return float(x)
    except Exception:
        return None


def minutes_sum(df: pd.DataFrame, col: str) -> float:
    if df.empty or col not in df:
        return 0.0
    return safe_float((df[col].astype(float) * df["dt_min"].astype(float)).sum()) or 0.0


def count_runs(df: pd.DataFrame, col: str) -> int:
    if df.empty or col not in df:
        return 0
    total = 0
    for _, g in df.sort_values("ts").groupby(["zone", "night"]):
        s = g[col].fillna(False).astype(bool)
        total += int((s & ~s.shift(fill_value=False)).sum())
    return total


def bootstrap_power(rp: pd.DataFrame, n_boot: int = 2000, seed: int = 20260501) -> dict[str, Any]:
    ov = override_frame(rp)
    if ov.empty:
        return {"n_overrides": 0}
    rng = np.random.default_rng(seed)
    by_night = [g for _, g in ov.groupby(["zone", "night"])]
    diffs = []
    maes = []
    for _ in range(n_boot):
        sample = pd.concat([by_night[i] for i in rng.integers(0, len(by_night), len(by_night))])
        diff = sample["pred_abs_err"].mean() - sample["baseline_abs_err"].mean()
        diffs.append(diff)
        maes.append(sample["pred_abs_err"].mean())
    n = len(ov)
    sd_paired = float((ov["pred_abs_err"] - ov["baseline_abs_err"]).std(ddof=1)) if n > 1 else None
    sd_baseline = float(ov["baseline_abs_err"].std(ddof=1)) if n > 1 else None
    mde_paired = None if sd_paired is None else 2.80 * sd_paired / math.sqrt(n)
    mde_vs_noisy_baseline = None if sd_baseline is None else 2.80 * sd_baseline / math.sqrt(n)
    by_zone = {}
    for zone, z in ov.groupby("zone"):
        zn = len(z)
        zsd = float(z["baseline_abs_err"].std(ddof=1)) if zn > 1 else None
        by_zone[zone] = {
            "n_overrides": int(zn),
            "baseline_abs_error_sd": zsd,
            "approx_mde_80pct_power_alpha_0_05_mae_steps": None if zsd is None else 2.80 * zsd / math.sqrt(zn),
        }
    return {
        "n_overrides": int(n),
        "n_zone_nights_with_overrides": len(by_night),
        "bootstrap_block": "zone-night",
        "bootstrap_policy_mae_ci95": [safe_float(np.quantile(maes, 0.025)), safe_float(np.quantile(maes, 0.975))],
        "bootstrap_mae_diff_vs_observed_ci95": [safe_float(np.quantile(diffs, 0.025)), safe_float(np.quantile(diffs, 0.975))],
        "paired_sd_abs_error_diff": sd_paired,
        "mde_80pct_power_alpha_0_05_paired_mae_steps": mde_paired,
        "baseline_abs_error_sd": sd_baseline,
        "approx_mde_80pct_power_alpha_0_05_mae_steps": mde_vs_noisy_baseline,
        "by_zone": by_zone,
        "claim_rule": "candidate must improve MAE by >= max(0.5 L-step, MDE) and zone-night bootstrap 95% CI for MAE(candidate)-MAE(v5.2) must be < 0; no case-test failures",
    }


def run_eval(policy: Policy, splits: EvalSplits | None = None, db_conn=None) -> EvalResult:
    df = load_data(db_conn)
    if splits is None:
        splits = build_splits(df)
    rp = add_comfort_proxies(replay(policy, df))
    primary, secondary, right_proxy, split_summary = summarize_metrics(rp, splits)
    cases = {cid: asdict(case_test(policy, cid, df=df)) for cid in CASE_DEFS}
    return EvalResult(
        policy_name=getattr(policy, "name", policy.__class__.__name__),
        generated_at=datetime.now().isoformat(timespec="seconds"),
        primary=primary,
        secondary=secondary,
        right_comfort_proxy=right_proxy,
        splits=split_summary,
        power=bootstrap_power(rp),
        cases=cases,
        rows={"readings": int(len(df)), "replayed": int(len(rp)), "overrides": int(len(override_frame(rp)))},
    )


def case_test(policy: Policy, case_id: str, df: pd.DataFrame | None = None) -> CaseResult:
    if case_id not in CASE_DEFS:
        raise ValueError(f"unknown case_id {case_id!r}; expected one of {sorted(CASE_DEFS)}")
    if df is None:
        df = load_data()
    c = CASE_DEFS[case_id]
    start = pd.Timestamp(c["start"])
    end = pd.Timestamp(c["end"])
    # Replay the full zone-night so history-dependent policies get only the
    # earlier same-night context they would have had online; filter to the case
    # window only after replay.
    history_start = pd.Timestamp(f"{(start - pd.Timedelta(days=1)).date()} 18:00:00", tz=start.tz)
    night_df = df[(df["zone"] == c["zone"]) & (df["ts"] >= history_start) & (df["ts"] <= end)].copy()
    rp_all = add_comfort_proxies(replay(policy, night_df)) if not night_df.empty else pd.DataFrame()
    rp = rp_all[(rp_all["ts"] >= start) & (rp_all["ts"] <= end)].copy() if not rp_all.empty else pd.DataFrame()
    if rp.empty:
        return CaseResult(case_id, False, {"reason": "no rows", "definition": c})
    if case_id == "A":
        ev = rp[(rp["ts"] >= pd.Timestamp(c["eval_start"])) & (rp["ts"] <= pd.Timestamp(c["eval_end"]))]
        mins_ge_m6 = float(ev.loc[ev["pred_setting"] >= -6, "dt_min"].sum())
        med_setting = safe_float(ev["pred_setting"].median())
        med_delta = safe_float(ev["cf_surface_delta_f"].median())
        passed = bool((med_setting is not None and med_setting >= -5) and mins_ge_m6 >= 20 and (med_delta is not None and med_delta >= 1.0))
        details = {"definition": c, "median_setting": med_setting, "minutes_setting_ge_-6": mins_ge_m6, "median_cf_surface_delta_f": med_delta}
    elif case_id == "B":
        ev = rp[(rp["ts"] >= pd.Timestamp(c["eval_start"])) & (rp["ts"] <= pd.Timestamp(c["eval_end"]))]
        mins_le_m5 = float(ev.loc[ev["pred_setting"] <= -5, "dt_min"].sum())
        near = rp[(rp["ts"] >= pd.Timestamp("2026-05-01 03:20:00-04:00")) & (rp["ts"] <= pd.Timestamp("2026-05-01 03:31:00-04:00"))]
        med_near = safe_float(near["pred_setting"].median())
        passed = bool(mins_le_m5 >= 15 and med_near is not None and med_near <= -5)
        details = {"definition": c, "minutes_setting_le_-5": mins_le_m5, "median_setting_0320_0331": med_near}
    else:
        warm = rp[(rp["ts"] >= pd.Timestamp(c["warm_start"])) & (rp["ts"] <= pd.Timestamp(c["warm_end"]))]
        cool = rp[(rp["ts"] >= pd.Timestamp(c["cool_start"])) & (rp["ts"] <= pd.Timestamp(c["cool_end"]))]
        warm_med = safe_float(warm["pred_setting"].median())
        cool_med = safe_float(cool["pred_setting"].median())
        passed = bool(warm_med is not None and warm_med >= -3 and cool_med is not None and cool_med <= -4)
        details = {"definition": c, "midnight_warm_window_median_setting": warm_med, "morning_cool_window_median_setting": cool_med}
    details["trajectory"] = rp[["ts", "zone", "elapsed_min", "body_left_f", "body_center_f", "room_temp_f", "observed_effective", "pred_setting", "cf_surface_delta_f"]].to_dict("records")
    return CaseResult(case_id, passed, details)


def _load_policy(name: str) -> Policy:
    if name in {"baseline", "v5.2", "v52", "v5.2_reference", "v5_2", "v5_2_actual"}:
        return V52BaselinePolicy()
    if name in {"v6_synth", "shadow_compare"}:
        # Try to load the real v6 policy; fall back to v5.2 wrapper with a
        # console warning so the harness still runs end-to-end pre-R1B.
        try:
            from ml.v6.policy import V6SynthPolicy  # type: ignore
            return V6SynthPolicy()
        except ImportError as exc:
            print(f"[v6_eval] ml.v6.policy not available ({exc}); "
                  f"falling back to V52 reference policy for {name}", file=sys.stderr)
            return V52BaselinePolicy()
    if ":" not in name:
        raise SystemExit("--policy must be 'baseline', 'v5_2_actual', 'v6_synth', "
                         "'shadow_compare', or module.path:ClassName")
    mod_name, cls_name = name.split(":", 1)
    import importlib
    mod = importlib.import_module(mod_name)
    cls = getattr(mod, cls_name)
    return cls()


# ---------------------------------------------------------------------------
# v6 R1A — §11.3 rollback-criteria metrics
# ---------------------------------------------------------------------------

V6_RIGHT_HOT_F = 86.0
V6_LEFT_HOT_F = 84.0
V6_COLD_F = 72.0


def _compute_dt_min(df: pd.DataFrame) -> pd.Series:
    return (
        df.sort_values("ts")
        .groupby(["zone", "night"])["ts"]
        .diff()
        .dt.total_seconds()
        .div(60)
        .clip(0.5, 30)
        .fillna(5.0)
    )


def _safe_import(modpath: str):
    try:
        import importlib
        return importlib.import_module(modpath), None
    except ImportError as exc:
        return None, str(exc)


def compute_v6_night_metrics(df: pd.DataFrame) -> list[dict[str, Any]]:
    """Compute per-night, per-zone §11.3 metrics from raw controller_readings.

    Operates on the actual logged rows (no policy replay). Used by
    --policy v5_2_actual to produce the historical baseline, and by
    --policy shadow_compare as the v5.2 leg of the comparison.
    """
    if df.empty:
        return []
    work = df.copy()
    work["ts"] = pd.to_datetime(work["ts"], utc=True)
    if "night" not in work.columns:
        work["night"] = (work["ts"] - pd.Timedelta(hours=18)).dt.date.astype(str)
    work["dt_min"] = _compute_dt_min(work)
    notes = work["notes"].fillna("").astype(str)
    body = pd.to_numeric(work.get("body_left_f"), errors="coerce")

    # §11.3 metric building blocks
    work["above_86f"] = (body > V6_RIGHT_HOT_F).fillna(False).astype(float)
    work["above_84f"] = (body > V6_LEFT_HOT_F).fillna(False).astype(float)
    work["below_72f"] = (body < V6_COLD_F).fillna(False).astype(float)
    work["is_override"] = work["action"].astype(str).str.lower().eq("override")
    work["rail_event"] = notes.str.contains("hot_rail|right_rail_engaged|rail_force",
                                             case=False, regex=True)
    # divergence guard activations come from the v6 column when present;
    # fall back to a notes scan for the same string.
    if "divergence_steps" in work.columns:
        work["divergence_event"] = pd.to_numeric(
            work["divergence_steps"], errors="coerce").fillna(0).gt(0)
    else:
        work["divergence_event"] = notes.str.contains("divergence_guard",
                                                       case=False, regex=False)

    # Try to use the v6 right_comfort_proxy if R1B has shipped it; otherwise
    # fall back to the in-file proxy used in the legacy harness.
    proxy_mod, proxy_err = _safe_import("ml.v6.right_comfort_proxy")
    if proxy_mod is not None and hasattr(proxy_mod, "score_frame"):
        work["right_comfort_proxy"] = proxy_mod.score_frame(work)
    else:
        work["right_comfort_proxy"] = float("nan")  # marker: not yet computed

    out: list[dict[str, Any]] = []
    for (zone, night), g in work.groupby(["zone", "night"], sort=True):
        rec = {
            "night": str(night),
            "zone": str(zone),
            "n_rows": int(len(g)),
            "override_count": int(g["is_override"].sum()),
            "minutes_above_86f": float((g["above_86f"] * g["dt_min"]).sum()),
            "minutes_above_84f": float((g["above_84f"] * g["dt_min"]).sum()),
            "minutes_below_72f": float((g["below_72f"] * g["dt_min"]).sum()),
            "rail_engagements": int(_count_event_starts(g["rail_event"])),
            "divergence_guard_activations": int(_count_event_starts(g["divergence_event"])),
            "right_comfort_proxy_min_ge_05": (
                float((g["right_comfort_proxy"].fillna(0).ge(0.5).astype(float) * g["dt_min"]).sum())
                if proxy_mod is not None else None
            ),
        }
        # Override MAE: the absolute |override_delta| sum/count for the night.
        ov = g[g["is_override"] & g["override_delta"].notna()]
        rec["override_mae_steps"] = (
            float(pd.to_numeric(ov["override_delta"], errors="coerce").abs().mean())
            if not ov.empty else None
        )
        out.append(rec)
    return out


def _count_event_starts(series: pd.Series) -> int:
    s = series.fillna(False).astype(bool).reset_index(drop=True)
    if s.empty:
        return 0
    starts = s & ~s.shift(fill_value=False)
    return int(starts.sum())


def aggregate_v6_metrics(per_night: list[dict[str, Any]]) -> dict[str, Any]:
    if not per_night:
        return {"n_nights": 0}
    df = pd.DataFrame(per_night)
    by_zone: dict[str, dict[str, Any]] = {}
    for zone, g in df.groupby("zone"):
        by_zone[str(zone)] = {
            "n_zone_nights": int(len(g)),
            "override_count_total": int(g["override_count"].sum()),
            "override_count_per_night": float(g["override_count"].mean()),
            "override_mae_steps_avg": float(
                pd.to_numeric(g["override_mae_steps"], errors="coerce").dropna().mean()
            ) if g["override_mae_steps"].notna().any() else None,
            "minutes_above_86f_per_night": float(g["minutes_above_86f"].mean()),
            "minutes_above_84f_per_night": float(g["minutes_above_84f"].mean()),
            "minutes_below_72f_per_night": float(g["minutes_below_72f"].mean()),
            "rail_engagements_total": int(g["rail_engagements"].sum()),
            "divergence_guard_activations_total": int(g["divergence_guard_activations"].sum()),
            "right_comfort_proxy_min_ge_05_per_night": (
                float(g["right_comfort_proxy_min_ge_05"].dropna().mean())
                if g["right_comfort_proxy_min_ge_05"].notna().any() else None
            ),
        }
    return {
        "n_nights": int(df["night"].nunique()),
        "zones": sorted(df["zone"].unique().tolist()),
        "by_zone": by_zone,
    }


def _filter_recent_nights(df: pd.DataFrame, nights: int | None) -> pd.DataFrame:
    if not nights or df.empty or "night" not in df.columns:
        return df
    keep = sorted(df["night"].dropna().unique())[-nights:]
    return df[df["night"].isin(keep)].copy()


def run_v6_actual(db_conn=None, since: str | None = None,
                  nights: int | None = None) -> dict[str, Any]:
    df = load_data(db_conn)
    if since:
        df = df[df["ts"] >= pd.Timestamp(since, tz="UTC").tz_convert("America/New_York")]
    df = _filter_recent_nights(df, nights)
    per_night = compute_v6_night_metrics(df)
    return {
        "policy": "v5_2_actual",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "since": since,
        "n_nights_requested": nights,
        "rows": int(len(df)),
        "per_night": per_night,
        "aggregate": aggregate_v6_metrics(per_night),
    }


def _print_v6_table(result: dict[str, Any], compare: dict[str, Any] | None = None) -> None:
    agg = result.get("aggregate", {}).get("by_zone", {})
    print(f"\n=== v6 §11.3 metrics ({result.get('policy')}) — n_nights={result.get('aggregate',{}).get('n_nights')} ===")
    cmp_agg = (compare or {}).get("aggregate", {}).get("by_zone", {})
    cmp_label = compare.get("policy") if compare else None
    cols = [
        ("override_count_per_night", "ovr/night"),
        ("override_mae_steps_avg", "ovr_MAE"),
        ("minutes_above_86f_per_night", "min>86F"),
        ("minutes_above_84f_per_night", "min>84F"),
        ("minutes_below_72f_per_night", "min<72F"),
        ("rail_engagements_total", "rail_eng"),
        ("divergence_guard_activations_total", "div_guard"),
        ("right_comfort_proxy_min_ge_05_per_night", "proxy>=0.5"),
    ]
    header = "zone".ljust(7) + "".join(label.rjust(13) for _, label in cols)
    if compare:
        header += "  | " + cmp_label.rjust(20)
    print(header)
    for zone in sorted(set(list(agg.keys()) + list(cmp_agg.keys()))):
        row = zone.ljust(7)
        for key, _ in cols:
            v = (agg.get(zone) or {}).get(key)
            row += (f"{v:13.2f}" if isinstance(v, (int, float)) else "         n/a")
        if compare:
            row += "  |"
            for key, _ in cols:
                v = (cmp_agg.get(zone) or {}).get(key)
                row += (f"{v:13.2f}" if isinstance(v, (int, float)) else "         n/a")
        print(row)


def run_shadow_compare(db_conn=None, since: str | None = None,
                       nights: int | None = None) -> dict[str, Any]:
    actual = run_v6_actual(db_conn=db_conn, since=since, nights=nights)
    v6_mod, v6_err = _safe_import("ml.v6.policy")
    shadow: dict[str, Any]
    if v6_mod is None:
        shadow = {
            "policy": "v6_synth",
            "status": "module_not_built",
            "import_error": v6_err,
            "aggregate": {"n_nights": 0, "by_zone": {}},
            "per_night": [],
        }
    else:  # pragma: no cover — exercised after R1B lands
        policy = v6_mod.V6SynthPolicy()
        df = load_data(db_conn)
        if since:
            df = df[df["ts"] >= pd.Timestamp(since, tz="UTC").tz_convert("America/New_York")]
        df = _filter_recent_nights(df, nights)
        rp = replay(policy, df)
        per_night = compute_v6_night_metrics(rp)
        shadow = {
            "policy": "v6_synth",
            "rows": int(len(rp)),
            "per_night": per_night,
            "aggregate": aggregate_v6_metrics(per_night),
        }
    return {
        "policy": "shadow_compare",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "since": since,
        "n_nights_requested": nights,
        "v5_2_actual": actual,
        "v6_synth": shadow,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Run honest v6 PerfectlySnug policy evaluation")
    ap.add_argument("--policy", default="baseline",
                    help="baseline | v5_2 | v5_2_actual | v6_synth | shadow_compare | module.path:ClassName")
    ap.add_argument("--final-nights", type=int, default=3)
    ap.add_argument("--json", action="store_true", help="print full JSON result")
    ap.add_argument("--out", type=Path, help="write JSON result to this path")
    ap.add_argument("--case", choices=sorted(CASE_DEFS), help="run one case test only")
    ap.add_argument("--since", default=None, help="ISO timestamp lower bound (e.g. 2026-04-01)")
    ap.add_argument("--nights", type=int, default=None,
                    help="restrict to the most recent N calendar nights")
    ap.add_argument("--findings-dir", type=Path, default=ROOT / "findings",
                    help="when set with --policy v5_2_actual|v6_synth|shadow_compare, "
                         "auto-write a timestamped JSON in this directory")
    args = ap.parse_args(argv)

    # New v6 R1A modes: §11.3 metrics over raw history (no replay) or via
    # shadow_compare with the v6_synth policy when available.
    if args.policy in {"v5_2_actual", "v5_2", "v52_actual"}:
        result = run_v6_actual(since=args.since, nights=args.nights)
        _print_v6_table(result)
        out = args.out or _default_findings_path(args.findings_dir, "v5_2_actual")
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(result, indent=2, default=str) + "\n")
        print(f"wrote {out}")
        return 0
    if args.policy in {"shadow_compare", "v6_synth"}:
        if args.policy == "v6_synth":
            # Run only the v6 leg (still goes through shadow_compare so we get
            # the actual baseline alongside for reference).
            result = run_shadow_compare(since=args.since, nights=args.nights)
        else:
            result = run_shadow_compare(since=args.since, nights=args.nights)
        _print_v6_table(result["v5_2_actual"], compare=result["v6_synth"])
        out = args.out or _default_findings_path(args.findings_dir, args.policy)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(result, indent=2, default=str) + "\n")
        print(f"wrote {out}")
        return 0

    policy = _load_policy(args.policy)
    df = load_data()
    if args.case:
        result = case_test(policy, args.case, df=df)
        print(json.dumps(asdict(result), indent=2, default=str, allow_nan=False))
        return 0 if result.passed else 2

    splits = build_splits(df, final_nights=args.final_nights)
    res = run_eval(policy, splits=splits, db_conn=None)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(res.to_json() + "\n")
    if args.json:
        print(res.to_json())
    else:
        p = res.primary.get("overall", {})
        left = res.primary.get("by_zone", {}).get("left", {})
        right = res.primary.get("by_zone", {}).get("right", {})
        print(f"policy={res.policy_name}")
        print(f"overall overrides n={p.get('n')} MAE={p.get('mae')} hit@1={p.get('hit_rate_abs_le_1')} observed_v5.2_MAE={p.get('baseline_mae_observed')}")
        print(f"left n={left.get('n')} MAE={left.get('mae')} | right n={right.get('n')} MAE={right.get('mae')}")
        print(f"right comfort proxy mean={res.right_comfort_proxy.get('mean')} p90={res.right_comfort_proxy.get('p90')}")
        print("cases=" + ", ".join(f"{k}:{'PASS' if v['passed'] else 'FAIL'}" for k, v in res.cases.items()))
        if args.out:
            print(f"wrote {args.out}")
    return 0


def _default_findings_path(findings_dir: Path, policy: str) -> Path:
    ts = datetime.now().strftime("%Y%m%dT%H%M%S")
    return findings_dir / f"v6_eval_{policy}_{ts}.json"


if __name__ == "__main__":
    raise SystemExit(main())
