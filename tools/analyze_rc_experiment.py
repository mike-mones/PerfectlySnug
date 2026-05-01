#!/usr/bin/env python3
"""
Analyze a Responsive Cooling reverse-engineering experiment.

Reads /config/rc_experiment_log.csv (fetched from HA), then pulls recorder
history for all relevant entities over each test window and fits simple models:

  * Test 1 (step response):   first-order plant + dead-time on blower vs setpoint error
  * Test 2 (icepack):         per-sensor sensitivity = Δblower / Δbody_sensor
  * Test 3 (BedJet heat):     blower response vs body-sensor delta from baseline
  * Test 4 (setpoint sweep):  static map setting -> steady-state blower

Usage:
  export HA_TOKEN="$(ssh root@192.168.0.106 'cat /config/.ha_token')"
  python3 analyze_rc_experiment.py \
      --ha-url http://192.168.0.106:8123 \
      --csv ./rc_experiment_log.csv \
      [--out report.md]

The CSV is most easily fetched with:
  scp root@192.168.0.106:/config/rc_experiment_log.csv .

Reports parameters with 95% confidence intervals where the fit supports it.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

import math

try:
    import numpy as np
except ImportError:
    sys.stderr.write("numpy required: pip install numpy\n")
    raise

try:
    from scipy.optimize import curve_fit  # type: ignore
    HAVE_SCIPY = True
except Exception:
    HAVE_SCIPY = False


ENTITIES = [
    "number.smart_topper_right_side_bedtime_temperature",
    "sensor.smart_topper_right_side_temperature_setpoint",
    "sensor.smart_topper_right_side_blower_output",
    "sensor.smart_topper_right_side_ambient_temperature",
    "sensor.smart_topper_right_side_body_sensor_left",
    "sensor.smart_topper_right_side_body_sensor_center",
    "sensor.smart_topper_right_side_body_sensor_right",
    "sensor.smart_topper_right_side_pid_control_output",
    "sensor.smart_topper_right_side_pid_integral_term",
    "sensor.smart_topper_right_side_pid_proportional_term",
    "sensor.smart_topper_right_side_run_progress",
    "sensor.bedjet_shar_ambient_temperature",
    "climate.bedjet_shar",
]


@dataclass
class TestWindow:
    name: str
    start: datetime
    end: datetime
    events: List[dict]


def parse_iso(s: str) -> datetime:
    # HA-emitted ISO with offset
    return datetime.fromisoformat(s)


def load_csv(path: str) -> List[TestWindow]:
    rows: List[dict] = []
    with open(path) as f:
        reader = csv.reader(f)
        for r in reader:
            if not r or r[0] == "header":
                continue
            if len(r) < 5:
                continue
            rows.append({
                "ts": parse_iso(r[0]),
                "test": r[1],
                "action": r[2],
                "entity": r[3],
                "value": r[4],
            })

    windows: Dict[str, TestWindow] = {}
    for row in rows:
        t = row["test"]
        if row["action"] == "test_start":
            windows[t] = TestWindow(name=t, start=row["ts"], end=row["ts"], events=[])
        elif row["action"] == "test_end" and t in windows:
            windows[t].end = row["ts"]
        elif t in windows:
            windows[t].events.append(row)
    return list(windows.values())


def ha_history(ha_url: str, token: str, entity_id: str,
               start: datetime, end: datetime) -> List[Tuple[datetime, Optional[float], str]]:
    start_iso = urllib.parse.quote(start.astimezone(timezone.utc).isoformat())
    url = f"{ha_url}/api/history/period/{start_iso}?filter_entity_id={entity_id}&end_time={urllib.parse.quote(end.astimezone(timezone.utc).isoformat())}&minimal_response&no_attributes"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=30) as r:
        data = json.loads(r.read())
    out: List[Tuple[datetime, Optional[float], str]] = []
    if not data:
        return out
    for s in data[0]:
        ts = parse_iso(s["last_changed"]) if "last_changed" in s else parse_iso(s["last_updated"])
        raw = s["state"]
        try:
            val = float(raw)
        except (TypeError, ValueError):
            val = None
        out.append((ts, val, raw))
    return out


def resample(series: List[Tuple[datetime, Optional[float], str]],
             start: datetime, end: datetime, dt_s: float = 5.0) -> Tuple[np.ndarray, np.ndarray]:
    if not series:
        return np.array([]), np.array([])
    n = max(1, int((end - start).total_seconds() / dt_s) + 1)
    t = np.array([(start + timedelta(seconds=i * dt_s)).timestamp() for i in range(n)])
    ts = np.array([s[0].timestamp() for s in series])
    vals = np.array([s[1] if s[1] is not None else np.nan for s in series], dtype=float)
    # forward-fill via searchsorted
    idx = np.clip(np.searchsorted(ts, t, side="right") - 1, 0, len(ts) - 1)
    y = vals[idx]
    # if first sample is after start, mask leading NaNs
    y = np.where(t < ts[0], np.nan, y)
    return t, y


def fetch_window(ha_url: str, token: str, w: TestWindow, pad_s: int = 60) -> Dict[str, Tuple[np.ndarray, np.ndarray]]:
    out = {}
    s = w.start - timedelta(seconds=pad_s)
    e = w.end + timedelta(seconds=pad_s)
    for eid in ENTITIES:
        try:
            series = ha_history(ha_url, token, eid, s, e)
        except Exception as ex:
            print(f"  ! history fetch failed for {eid}: {ex}", file=sys.stderr)
            series = []
        out[eid] = resample(series, s, e, dt_s=5.0)
    return out


# ---- model fits ---------------------------------------------------------

def first_order_step(t, K, tau, td, y0):
    """y0 + K * (1 - exp(-(t-td)/tau)) for t>=td else y0."""
    out = np.full_like(t, y0, dtype=float)
    mask = t >= td
    out[mask] = y0 + K * (1.0 - np.exp(-(t[mask] - td) / max(tau, 1e-3)))
    return out


def fit_first_order_step(t: np.ndarray, y: np.ndarray, t_step: float) -> dict:
    """Fit a first-order-plus-dead-time response to a step that occurs at t_step."""
    mask = ~np.isnan(y)
    t = t[mask]
    y = y[mask]
    if len(t) < 8:
        return {"error": "not enough samples"}
    t_rel = t - t_step
    y0 = float(np.mean(y[t_rel <= 0])) if np.any(t_rel <= 0) else float(y[0])
    y_inf = float(np.median(y[t_rel >= (t_rel.max() - 60)])) if t_rel.max() > 60 else float(y[-1])
    K0 = y_inf - y0
    if not HAVE_SCIPY or abs(K0) < 1e-6:
        # fall back: 63% rise time estimate
        target = y0 + 0.632 * K0
        idx = np.argmin(np.abs(y - target))
        tau = max(t_rel[idx], 1.0) if K0 != 0 else float("nan")
        return {
            "K": K0,
            "tau_s": tau,
            "td_s": 0.0,
            "y0": y0,
            "y_inf": y_inf,
            "method": "63% rise (no scipy)",
        }
    try:
        popt, pcov = curve_fit(
            first_order_step, t_rel, y,
            p0=[K0, 60.0, 5.0, y0],
            bounds=([min(K0, -200) - 1, 1.0, 0.0, min(y) - 5],
                    [max(K0, 200) + 1, 1800.0, 600.0, max(y) + 5]),
            maxfev=4000,
        )
        perr = np.sqrt(np.diag(pcov))
        return {
            "K": float(popt[0]),
            "K_ci95": float(1.96 * perr[0]),
            "tau_s": float(popt[1]),
            "tau_ci95": float(1.96 * perr[1]),
            "td_s": float(popt[2]),
            "td_ci95": float(1.96 * perr[2]),
            "y0": float(popt[3]),
            "y_inf": float(popt[3] + popt[0]),
            "method": "scipy curve_fit",
        }
    except Exception as ex:
        return {"error": str(ex), "K_init": K0}


# ---- per-test analyses --------------------------------------------------

E_BEDTIME = "number.smart_topper_right_side_bedtime_temperature"
E_SETPOINT = "sensor.smart_topper_right_side_temperature_setpoint"
E_BLOWER = "sensor.smart_topper_right_side_blower_output"
E_AMB = "sensor.smart_topper_right_side_ambient_temperature"
E_BL = "sensor.smart_topper_right_side_body_sensor_left"
E_BC = "sensor.smart_topper_right_side_body_sensor_center"
E_BR = "sensor.smart_topper_right_side_body_sensor_right"


def analyze_test1(w: TestWindow, data) -> dict:
    t, blower = data[E_BLOWER]
    _, setp = data[E_SETPOINT]
    # Find the t_step where bedtime went to -10 from the events
    step_event = next((e for e in w.events if e["action"] == "set_bedtime" and e["value"] == "-10"), None)
    decay_event = next(
        (e for e in w.events if e["action"] == "set_bedtime" and e["value"] == "0" and e["ts"] > step_event["ts"]),
        None) if step_event else None
    out = {"step_event_ts": str(step_event["ts"]) if step_event else None}
    if step_event is None:
        return out
    t_step = step_event["ts"].timestamp()
    fit_up = fit_first_order_step(t, blower, t_step)
    out["blower_step_response"] = fit_up
    fit_sp = fit_first_order_step(t, setp, t_step)
    out["setpoint_response"] = fit_sp
    if decay_event:
        t_dec = decay_event["ts"].timestamp()
        # crop to decay window
        mask = (t >= t_dec - 30) & (t <= t_dec + 600)
        out["blower_decay"] = fit_first_order_step(t[mask], blower[mask], t_dec)
    # steady-state blower at setting=-10
    if decay_event:
        ss_mask = (t > t_step + 600) & (t < decay_event["ts"].timestamp() - 30)
        if ss_mask.any():
            out["blower_steady_at_-10"] = float(np.nanmean(blower[ss_mask]))
    return out


def analyze_test2(w: TestWindow, data) -> dict:
    t, blower = data[E_BLOWER]
    body_map = {"left": E_BL, "center": E_BC, "right": E_BR}
    results = {}
    for label, eid in body_map.items():
        on_evt = next((e for e in w.events if e["action"] == f"icepack_on_{label}"), None)
        off_evt = next((e for e in w.events if e["action"] == f"icepack_off_{label}"), None)
        if not (on_evt and off_evt):
            continue
        t_on = on_evt["ts"].timestamp()
        t_off = off_evt["ts"].timestamp()
        baseline_mask = (t >= t_on - 90) & (t < t_on)
        peak_mask = (t >= t_on + 30) & (t <= t_off + 30)
        _, body = data[eid]
        # how much did this sensor drop?
        body_baseline = float(np.nanmean(body[baseline_mask])) if baseline_mask.any() else np.nan
        body_min = float(np.nanmin(body[peak_mask])) if peak_mask.any() else np.nan
        body_drop = body_baseline - body_min
        # how much did blower drop?
        blower_baseline = float(np.nanmean(blower[baseline_mask])) if baseline_mask.any() else np.nan
        blower_min = float(np.nanmin(blower[peak_mask])) if peak_mask.any() else np.nan
        blower_drop = blower_baseline - blower_min
        sensitivity = (blower_drop / body_drop) if body_drop > 0.5 else float("nan")
        results[label] = {
            "body_baseline_F": body_baseline,
            "body_min_F": body_min,
            "body_drop_F": body_drop,
            "blower_baseline_pct": blower_baseline,
            "blower_min_pct": blower_min,
            "blower_drop_pct": blower_drop,
            "d_blower_per_dF": sensitivity,
        }
    # all-three
    on_all = next((e for e in w.events if e["action"] == "icepack_on_all"), None)
    off_all = next((e for e in w.events if e["action"] == "icepack_off_all"), None)
    if on_all and off_all:
        t_on = on_all["ts"].timestamp()
        t_off = off_all["ts"].timestamp()
        baseline_mask = (t >= t_on - 90) & (t < t_on)
        peak_mask = (t >= t_on + 30) & (t <= t_off + 30)
        results["all"] = {
            "blower_baseline_pct": float(np.nanmean(blower[baseline_mask])) if baseline_mask.any() else np.nan,
            "blower_min_pct": float(np.nanmin(blower[peak_mask])) if peak_mask.any() else np.nan,
        }
    # rough symmetry / weighting check
    sens = {k: v["d_blower_per_dF"] for k, v in results.items() if k in ("left", "center", "right")}
    valid = {k: v for k, v in sens.items() if not (v != v)}  # filter NaN
    if len(valid) >= 2:
        total = sum(valid.values())
        if total != 0:
            results["normalized_weights"] = {k: v / total for k, v in valid.items()}
    return results


def analyze_test3(w: TestWindow, data) -> dict:
    t, blower = data[E_BLOWER]
    on_evt = next((e for e in w.events if e["action"] == "bedjet_heat_on"), None)
    off_evt = next((e for e in w.events if e["action"] == "bedjet_off"), None)
    out: dict = {}
    if not (on_evt and off_evt):
        return out
    t_on = on_evt["ts"].timestamp()
    t_off = off_evt["ts"].timestamp()
    baseline_mask = (t >= t_on - 120) & (t < t_on)
    peak_mask = (t >= t_on + 60) & (t <= t_off)
    out["blower_baseline_pct"] = float(np.nanmean(blower[baseline_mask])) if baseline_mask.any() else np.nan
    out["blower_peak_pct"] = float(np.nanmax(blower[peak_mask])) if peak_mask.any() else np.nan
    out["blower_delta_pct"] = out["blower_peak_pct"] - out["blower_baseline_pct"]
    # body sensor max delta (any sensor)
    bodies = {}
    for label, eid in (("left", E_BL), ("center", E_BC), ("right", E_BR)):
        _, b = data[eid]
        base = float(np.nanmean(b[baseline_mask])) if baseline_mask.any() else np.nan
        peak = float(np.nanmax(b[peak_mask])) if peak_mask.any() else np.nan
        bodies[label] = {"baseline_F": base, "peak_F": peak, "delta_F": peak - base}
    out["body_sensors"] = bodies
    avg_body_delta = np.nanmean([v["delta_F"] for v in bodies.values()])
    if avg_body_delta and avg_body_delta > 0.5:
        out["d_blower_per_dF_heat"] = out["blower_delta_pct"] / avg_body_delta
    # fit time constant of the blower rise
    out["blower_rise_fit"] = fit_first_order_step(t, blower, t_on)
    return out


def analyze_test4(w: TestWindow, data) -> dict:
    t, blower = data[E_BLOWER]
    _, setp = data[E_SETPOINT]
    points = []
    set_events = [e for e in w.events if e["action"] == "set_bedtime"]
    for i, e in enumerate(set_events):
        t0 = e["ts"].timestamp()
        # use last 30 s before the next event (steady-ish) for steady-state estimate
        t1 = set_events[i + 1]["ts"].timestamp() if i + 1 < len(set_events) else t0 + 90
        ss_mask = (t >= t1 - 30) & (t < t1)
        if not ss_mask.any():
            continue
        points.append({
            "setting": float(e["value"]),
            "ss_blower_pct": float(np.nanmean(blower[ss_mask])),
            "ss_setpoint_F": float(np.nanmean(setp[ss_mask])),
        })
    out: dict = {"points": points}
    if len(points) >= 3:
        x = np.array([p["setting"] for p in points])
        y_b = np.array([p["ss_blower_pct"] for p in points])
        y_s = np.array([p["ss_setpoint_F"] for p in points])
        # linear fit blower vs setting
        A = np.vstack([x, np.ones_like(x)]).T
        slope_b, icpt_b = np.linalg.lstsq(A, y_b, rcond=None)[0]
        slope_s, icpt_s = np.linalg.lstsq(A, y_s, rcond=None)[0]
        out["blower_vs_setting"] = {"slope_pct_per_unit": float(slope_b), "intercept_pct": float(icpt_b)}
        out["setpoint_vs_setting"] = {"slope_F_per_unit": float(slope_s), "intercept_F": float(icpt_s)}
    return out


# ---- main --------------------------------------------------------------

def write_report(windows, fits, out_path: str) -> None:
    lines = ["# RC experiment analysis", ""]
    for w in windows:
        lines.append(f"## {w.name}")
        lines.append(f"- start: `{w.start.isoformat()}`")
        lines.append(f"- end:   `{w.end.isoformat()}`")
        lines.append(f"- duration: {(w.end - w.start).total_seconds()/60:.1f} min")
        lines.append("")
        lines.append("```json")
        lines.append(json.dumps(fits.get(w.name, {}), indent=2, default=str))
        lines.append("```")
        lines.append("")
    with open(out_path, "w") as f:
        f.write("\n".join(lines))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ha-url", default=os.environ.get("HA_URL", "http://192.168.0.106:8123"))
    ap.add_argument("--token", default=os.environ.get("HA_TOKEN"))
    ap.add_argument("--csv", required=True, help="Path to rc_experiment_log.csv")
    ap.add_argument("--out", default="rc_experiment_report.md")
    args = ap.parse_args()

    if not args.token:
        sys.stderr.write("Set HA_TOKEN env var (or --token).\n")
        return 2

    windows = load_csv(args.csv)
    if not windows:
        sys.stderr.write("No test windows found in CSV.\n")
        return 2

    fits = {}
    for w in windows:
        print(f"[{w.name}] {w.start.isoformat()} -> {w.end.isoformat()} "
              f"({(w.end - w.start).total_seconds()/60:.1f} min, {len(w.events)} events)")
        data = fetch_window(args.ha_url, args.token, w)
        if w.name == "test1_step":
            fits[w.name] = analyze_test1(w, data)
        elif w.name == "test2_icepack":
            fits[w.name] = analyze_test2(w, data)
        elif w.name == "test3_bedjet":
            fits[w.name] = analyze_test3(w, data)
        elif w.name == "test4_sweep":
            fits[w.name] = analyze_test4(w, data)
        else:
            fits[w.name] = {"note": "no analyzer"}
        print(json.dumps(fits[w.name], indent=2, default=str))

    write_report(windows, fits, args.out)
    print(f"\nReport written to {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
