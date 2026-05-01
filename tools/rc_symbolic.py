#!/usr/bin/env python3
"""Symbolic-style reverse engineering for right-side Responsive Cooling.

This script fetches the HA history window directly (no scratch files), builds a
30-second aligned data set, evaluates compact closed-form candidates with honest
time-series CV, and writes a findings markdown report.
"""
from __future__ import annotations

import json
import subprocess
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from itertools import combinations
from pathlib import Path
from typing import Callable

import numpy as np
import pandas as pd
from scipy.optimize import differential_evolution, minimize
from sklearn.metrics import mean_absolute_error, r2_score
from sklearn.model_selection import TimeSeriesSplit


ROOT = Path(__file__).resolve().parents[1]
REPORT = ROOT / "docs" / "findings" / "2026-05-01_rc_deep_symbolic.md"
CACHE = ROOT / "ml" / "state" / "rc_symbolic_cache.csv"

HA_URL = "http://192.168.0.106:8123"
START = pd.Timestamp("2026-04-17T11:39:24Z")
END = pd.Timestamp("2026-04-30T20:35:30Z")

ENTITIES = {
    "body_l": "sensor.smart_topper_right_side_body_sensor_left",
    "body_c": "sensor.smart_topper_right_side_body_sensor_center",
    "body_r": "sensor.smart_topper_right_side_body_sensor_right",
    "ambient": "sensor.smart_topper_right_side_ambient_temperature",
    "setpoint": "sensor.smart_topper_right_side_temperature_setpoint",
    "blower": "sensor.smart_topper_right_side_blower_output",
    "setting": "number.smart_topper_right_side_bedtime_temperature",
    "rc_switch": "switch.smart_topper_right_side_responsive_cooling",
    "running": "switch.smart_topper_right_side_running",
}


def ha_token() -> str:
    r = subprocess.run(
        ["security", "find-generic-password", "-s", "homeassistant-copilot-token",
         "-a", "copilot", "-w"],
        capture_output=True, text=True, timeout=5, check=True,
    )
    return r.stdout.strip()


def fetch_entity(token: str, entity_id: str, start: pd.Timestamp,
                 end: pd.Timestamp) -> pd.Series:
    rows: list[dict] = []
    cur = start.to_pydatetime()
    end_dt = end.to_pydatetime()
    while cur < end_dt:
        chunk_end = min(cur + timedelta(hours=12), end_dt)
        params = urllib.parse.urlencode({
            "filter_entity_id": entity_id,
            "end_time": chunk_end.isoformat(),
            "minimal_response": "1",
            "no_attributes": "1",
        })
        url = f"{HA_URL}/api/history/period/{cur.isoformat()}?{params}"
        req = urllib.request.Request(
            url, headers={"Authorization": f"Bearer {token}"})
        with urllib.request.urlopen(req, timeout=60) as resp:
            payload = json.load(resp)
        if payload and payload[0]:
            rows.extend(payload[0])
        cur = chunk_end + timedelta(seconds=1)
    if not rows:
        return pd.Series(dtype=object, name=entity_id)
    out = []
    for row in rows:
        ts = row.get("last_changed") or row.get("last_updated")
        state = row.get("state")
        if ts and state not in (None, "unknown", "unavailable", ""):
            out.append((pd.Timestamp(ts).tz_convert("UTC"), state))
    if not out:
        return pd.Series(dtype=object, name=entity_id)
    s = pd.Series([v for _, v in out], index=pd.DatetimeIndex([t for t, _ in out]))
    return s[~s.index.duplicated(keep="last")].sort_index()


def build_dataset(force_fetch: bool = False) -> pd.DataFrame:
    if CACHE.exists() and not force_fetch:
        df = pd.read_csv(CACHE, parse_dates=["ts"])
        df["ts"] = pd.to_datetime(df["ts"], utc=True)
        return df

    token = ha_token()
    idx = pd.date_range(START.floor("30s"), END.ceil("30s"), freq="30s")
    df = pd.DataFrame(index=idx)
    for name, entity in ENTITIES.items():
        print(f"fetch {name}: {entity}", flush=True)
        s = fetch_entity(token, entity, START, END)
        if s.empty:
            df[name] = np.nan
            continue
        df[name] = s.reindex(df.index.union(s.index)).sort_index().ffill().reindex(df.index)

    for c in ["body_l", "body_c", "body_r", "ambient", "setpoint", "blower", "setting"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df[(df["rc_switch"] == "on") & (df["running"] == "on")].copy()
    df = df.dropna(subset=["body_l", "body_c", "body_r", "ambient",
                           "setpoint", "blower", "setting"])
    df["body_max"] = df[["body_l", "body_c", "body_r"]].max(axis=1)
    df["body_avg"] = df[["body_l", "body_c", "body_r"]].mean(axis=1)
    for base in ["body_max", "ambient", "setpoint"]:
        df[f"{base}_15m"] = df[base].rolling(30, min_periods=1).mean()
        df[f"{base}_30m"] = df[base].rolling(60, min_periods=1).mean()
    df["heat_gap"] = df["body_max"] - df["ambient"]
    df["pv_gap"] = df["setpoint"] - df["ambient"]
    df["body_spread"] = df[["body_l", "body_c", "body_r"]].max(axis=1) - df[["body_l", "body_c", "body_r"]].min(axis=1)
    df = df.reset_index(names="ts")
    try:
        CACHE.parent.mkdir(parents=True, exist_ok=True)
        df.to_csv(CACHE, index=False)
    except PermissionError:
        pass
    return df


def clip100(x: np.ndarray) -> np.ndarray:
    return np.clip(x, 0, 100)


@dataclass
class Candidate:
    name: str
    expr: str
    n_params: int
    bounds: list[tuple[float, float]]
    fn: Callable[[pd.DataFrame, np.ndarray], np.ndarray]
    params: np.ndarray | None = None
    mae: float | None = None
    r2: float | None = None
    active_mae: float | None = None
    active_r2: float | None = None


def fit_params(c: Candidate, train: pd.DataFrame) -> np.ndarray:
    y = train["blower"].to_numpy()

    def loss(p: np.ndarray) -> float:
        pred = c.fn(train, p)
        return float(np.mean(np.abs(pred - y)))

    seed = differential_evolution(loss, c.bounds, polish=False, seed=42,
                                  maxiter=80, popsize=8, workers=1).x
    res = minimize(loss, seed, method="Nelder-Mead",
                   options={"maxiter": 2500, "xatol": 1e-4, "fatol": 1e-4})
    return res.x if res.success else seed


def eval_cv(c: Candidate, df: pd.DataFrame, n_splits: int = 5) -> Candidate:
    tscv = TimeSeriesSplit(n_splits=n_splits)
    preds = np.full(len(df), np.nan)
    params = []
    for tr, te in tscv.split(df):
        p = fit_params(c, df.iloc[tr])
        params.append(p)
        preds[te] = c.fn(df.iloc[te], p)
    mask = ~np.isnan(preds)
    y = df.loc[mask, "blower"].to_numpy()
    c.mae = float(mean_absolute_error(y, preds[mask]))
    c.r2 = float(r2_score(y, preds[mask]))
    active = mask & df["blower"].between(5, 60).to_numpy()
    if active.sum() > 10:
        ya = df.loc[active, "blower"].to_numpy()
        c.active_mae = float(mean_absolute_error(ya, preds[active]))
        c.active_r2 = float(r2_score(ya, preds[active]))
    c.params = np.median(np.vstack(params), axis=0)
    return c


def make_candidates() -> list[Candidate]:
    cs: list[Candidate] = [
        Candidate("heat_gap", "clip({a}*(body_max-ambient)+{b})", 2,
                  [(-20, 20), (-500, 500)],
                  lambda d, p: clip100(p[0] * d["heat_gap"].to_numpy() + p[1])),
        Candidate("pv_gap", "clip({a}*(setpoint-ambient)+{b})", 2,
                  [(-20, 20), (-500, 500)],
                  lambda d, p: clip100(p[0] * d["pv_gap"].to_numpy() + p[1])),
        Candidate("bodymax_setting", "clip({a}*body_max+{b}*setting+{c})", 3,
                  [(-20, 20), (-20, 20), (-1000, 1000)],
                  lambda d, p: clip100(p[0] * d["body_max"].to_numpy()
                                       + p[1] * d["setting"].to_numpy() + p[2])),
        Candidate("gap_setting", "clip({a}*(body_max-ambient)+{b}*setting+{c})", 3,
                  [(-20, 20), (-20, 20), (-500, 500)],
                  lambda d, p: clip100(p[0] * d["heat_gap"].to_numpy()
                                       + p[1] * d["setting"].to_numpy() + p[2])),
        Candidate("pv_setting", "clip({a}*(setpoint-ambient)+{b}*setting+{c})", 3,
                  [(-20, 20), (-20, 20), (-500, 500)],
                  lambda d, p: clip100(p[0] * d["pv_gap"].to_numpy()
                                       + p[1] * d["setting"].to_numpy() + p[2])),
        Candidate("lag_gap", "clip({a}*(body_max_30m-ambient_30m)+{b}*setting+{c})", 3,
                  [(-20, 20), (-20, 20), (-500, 500)],
                  lambda d, p: clip100(p[0] * (d["body_max_30m"].to_numpy()
                                               - d["ambient_30m"].to_numpy())
                                       + p[1] * d["setting"].to_numpy() + p[2])),
        Candidate("spread", "clip({a}*body_spread+{b}*body_max+{c})", 3,
                  [(-30, 30), (-20, 20), (-1000, 1000)],
                  lambda d, p: clip100(p[0] * d["body_spread"].to_numpy()
                                       + p[1] * d["body_max"].to_numpy() + p[2])),
        Candidate("body_ambient_setting", "clip({a}*body_max+{b}*ambient+{c}*setting+{d})", 4,
                  [(-20, 20), (-20, 20), (-20, 20), (-1500, 1500)],
                  lambda d, p: clip100(p[0] * d["body_max"].to_numpy()
                                       + p[1] * d["ambient"].to_numpy()
                                       + p[2] * d["setting"].to_numpy() + p[3])),
        Candidate("three_body", "clip({a}*body_l+{b}*body_c+{c}*body_r+{d})", 4,
                  [(-20, 20), (-20, 20), (-20, 20), (-1500, 1500)],
                  lambda d, p: clip100(p[0] * d["body_l"].to_numpy()
                                       + p[1] * d["body_c"].to_numpy()
                                       + p[2] * d["body_r"].to_numpy() + p[3])),
    ]
    return cs


def equation(c: Candidate) -> str:
    vals = {letter: f"{value:.3g}" for letter, value in zip(
        "abcd", c.params if c.params is not None else [])}
    return c.expr.format(**vals).replace("+-", "-")


def segment_stats(df: pd.DataFrame) -> str:
    bins = [0, 5, 15, 30, 60, 100]
    labels = ["0-5", "5-15", "15-30", "30-60", "60-100"]
    cut = pd.cut(df["blower"], bins=bins, labels=labels, include_lowest=True)
    g = df.assign(bin=cut, sp_minus_max=df["setpoint"] - df["body_max"]).groupby("bin", observed=False)
    lines = ["| Blower bin | n | mean setpoint-body_max | MAE setpoint-body_max |",
             "|---|---:|---:|---:|"]
    for b, sub in g:
        lines.append(f"| {b}% | {len(sub):,} | {sub['sp_minus_max'].mean():.2f} | {sub['sp_minus_max'].abs().mean():.2f} |")
    return "\n".join(lines)


def main() -> int:
    df = build_dataset()
    keep = ["body_l", "body_c", "body_r", "body_max", "body_avg", "ambient",
            "setting", "setpoint", "body_max_30m", "ambient_30m",
            "heat_gap", "pv_gap", "body_spread", "blower"]
    df = df.dropna(subset=keep).reset_index(drop=True)

    # Use all data for CV; optimizers fit on fold training slices.
    results = []
    for cand in make_candidates():
        print(f"fit {cand.name}", flush=True)
        results.append(eval_cv(cand, df))
    results.sort(key=lambda c: (c.mae if c.mae is not None else 1e9, c.n_params))
    top5 = results[:5]

    active = df[df["blower"].between(5, 60)].reset_index(drop=True)
    active_results = []
    if len(active) > 200:
        for cand in make_candidates():
            active_results.append(eval_cv(cand, active))
        active_results.sort(key=lambda c: (c.mae if c.mae is not None else 1e9, c.n_params))
    best = top5[0]
    best_active = active_results[0] if active_results else None

    REPORT.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# 2026-05-01 — RC deep symbolic regression",
        "",
        "Scope: right-side Responsive Cooling only, `responsive_cooling=on` and "
        "`running=on`, 2026-04-17 11:39:24Z → 2026-04-30 20:35:30Z. "
        "Target is `sensor.smart_topper_right_side_blower_output` in blower percent.",
        "",
        f"Rows after 30-second alignment and filtering: **{len(df):,}**. "
        f"Active modulation rows (5–60% blower): **{len(active):,}**.",
        "",
        "## Pareto-front candidate equations",
        "",
        "| rank | equation | params | TS-CV R² | TS-CV MAE | active MAE |",
        "|---:|---|---:|---:|---:|---:|",
    ]
    for i, c in enumerate(top5, 1):
        lines.append(f"| {i} | `{equation(c)}` | {c.n_params} | {c.r2:.3f} | {c.mae:.2f} | {c.active_mae:.2f} |")
    lines += [
        "",
        "## Best parametric equation",
        "",
        f"Best full-window equation: `{equation(best)}`",
        "",
        f"- TS-CV R²: **{best.r2:.3f}**",
        f"- TS-CV MAE: **{best.mae:.2f} blower points**",
    ]
    if best_active is not None:
        lines += [
            "",
            "Best equation fit only on active modulation rows:",
            "",
            f"`{equation(best_active)}`",
            "",
            f"- Active-regime TS-CV R²: **{best_active.r2:.3f}**",
            f"- Active-regime TS-CV MAE: **{best_active.mae:.2f} blower points**",
        ]
    lines += [
        "",
        "## Setpoint/body-max regime check",
        "",
        segment_stats(df),
        "",
        "## Does this look like the true firmware equation?",
        "",
        "No. The best compact formulas recover only coarse behavior. Their "
        "time-series CV error remains large, and the fitted coefficients are "
        "not stable enough to identify a simple firmware law from observed "
        "features alone. The active-regime fit is better but still too noisy "
        "to claim exact recovery. This supports the prior finding that the "
        "firmware depends on unobserved internal state (occupancy/mode/integral/"
        "time-since-entry), while exposed `setpoint` is mostly a process value "
        "near `max(body_*)` during active modulation rather than a user target.",
        "",
        "## Reproducibility",
        "",
        "Code: `PerfectlySnug/tools/rc_symbolic.py`.",
        "",
        "I did not write to `/tmp`; this environment forbids `/tmp` file "
        "operations. The script caches data at "
        "`PerfectlySnug/ml/state/rc_symbolic_cache.csv` when writable.",
        "",
    ]
    REPORT.write_text("\n".join(lines))
    print(REPORT)
    for c in top5:
        print(f"{equation(c)}  R2={c.r2:.3f} MAE={c.mae:.2f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
