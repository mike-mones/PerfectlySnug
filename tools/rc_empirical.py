#!/usr/bin/env python3
"""Empirical/nonparametric analysis for PerfectlySnug Responsive Cooling.

Input is a Home Assistant history JSON export containing the right-side
PerfectlySnug entities. Example:

    python PerfectlySnug/tools/rc_empirical.py snug_v2.json \
      --out-dir PerfectlySnug/docs/findings/rc_empirical_plots

The script filters to the confirmed RC-on window and requires both Responsive
Cooling and right-side running switches to be on.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from sklearn.base import BaseEstimator, RegressorMixin
from sklearn.ensemble import GradientBoostingRegressor, RandomForestRegressor
from sklearn.kernel_approximation import RBFSampler
from sklearn.linear_model import Ridge
from sklearn.metrics import mean_absolute_error, r2_score
from sklearn.model_selection import TimeSeriesSplit
from sklearn.neighbors import KNeighborsRegressor
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import PolynomialFeatures, StandardScaler


RC_START = pd.Timestamp("2026-04-17 11:39:24", tz="UTC")
RC_END = pd.Timestamp("2026-04-30 20:35:30", tz="UTC")


NUMERIC_ENTITIES = {
    "sensor.smart_topper_right_side_temperature_setpoint": "setpoint",
    "number.smart_topper_right_side_bedtime_temperature": "setting",
    "sensor.smart_topper_right_side_ambient_temperature": "ambient",
    "sensor.smart_topper_right_side_blower_output": "blower",
    "sensor.smart_topper_right_side_body_sensor_center": "body_c",
    "sensor.smart_topper_right_side_body_sensor_left": "body_l",
    "sensor.smart_topper_right_side_body_sensor_right": "body_r",
}

SWITCH_ENTITIES = {
    "switch.smart_topper_right_side_responsive_cooling": "rc_on",
    "switch.smart_topper_right_side_running": "running",
}


def series_from_history(arr: list[dict], numeric: bool) -> pd.Series:
    rows = []
    for row in arr:
        try:
            ts = pd.to_datetime(row["last_changed"], utc=True)
        except (KeyError, ValueError, TypeError):
            continue
        state = row.get("state")
        if numeric:
            try:
                state = float(state)
            except (ValueError, TypeError):
                continue
        rows.append((ts, state))
    if not rows:
        return pd.Series(dtype=float if numeric else object)
    s = pd.Series(dict(rows)).sort_index()
    return s[~s.index.duplicated(keep="last")]


def load_history(path: Path) -> pd.DataFrame:
    data = json.loads(path.read_text())
    series: dict[str, pd.Series] = {}
    for arr in data:
        if not arr:
            continue
        entity_id = arr[0].get("entity_id")
        if entity_id in NUMERIC_ENTITIES:
            series[NUMERIC_ENTITIES[entity_id]] = series_from_history(arr, numeric=True)
        elif entity_id in SWITCH_ENTITIES:
            series[SWITCH_ENTITIES[entity_id]] = series_from_history(arr, numeric=False)

    missing = {"setpoint", "ambient", "blower", "body_l", "body_c", "body_r"} - set(series)
    if missing:
        raise ValueError(f"Missing required entity histories: {sorted(missing)}")

    idx = pd.Index([])
    for s in series.values():
        idx = idx.union(s.index)
    df = pd.DataFrame({name: s.reindex(idx).ffill() for name, s in series.items()})
    df = df[(df.index >= RC_START) & (df.index <= RC_END)]
    if "rc_on" in df:
        df = df[df["rc_on"] == "on"]
    if "running" in df:
        df = df[df["running"] == "on"]
    df = df.drop(columns=[c for c in ("rc_on", "running") if c in df.columns])
    return df.dropna(subset=["setpoint", "ambient", "blower", "body_l", "body_c", "body_r"])


def make_features(df: pd.DataFrame, resample: str = "30s") -> pd.DataFrame:
    d = df.resample(resample).last().ffill().dropna().copy()
    d["body_max"] = d[["body_l", "body_c", "body_r"]].max(axis=1)
    d["body_avg"] = d[["body_l", "body_c", "body_r"]].mean(axis=1)
    d["body_min"] = d[["body_l", "body_c", "body_r"]].min(axis=1)
    d["body_spread"] = d["body_max"] - d["body_min"]
    for col in ["ambient", "body_max", "body_avg", "body_l", "body_c", "body_r"]:
        d[f"{col}_err"] = d[col] - d["setpoint"]

    # Occupied-only: empty bed makes body sensors collapse and blower often zero.
    d = d[d["body_max"] > 75].copy()

    # 30s grid lags: 1m=2 ticks, 5m=10 ticks.
    for col in ["body_max", "body_max_err", "body_avg_err", "ambient_err", "blower"]:
        for lag in [1, 2, 5, 10, 20, 60]:
            d[f"{col}_lag{lag}"] = d[col].shift(lag)

    for col, prefix in [
        ("body_max_err", "bmax"),
        ("body_avg_err", "bavg"),
        ("ambient_err", "amb"),
        ("body_l_err", "bl"),
        ("body_c_err", "bc"),
        ("body_r_err", "br"),
    ]:
        for seconds in [60, 180, 300, 600, 1800]:
            d[f"{prefix}_int{seconds}"] = d[col].rolling(max(1, seconds // 30)).mean()

    return d.dropna()


@dataclass
class CVResult:
    name: str
    r2_mean: float
    r2_std: float
    mae_mean: float
    mae_std: float


class BinnedLookupRegressor(BaseEstimator, RegressorMixin):
    """Rounded-grid lookup with nearest populated cell fallback."""

    def __init__(self, bin_width: float = 1.0):
        self.bin_width = bin_width

    def fit(self, X: np.ndarray, y: np.ndarray):
        Xb = np.round(X / self.bin_width) * self.bin_width
        df = pd.DataFrame(Xb)
        df["y"] = y
        grouped = df.groupby(list(range(X.shape[1])))["y"].mean().reset_index()
        self.centers_ = grouped[list(range(X.shape[1]))].to_numpy()
        self.values_ = grouped["y"].to_numpy()
        self.global_mean_ = float(np.mean(y))
        return self

    def predict(self, X: np.ndarray) -> np.ndarray:
        if len(self.centers_) == 0:
            return np.full(len(X), self.global_mean_)
        Xb = np.round(X / self.bin_width) * self.bin_width
        preds = np.empty(len(Xb))
        for i, row in enumerate(Xb):
            dist = np.sum((self.centers_ - row) ** 2, axis=1)
            preds[i] = self.values_[int(np.argmin(dist))]
        return preds


def ts_cv(
    df: pd.DataFrame,
    experiments: list[tuple[str, list[str], object]],
    n_splits: int = 5,
) -> list[CVResult]:
    y = df["blower"].to_numpy()
    out: list[CVResult] = []
    tscv = TimeSeriesSplit(n_splits=n_splits)
    for name, cols, model in experiments:
        X = df[cols].to_numpy()
        r2s, maes = [], []
        for train, test in tscv.split(X):
            model.fit(X[train], y[train])
            pred = np.clip(model.predict(X[test]), 0, 100)
            r2s.append(r2_score(y[test], pred))
            maes.append(mean_absolute_error(y[test], pred))
        out.append(
            CVResult(
                name,
                float(np.mean(r2s)),
                float(np.std(r2s)),
                float(np.mean(maes)),
                float(np.std(maes)),
            )
        )
    return sorted(out, key=lambda r: (r.mae_mean, -r.r2_mean))


def plot_heatmap(df: pd.DataFrame, x: str, y: str, out_dir: Path) -> None:
    xb = np.floor(df[x]).astype(int)
    yb = np.floor(df[y]).astype(int)
    table = df.assign(_x=xb, _y=yb).pivot_table(index="_y", columns="_x", values="blower", aggfunc="mean")
    std = df.assign(_x=xb, _y=yb).pivot_table(index="_y", columns="_x", values="blower", aggfunc="std")

    for name, data, cmap in [("mean", table, "viridis"), ("std", std, "magma")]:
        fig, ax = plt.subplots(figsize=(9, 6), constrained_layout=True)
        im = ax.imshow(data, origin="lower", aspect="auto", cmap=cmap)
        ax.set_title(f"blower {name}: {x} vs {y} (1°F bins)")
        ax.set_xlabel(x)
        ax.set_ylabel(y)
        ax.set_xticks(range(len(data.columns))[:: max(1, len(data.columns) // 8)])
        ax.set_xticklabels(data.columns[:: max(1, len(data.columns) // 8)])
        ax.set_yticks(range(len(data.index))[:: max(1, len(data.index) // 8)])
        ax.set_yticklabels(data.index[:: max(1, len(data.index) // 8)])
        fig.colorbar(im, ax=ax, label="blower %")
        fig.savefig(out_dir / f"rc_empirical_heatmap_{x}_{y}_{name}.png", dpi=150)
        plt.close(fig)


def plot_conditional_means(df: pd.DataFrame, cols: Iterable[str], out_dir: Path) -> None:
    fig, axes = plt.subplots(3, 3, figsize=(13, 10), constrained_layout=True)
    for ax, col in zip(axes.flat, cols):
        b = np.floor(df[col]).astype(int)
        agg = df.assign(_bin=b).groupby("_bin")["blower"].agg(["mean", "std", "count"])
        agg = agg[agg["count"] >= 20]
        ax.plot(agg.index, agg["mean"], marker="o")
        ax.fill_between(agg.index, agg["mean"] - agg["std"], agg["mean"] + agg["std"], alpha=0.15)
        ax.set_title(col)
        ax.set_ylabel("blower %")
    fig.suptitle("1D conditional means: mean blower by 1°F bin")
    fig.savefig(out_dir / "rc_empirical_conditional_means.png", dpi=150)
    plt.close(fig)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("json_path", type=Path)
    parser.add_argument("--out-dir", type=Path, default=Path("PerfectlySnug/docs/findings/rc_empirical_plots"))
    args = parser.parse_args()

    args.out_dir.mkdir(parents=True, exist_ok=True)
    raw = load_history(args.json_path)
    df = make_features(raw)
    print(f"rows={len(df)} start={df.index.min()} end={df.index.max()}")
    print(
        "blower "
        f"mean={df['blower'].mean():.2f} std={df['blower'].std():.2f} "
        f"min={df['blower'].min():.1f} max={df['blower'].max():.1f}"
    )

    plot_heatmap(df, "body_max", "ambient", args.out_dir)
    if "setting" in df:
        plot_heatmap(df, "body_max", "setting", args.out_dir)
    plot_heatmap(df, "setpoint", "ambient", args.out_dir)
    plot_conditional_means(
        df,
        [
            "body_max",
            "body_max_err",
            "ambient",
            "ambient_err",
            "setpoint",
            "body_avg",
            "body_spread",
            "bmax_int300",
            "amb_int600",
        ],
        args.out_dir,
    )

    f_2d = ["body_max", "ambient"]
    f_2d_err = ["body_max_err", "ambient_err"]
    f_5d = ["body_max", "body_max_lag2", "body_max_lag10", "ambient", "setpoint"]
    f_core = ["body_max_err", "ambient_err", "bmax_int300", "amb_int600", "setpoint"]
    f_rich = [
        c
        for c in df.columns
        if c != "blower" and not c.startswith("blower_lag") and pd.api.types.is_numeric_dtype(df[c])
    ]
    experiments = [
        ("lookup_1F / 2d_bodymax_ambient", f_2d, BinnedLookupRegressor(bin_width=1.0)),
        ("lookup_1F / 2d_errors", f_2d_err, BinnedLookupRegressor(bin_width=1.0)),
        ("lookup_1F / integral_core", f_core, BinnedLookupRegressor(bin_width=1.0)),
        ("knn_k25 / 5d_requested", f_5d, make_pipeline(StandardScaler(), KNeighborsRegressor(n_neighbors=25, weights="distance"))),
        ("knn_k100 / 5d_requested", f_5d, make_pipeline(StandardScaler(), KNeighborsRegressor(n_neighbors=100, weights="distance"))),
        ("rbf_sampler_ridge / integral_core", f_core, make_pipeline(StandardScaler(), RBFSampler(gamma=0.08, n_components=800, random_state=0), Ridge(alpha=10.0))),
        ("poly2_ridge / integral_core", f_core, make_pipeline(PolynomialFeatures(2, include_bias=False), StandardScaler(), Ridge(alpha=10.0))),
        ("rf / rich_no_blower_lag", f_rich, RandomForestRegressor(n_estimators=200, max_depth=15, n_jobs=-1, random_state=0)),
        ("gb / rich_no_blower_lag", f_rich, GradientBoostingRegressor(n_estimators=300, max_depth=4, random_state=0)),
    ]

    results = ts_cv(df, experiments)
    print("\n=== Time-series CV ===")
    for r in results:
        print(f"{r.name:42s} R²={r.r2_mean:+.3f}±{r.r2_std:.3f}  MAE={r.mae_mean:.2f}±{r.mae_std:.2f}%")


if __name__ == "__main__":
    main()
