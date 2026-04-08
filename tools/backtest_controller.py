"""
Backtest the PID controller against REAL overnight sensor data from InfluxDB.

Pulls actual body temperature, ambient, and setting data from the topper
and replays the controller logic to see what settings it would have chosen.

Usage:
    cd PerfectlySnug
    python3 tools/backtest_controller.py                     # all recent nights
    python3 tools/backtest_controller.py --date 2026-03-08   # specific night
    python3 tools/backtest_controller.py --max-setting -3    # test with tighter clamp
"""

import argparse
import json
import logging
import sys
from datetime import datetime, timedelta
from pathlib import Path
from urllib.request import Request, urlopen

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("backtest")

INFLUX_URL = "http://192.168.0.106:8086"
INFLUX_DB = "perfectly_snug"

# Import controller trajectory
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from ml.controller import (
    TargetTrajectory, make_warm_sleeper_trajectory,
    MIN_SETTING, MAX_SETTING, SETTING_OFFSET, MAX_STEP_PER_LOOP,
)


def influx_query(query: str) -> list[dict]:
    """Run an InfluxDB query and return results."""
    from urllib.parse import urlencode
    url = f"{INFLUX_URL}/query?{urlencode({'db': INFLUX_DB, 'q': query})}"
    req = Request(url)
    with urlopen(req, timeout=15) as resp:
        data = json.loads(resp.read())
    results = data.get("results", [{}])[0]
    series = results.get("series", [])
    if not series:
        return []
    cols = series[0]["columns"]
    return [dict(zip(cols, row)) for row in series[0]["values"]]


def pull_overnight_data(date_str: str, zone: str = "left") -> list[dict]:
    """
    Pull one night of sensor data from InfluxDB.
    Night window: date 9PM to date+1 11AM (UTC-adjusted).
    """
    # Assume EST (UTC-5) — 9PM EST = 2AM UTC next day, 11AM EST = 4PM UTC
    d = datetime.strptime(date_str, "%Y-%m-%d")
    start_utc = d + timedelta(hours=21 + 5)  # 9PM EST = 2AM UTC next day
    end_utc = d + timedelta(days=1, hours=11 + 5)  # 11AM EST next day = 4PM UTC

    start_str = start_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
    end_str = end_utc.strftime("%Y-%m-%dT%H:%M:%SZ")

    entity_prefix = f"smart_topper_{zone}_side"

    # Pull body sensor center, ambient, and the bedtime setting
    q = f"""
    SELECT mean(value) FROM "°F"
    WHERE entity_id = '{entity_prefix}_body_sensor_center'
      AND time >= '{start_str}' AND time <= '{end_str}'
    GROUP BY time(5m) fill(previous)
    """
    body_data = influx_query(q)

    # Use dehumidifier sensor for real room temp instead of topper's onboard
    # ambient sensor which reads 5-10°F too high due to radiated body heat
    q_ambient = f"""
    SELECT mean(value) FROM "°F"
    WHERE entity_id = 'superior_6000s_temperature'
      AND time >= '{start_str}' AND time <= '{end_str}'
    GROUP BY time(5m) fill(previous)
    """
    ambient_data = influx_query(q_ambient)

    # Also get the actual setting that was used
    q_setting = f"""
    SELECT mean(value) FROM "number.{entity_prefix}_bedtime_temperature"
    WHERE time >= '{start_str}' AND time <= '{end_str}'
    GROUP BY time(5m) fill(previous)
    """
    setting_data = influx_query(q_setting)

    # Also get run_progress to detect actual bedtime
    q_progress = f"""
    SELECT mean(value) FROM "sensor.{entity_prefix}_run_progress"
    WHERE time >= '{start_str}' AND time <= '{end_str}'
    GROUP BY time(5m) fill(previous)
    """
    progress_data = influx_query(q_progress)

    # Merge by timestamp
    merged = {}
    for row in body_data:
        ts = row["time"]
        if row["mean"] is not None:
            merged.setdefault(ts, {})["body_f"] = row["mean"]

    for row in ambient_data:
        ts = row["time"]
        if row["mean"] is not None:
            merged.setdefault(ts, {})["ambient_f"] = row["mean"]

    for row in setting_data:
        ts = row["time"]
        if row["mean"] is not None:
            merged.setdefault(ts, {})["actual_setting"] = row["mean"]

    for row in progress_data:
        ts = row["time"]
        if row["mean"] is not None:
            merged.setdefault(ts, {})["run_progress"] = row["mean"]

    # Convert to sorted list
    records = []
    for ts in sorted(merged.keys()):
        r = merged[ts]
        if "body_f" in r:
            r["time"] = ts
            records.append(r)

    return records


def backtest_night(records: list[dict], max_display_setting: int = 0) -> dict:
    """
    Replay the PID controller against real sensor data.

    Args:
        records: List of {time, body_f, ambient_f, actual_setting, run_progress}
        max_display_setting: Maximum display setting allowed (0 = no heating)

    Returns:
        Summary dict with all controller decisions
    """
    if not records:
        return {"error": "No data"}

    trajectory = make_warm_sleeper_trajectory()
    max_raw = max_display_setting + SETTING_OFFSET  # 0 + 10 = raw 10

    # Find bedtime (first record where run_progress > 0)
    bedtime_idx = 0
    for i, r in enumerate(records):
        if r.get("run_progress", 0) and r["run_progress"] > 0:
            bedtime_idx = i
            break

    # State
    current_setting = 1  # Start at raw 1 = display -9
    integral_error = 0.0
    last_body_temp = None
    adjustments = []
    all_points = []

    for i, r in enumerate(records[bedtime_idx:]):
        minutes = i * 5  # 5-min intervals
        body_f = r["body_f"]
        ambient_f = r.get("ambient_f", 72.0)
        actual_setting_display = r.get("actual_setting", None)
        target_f = trajectory.target_at(minutes)

        # PID control
        pid_output = 0.0
        if last_body_temp is not None:
            error = body_f - target_f
            integral_error += error * 0.02
            integral_error = max(-5.0, min(5.0, integral_error))
            derivative = body_f - last_body_temp

            pid_output = -(0.5 * error + integral_error + 0.1 * derivative)
            pid_output = max(-MAX_STEP_PER_LOOP, min(MAX_STEP_PER_LOOP, pid_output))

            new_setting = current_setting + round(pid_output)
            new_setting = max(MIN_SETTING, min(max_raw, new_setting))

            if new_setting != current_setting:
                adjustments.append({
                    "min": minutes,
                    "time": r["time"],
                    "body_f": round(body_f, 1),
                    "target_f": round(target_f, 1),
                    "old": current_setting - SETTING_OFFSET,
                    "new": new_setting - SETTING_OFFSET,
                    "pid": round(pid_output, 2),
                    "actual": actual_setting_display,
                })
                current_setting = new_setting

        last_body_temp = body_f

        all_points.append({
            "min": minutes,
            "body_f": round(body_f, 1),
            "target_f": round(target_f, 1),
            "controller_setting": current_setting - SETTING_OFFSET,
            "actual_setting": actual_setting_display,
            "pid": round(pid_output, 2),
        })

    # Summary stats
    controller_settings = [p["controller_setting"] for p in all_points]
    actual_settings = [p["actual_setting"] for p in all_points if p["actual_setting"] is not None]
    body_temps = [p["body_f"] for p in all_points]

    return {
        "duration_hours": len(records[bedtime_idx:]) * 5 / 60,
        "data_points": len(all_points),
        "adjustments": len(adjustments),
        "controller_range": [min(controller_settings), max(controller_settings)],
        "actual_range": [min(actual_settings), max(actual_settings)] if actual_settings else None,
        "body_temp_range": [min(body_temps), max(body_temps)],
        "body_temp_avg": round(sum(body_temps) / len(body_temps), 1),
        "adjustment_log": adjustments,
        "all_points": all_points,
    }


def main():
    parser = argparse.ArgumentParser(description="Backtest controller against real sensor data")
    parser.add_argument("--date", help="Specific date (YYYY-MM-DD), or 'all' for recent nights")
    parser.add_argument("--zone", default="left", choices=["left", "right"])
    parser.add_argument("--max-setting", type=int, default=0,
                        help="Max display setting (default: 0, try -3 for cooling-only)")
    args = parser.parse_args()

    # Determine dates
    if args.date and args.date != "all":
        dates = [args.date]
    else:
        # Last 5 nights
        today = datetime.now()
        dates = [(today - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(1, 6)]

    log.info(f"Backtesting with max display setting = {args.max_setting}")
    log.info(f"Zone: {args.zone}")
    log.info("")

    all_results = {}

    for date in dates:
        log.info(f"{'=' * 60}")
        log.info(f"NIGHT: {date} → {date} (next day)")
        log.info(f"{'=' * 60}")

        records = pull_overnight_data(date, args.zone)
        if not records:
            log.warning(f"  No data for {date}")
            continue

        log.info(f"  Pulled {len(records)} data points ({len(records)*5/60:.1f} hours)")

        result = backtest_night(records, max_display_setting=args.max_setting)
        all_results[date] = result

        if "error" in result:
            log.warning(f"  {result['error']}")
            continue

        log.info(f"  Duration:           {result['duration_hours']:.1f} hours")
        log.info(f"  Body temp range:    {result['body_temp_range'][0]:.1f}–{result['body_temp_range'][1]:.1f}°F (avg {result['body_temp_avg']}°F)")
        log.info(f"  Actual setting:     {result['actual_range']}" if result['actual_range'] else "  Actual setting:     N/A")
        log.info(f"  Controller range:   {result['controller_range'][0]:+d} to {result['controller_range'][1]:+d}")
        log.info(f"  Adjustments:        {result['adjustments']}")

        # Show key moments
        log.info("")
        log.info(f"  {'Min':>5} | {'BodyF':>6} | {'Target':>6} | {'Ctrl':>5} | {'Actual':>6} | {'PID':>6}")
        for p in result["all_points"][::6]:  # Every 30 min
            actual = f"{p['actual_setting']:+.0f}" if p['actual_setting'] is not None else "  —"
            log.info(f"  {p['min']:5d} | {p['body_f']:6.1f} | {p['target_f']:6.1f} | "
                     f"{p['controller_setting']:>+4d}  | {actual:>5}  | {p['pid']:+6.2f}")

        if result["adjustment_log"]:
            log.info("")
            log.info("  Setting Changes:")
            for a in result["adjustment_log"]:
                actual = f"(actual was {a['actual']:+.0f})" if a['actual'] is not None else ""
                log.info(f"    {a['min']:4d}min | body={a['body_f']:.1f}°F target={a['target_f']:.1f}°F | "
                         f"{a['old']:+d} → {a['new']:+d} {actual}")

        log.info("")

    # Cross-night summary
    if len(all_results) > 1:
        log.info("=" * 60)
        log.info("CROSS-NIGHT SUMMARY")
        log.info("=" * 60)
        for date, r in all_results.items():
            if "error" in r:
                continue
            log.info(f"  {date}: body {r['body_temp_range'][0]:.0f}–{r['body_temp_range'][1]:.0f}°F | "
                     f"ctrl {r['controller_range'][0]:+d} to {r['controller_range'][1]:+d} | "
                     f"actual {r['actual_range']} | "
                     f"{r['adjustments']} changes")

    # Save full results
    output = Path(__file__).parent / "backtest_results.json"
    with open(output, "w") as f:
        json.dump(all_results, f, indent=2, default=str)
    log.info(f"\nFull results saved to {output}")


if __name__ == "__main__":
    main()
