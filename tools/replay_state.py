#!/usr/bin/env python3
"""PerfectlySnug — Offline replay of the latent state estimator.

Reconstructs Features per controller_readings tick from PG history, runs
ml/v6/state_estimator.estimate_state, and scores against the three validation
buckets in docs/proposals/2026-05-04_state_estimation.md §8.2:

    1. Override correlation: in the 5-min window BEFORE each user override,
       the inferred state should NOT be STABLE_SLEEP/conf>0.8.
       Target: ≥70% of overrides have state ∈ {AWAKE_IN_BED, SETTLING,
       RESTLESS, WAKE_TRANSITION} in that lead window.

    2. Empty-bed false-positive rate: when presence=False ≥10min, state
       must equal OFF_BED. Target: ≥99%.

    3. Stability mass: across all nights, share of OCCUPIED MID-NIGHT
       (90 min < secs_since_presence_change < 5h) ticks labeled STABLE_SLEEP
       should be 30–80%. Outside that band invalidates the design.

Plus reachability: every state must be reached at least once.

Usage:
    python tools/replay_state.py                              # last 14 nights, both zones
    python tools/replay_state.py --night 2026-05-02 --zone left
    python tools/replay_state.py --from 2026-04-20 --to 2026-05-03 --json out.json

The estimator is pure; this script touches PG read-only and never writes.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter, defaultdict
from dataclasses import asdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Optional

import psycopg2
import psycopg2.extras

# Make `ml.v6.state_estimator` importable when invoked from anywhere.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ml.v6.state_estimator import (  # noqa: E402
    Features,
    LatentState,
    Percentiles,
    STATE_AWAKE_IN_BED,
    STATE_NAMES,
    STATE_OFF_BED,
    STATE_RESTLESS,
    STATE_SETTLING,
    STATE_STABLE_SLEEP,
    STATE_WAKE_TRANSITION,
    estimate_state,
)

# ── PG connection (mirror tools/eval_nightly.py) ──────────────────────
PG_HOST = os.environ.get("PG_HOST", "192.168.0.3")
PG_PORT = int(os.environ.get("PG_PORT", "5432"))
PG_DB = os.environ.get("PG_DB", "sleepdata")
PG_USER = os.environ.get("PG_USER", "sleepsync")
PG_PASS = os.environ.get("PG_PASS", "sleepsync_local")

OVERRIDE_LEAD_S = 5 * 60        # 5-minute window (spec §8.2 bucket 1)
OFF_BED_DEBOUNCE_S = 10 * 60    # 10-minute window (spec §8.2 bucket 2)
MID_NIGHT_MIN_S = 90 * 60       # spec §8.2 bucket 3 lower bound
MID_NIGHT_MAX_S = 5 * 3600      # spec §8.2 bucket 3 upper bound

NON_STABLE_VALID_STATES = {STATE_AWAKE_IN_BED, STATE_SETTLING,
                           STATE_RESTLESS, STATE_WAKE_TRANSITION,
                           # Degraded-mode equivalents (spec §6.1 mapping):
                           # OCCUPIED_AWAKE → AWAKE_IN_BED control behavior.
                           "OCCUPIED_AWAKE"}
# Spec §6.1: OCCUPIED_QUIET → STABLE_SLEEP control behavior. For bucket 3
# we count both as "stable" so the metric is meaningful in degraded mode.
STABLE_EQUIVALENT_STATES = {STATE_STABLE_SLEEP, "OCCUPIED_QUIET"}


def get_conn():
    return psycopg2.connect(
        host=PG_HOST, port=PG_PORT, dbname=PG_DB,
        user=PG_USER, password=PG_PASS, connect_timeout=10,
    )


# ── Feature reconstruction ────────────────────────────────────────────
def _fetch_readings(conn, zone: str, start: datetime, end: datetime) -> list[dict]:
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """
            SELECT ts, action, setting, override_delta,
                   body_avg_f, body_left_f, room_temp_f, ambient_f,
                   bed_occupied_left, bed_occupied_right, controller_version
            FROM controller_readings
            WHERE zone=%s AND ts>=%s AND ts<%s
            ORDER BY ts
            """,
            (zone, start, end),
        )
        return list(cur.fetchall())


def _fetch_pressure(conn, zone: str, start: datetime, end: datetime) -> list[dict]:
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """
            SELECT ts, abs_delta_sum_60s, max_delta_60s, sample_count, occupied
            FROM controller_pressure_movement
            WHERE zone=%s AND ts>=%s AND ts<%s
            ORDER BY ts
            """,
            (zone, start, end),
        )
        return list(cur.fetchall())


def _occupied(row: dict, zone: str) -> Optional[bool]:
    key = "bed_occupied_left" if zone == "left" else "bed_occupied_right"
    val = row.get(key)
    if val is None:
        return None
    return bool(val)


def _build_features(
    row: dict,
    zone: str,
    pressure_window: list[dict],
    body_window: list[dict],
    seconds_since_presence_change: Optional[float],
) -> Features:
    """Build a Features snapshot for one controller_readings tick.

    pressure_window: rows in trailing 15min, oldest→newest.
    body_window:    controller_readings rows in trailing 15min, oldest→newest.
    """
    # Movement features from pressure aggregates.  abs_delta_sum_60s is the
    # per-minute |Δp| total — use it as a proxy for the per-second RMS the
    # live controller would compute. Zero/None windows ⇒ unavailable.
    last_5 = [p for p in pressure_window
              if p["ts"] >= row["ts"] - timedelta(seconds=5 * 60)]
    last_15 = pressure_window
    rms5 = _rms([p["abs_delta_sum_60s"] for p in last_5
                 if p.get("abs_delta_sum_60s") is not None])
    rms15 = _rms([p["abs_delta_sum_60s"] for p in last_15
                  if p.get("abs_delta_sum_60s") is not None])
    var15 = _variance([p["abs_delta_sum_60s"] for p in last_15
                       if p.get("abs_delta_sum_60s") is not None])
    max60 = None
    last_1 = [p for p in pressure_window
              if p["ts"] >= row["ts"] - timedelta(seconds=60)]
    if last_1:
        vals = [p["max_delta_60s"] for p in last_1 if p.get("max_delta_60s") is not None]
        if vals:
            max60 = max(vals)

    # Use body_left_f (skin-contact) for trend and validity per
    # right_comfort_proxy convention and the existing body_fb path. body_avg_f
    # is dragged down by non-contact center/right sensors in our 3-sensor mean,
    # which spuriously fails the validity gate.
    body_for_trend = "body_left_f"
    body_trend = _ols_slope_per_15m([(b["ts"], b[body_for_trend])
                                     for b in body_window
                                     if b.get(body_for_trend) is not None])

    return Features(
        movement_rms_5min=rms5,
        movement_rms_15min=rms15,
        movement_variance_15min=var15,
        movement_max_delta_60s=max60,
        presence_binary=_occupied(row, zone),
        seconds_since_presence_change=seconds_since_presence_change,
        body_avg_f=row.get(body_for_trend),
        body_trend_15min=body_trend,
        room_temp_f=row.get("room_temp_f"),
        setting_recent_change_30min=0,  # not used by current rule cascade
    )


def _rms(values: list[float]) -> Optional[float]:
    vals = [v for v in values if v is not None]
    if not vals:
        return None
    sq = sum(v * v for v in vals) / len(vals)
    return sq ** 0.5


def _variance(values: list[float]) -> Optional[float]:
    vals = [v for v in values if v is not None]
    if len(vals) < 2:
        return None
    mean = sum(vals) / len(vals)
    return sum((v - mean) ** 2 for v in vals) / (len(vals) - 1)


def _ols_slope_per_15m(samples: list[tuple[datetime, float]]) -> Optional[float]:
    """OLS slope in °F per 15 min. Returns None if <3 samples (replay cadence
    is ~5min so 15-min window yields 3-4 samples; live 60s tick cadence yields
    ~15 — the live wrapper enforces the spec §2.3 ≥5 minimum)."""
    valid = [(t, v) for t, v in samples if v is not None]
    if len(valid) < 3:
        return None
    t0 = valid[0][0]
    xs = [(t - t0).total_seconds() / 60.0 for t, _ in valid]    # minutes
    ys = [float(v) for _, v in valid]
    n = len(xs)
    mx = sum(xs) / n
    my = sum(ys) / n
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    den = sum((x - mx) ** 2 for x in xs)
    if den == 0:
        return None
    slope_per_min = num / den
    return slope_per_min * 15.0


# ── Replay one (night, zone) ──────────────────────────────────────────
def replay_night(conn, night: date, zone: str) -> dict:
    """Run the estimator over one night × zone. Returns scoring summary."""
    # Use the controller_readings range that touches this night (24h window
    # noon-to-noon ET, mirroring eval_nightly's bed-window detection in spirit).
    start = datetime(night.year, night.month, night.day,
                     16, 0, tzinfo=timezone.utc)  # noon ET ≈ 16Z
    end = start + timedelta(hours=24)

    readings = _fetch_readings(conn, zone, start, end)
    if not readings:
        return {"night": night.isoformat(), "zone": zone, "ticks": 0,
                "skipped": "no_readings"}
    pressure = _fetch_pressure(conn, zone, start, end)

    # Pre-index pressure by ts for trailing-window scans.
    p_by_ts = sorted(pressure, key=lambda r: r["ts"])

    prev_state: Optional[str] = None
    prev_presence: Optional[bool] = None
    presence_change_ts: Optional[datetime] = None

    # Each entry: dict(ts, state, confidence, presence, secs_in_bed,
    #                  is_override, is_off_bed_persistent)
    history: list[dict] = []

    for i, r in enumerate(readings):
        # Maintain seconds_since_presence_change.
        cur_presence = _occupied(r, zone)
        if cur_presence != prev_presence:
            presence_change_ts = r["ts"]
            prev_presence = cur_presence
        secs_since = (r["ts"] - presence_change_ts).total_seconds() \
            if presence_change_ts is not None else None

        # Trailing windows.
        win_start = r["ts"] - timedelta(seconds=15 * 60)
        pressure_window = [p for p in p_by_ts
                           if win_start <= p["ts"] <= r["ts"]]
        body_window = [b for b in readings[:i + 1]
                       if b["ts"] >= win_start]

        feats = _build_features(r, zone, pressure_window, body_window, secs_since)
        latent = estimate_state(feats, prev_state=prev_state)
        if latent.state != "DISTURBANCE":
            prev_state = latent.state

        # Off-bed persistent: True if presence has been False for ≥10min.
        is_off_bed_persistent = (cur_presence is False
                                 and secs_since is not None
                                 and secs_since >= OFF_BED_DEBOUNCE_S)

        history.append({
            "ts": r["ts"],
            "state": latent.state,
            "confidence": latent.confidence,
            "degraded": latent.degraded,
            "presence": cur_presence,
            "secs_since_presence_change": secs_since,
            "is_override": (r["action"] == "override"
                            and r.get("override_delta") is not None),
            "override_delta": r.get("override_delta"),
            "is_off_bed_persistent": is_off_bed_persistent,
        })

    # ── Score this night ─────────────────────────────────────────────
    state_hist = Counter(h["state"] for h in history)

    # Bucket 1: override correlation
    override_events = [(i, h) for i, h in enumerate(history) if h["is_override"]]
    overrides_well_anticipated = 0
    for i, h in override_events:
        # Look at the 5 minutes BEFORE this override.
        cutoff = h["ts"] - timedelta(seconds=OVERRIDE_LEAD_S)
        window = [history[j] for j in range(i)
                  if history[j]["ts"] >= cutoff]
        if not window:
            continue
        # If ANY tick in the window shows a non-stable in-bed state, it counts.
        if any(w["state"] in NON_STABLE_VALID_STATES for w in window):
            overrides_well_anticipated += 1

    # Bucket 2: empty-bed FP rate (persistent off-bed)
    persistent_off = [h for h in history if h["is_off_bed_persistent"]]
    persistent_off_correct = sum(1 for h in persistent_off
                                 if h["state"] == STATE_OFF_BED)

    # Bucket 3: stability mass over occupied mid-night ticks
    mid_night = [h for h in history
                 if h["presence"] is True
                 and h["secs_since_presence_change"] is not None
                 and MID_NIGHT_MIN_S < h["secs_since_presence_change"] < MID_NIGHT_MAX_S]
    mid_night_stable = sum(1 for h in mid_night
                           if h["state"] in STABLE_EQUIVALENT_STATES)

    # Track degraded-mode coverage so the reader knows when movement was missing.
    degraded_ticks = sum(1 for h in history if h["degraded"] is not None)

    return {
        "night": night.isoformat(),
        "zone": zone,
        "ticks": len(history),
        "degraded_ticks": degraded_ticks,
        "state_histogram": dict(state_hist),
        "overrides": len(override_events),
        "overrides_well_anticipated": overrides_well_anticipated,
        "persistent_off_bed_ticks": len(persistent_off),
        "persistent_off_bed_correct": persistent_off_correct,
        "mid_night_ticks": len(mid_night),
        "mid_night_stable_ticks": mid_night_stable,
    }


# ── Aggregate scoring across nights ───────────────────────────────────
def score_summaries(summaries: list[dict], min_movement_share: float = 0.0) -> dict:
    state_hist: Counter = Counter()
    overrides = overrides_anticipated = 0
    pers_off_total = pers_off_correct = 0
    mid_night_total = mid_night_stable = 0
    nights = 0
    total_ticks = total_degraded = 0
    skipped_low_movement = 0

    for s in summaries:
        if s.get("skipped"):
            continue
        # Filter: require this (night, zone) to have at least
        # `min_movement_share` of ticks with movement data (i.e. NOT degraded).
        ticks = s.get("ticks", 0)
        deg = s.get("degraded_ticks", 0)
        if ticks > 0:
            movement_share = (ticks - deg) / ticks
            if movement_share < min_movement_share:
                skipped_low_movement += 1
                continue

        nights += 1
        for st, n in s.get("state_histogram", {}).items():
            state_hist[st] += n
        overrides += s.get("overrides", 0)
        overrides_anticipated += s.get("overrides_well_anticipated", 0)
        pers_off_total += s.get("persistent_off_bed_ticks", 0)
        pers_off_correct += s.get("persistent_off_bed_correct", 0)
        mid_night_total += s.get("mid_night_ticks", 0)
        mid_night_stable += s.get("mid_night_stable_ticks", 0)
        total_ticks += s.get("ticks", 0)
        total_degraded += s.get("degraded_ticks", 0)

    bucket1 = (overrides_anticipated / overrides) if overrides else None
    bucket2 = (pers_off_correct / pers_off_total) if pers_off_total else None
    bucket3 = (mid_night_stable / mid_night_total) if mid_night_total else None
    degraded_share = (total_degraded / total_ticks) if total_ticks else None

    reachable = {st: state_hist.get(st, 0) for st in STATE_NAMES}
    unreached = [st for st, n in reachable.items() if n == 0]

    return {
        "summaries_count": nights,
        "summaries_skipped_low_movement": skipped_low_movement,
        "state_histogram": dict(state_hist),
        "unreached_states": unreached,
        "movement_degraded_share": degraded_share,
        "bucket1_override_lead": {
            "overrides": overrides,
            "well_anticipated": overrides_anticipated,
            "coverage": bucket1,
            "target": 0.70,
            "pass": bucket1 is None or bucket1 >= 0.70,
        },
        "bucket2_off_bed_fp": {
            "persistent_off_bed_ticks": pers_off_total,
            "labeled_off_bed": pers_off_correct,
            "rate": bucket2,
            "target": 0.99,
            "pass": bucket2 is None or bucket2 >= 0.99,
        },
        "bucket3_stability_mass": {
            "mid_night_ticks": mid_night_total,
            "stable_sleep_ticks": mid_night_stable,
            "share": bucket3,
            "target_band": [0.30, 0.80],
            "pass": bucket3 is None or 0.30 <= bucket3 <= 0.80,
        },
    }


# ── Driver ────────────────────────────────────────────────────────────
def parse_date(s: str) -> date:
    return datetime.strptime(s, "%Y-%m-%d").date()


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--night", type=parse_date,
                   help="Single night YYYY-MM-DD (overrides --from/--to).")
    p.add_argument("--from", dest="d_from", type=parse_date,
                   help="Inclusive start date.")
    p.add_argument("--to", dest="d_to", type=parse_date,
                   help="Inclusive end date.")
    p.add_argument("--zone", choices=("left", "right", "both"), default="both")
    p.add_argument("--last", type=int, default=14,
                   help="Default range: last N nights ending yesterday.")
    p.add_argument("--json", help="Write per-night summaries + aggregate to file.")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()

    if args.night:
        nights = [args.night]
    elif args.d_from and args.d_to:
        nights = []
        d = args.d_from
        while d <= args.d_to:
            nights.append(d)
            d += timedelta(days=1)
    else:
        # Last N nights ending yesterday.
        end = date.today() - timedelta(days=1)
        nights = [end - timedelta(days=i) for i in range(args.last - 1, -1, -1)]

    zones = ("left", "right") if args.zone == "both" else (args.zone,)

    summaries = []
    with get_conn() as conn:
        for n in nights:
            for z in zones:
                s = replay_night(conn, n, z)
                summaries.append(s)
                if args.verbose or "skipped" not in s:
                    hist = s.get("state_histogram", {})
                    top3 = sorted(hist.items(), key=lambda kv: -kv[1])[:3]
                    top3_str = " ".join(f"{k}={v}" for k, v in top3)
                    print(f"  {n} {z}: ticks={s.get('ticks', 0):4d} "
                          f"overrides={s.get('overrides', 0)} "
                          f"top: {top3_str}")

    agg = score_summaries(summaries)
    agg_inst = score_summaries(summaries, min_movement_share=0.5)

    print()
    print("=" * 70)
    print(f"FULL CORPUS — {agg['summaries_count']} (night,zone) combos")
    print(f"State histogram: {agg['state_histogram']}")
    if agg.get("movement_degraded_share") is not None:
        print(f"Movement-degraded share: {agg['movement_degraded_share']:.1%}")
    if agg["unreached_states"]:
        print(f"Unreached states: {agg['unreached_states']}")
    print()
    for key, label in [
        ("bucket1_override_lead", "Bucket 1: Override lead-time recall"),
        ("bucket2_off_bed_fp", "Bucket 2: OFF_BED rate on persistent empty"),
        ("bucket3_stability_mass", "Bucket 3: STABLE_SLEEP mid-night share"),
    ]:
        b = agg[key]
        passed = "PASS" if b["pass"] else "FAIL"
        print(f"{passed}  {label}")
        for k, v in b.items():
            if k == "pass":
                continue
            print(f"        {k}: {v}")
        print()

    if agg_inst["summaries_count"] > 0:
        print("=" * 70)
        print(f"INSTRUMENTED SUBSET (≥50% ticks with movement data) — "
              f"{agg_inst['summaries_count']} combos "
              f"(skipped {agg_inst['summaries_skipped_low_movement']})")
        print(f"Movement-degraded share: "
              f"{agg_inst.get('movement_degraded_share', 0):.1%}")
        print()
        for key, label in [
            ("bucket1_override_lead", "Bucket 1: Override lead-time recall"),
            ("bucket3_stability_mass", "Bucket 3: STABLE_SLEEP mid-night share"),
        ]:
            b = agg_inst[key]
            passed = "PASS" if b["pass"] else "FAIL"
            print(f"{passed}  {label}")
            for k, v in b.items():
                if k == "pass":
                    continue
                print(f"        {k}: {v}")
            print()

    if args.json:
        out = {
            "summaries": [_serialize(s) for s in summaries],
            "aggregate_full": agg,
            "aggregate_instrumented": agg_inst,
        }
        Path(args.json).write_text(json.dumps(out, default=str, indent=2))
        print(f"Wrote {args.json}")

    failed = sum(1 for k in ("bucket1_override_lead", "bucket2_off_bed_fp",
                             "bucket3_stability_mass")
                 if not agg_inst[k]["pass"]
                 and agg_inst["summaries_count"] > 0)
    return 0 if failed == 0 else 2


def _serialize(d):
    return {k: (v.isoformat() if isinstance(v, datetime) else v)
            for k, v in d.items()}


if __name__ == "__main__":
    sys.exit(main())
