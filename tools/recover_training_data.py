"""
Reconstruct stage classifier training data from InfluxDB history.

The controller's amnesia bug (fixed 2026-03-20) wiped stage_training_data
on every AppDaemon restart. This script recovers ~2 weeks of historical
HR, HRV, and sleep stage data from InfluxDB and produces training samples
in the exact format the stage classifier expects.

Data sources (all logged via HA → InfluxDB since 2026-03-05):
  - input_number.apple_health_hr_avg     (bpm)
  - input_number.apple_health_hrv        (ms)
  - input_number.apple_health_respiratory_rate (breaths/min)
  - input_text.apple_health_sleep_stage  (deep/core/rem/awake/in_bed)

Output:
  PerfectlySnug/ml/state/recovered_training_data.json
  — can be merged into controller_state.json or fed directly
    to train_stage_classifier.py

Usage:
    python3 PerfectlySnug/tools/recover_training_data.py
    python3 PerfectlySnug/tools/recover_training_data.py --influx-host 192.168.0.106
"""

import argparse
import json
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

INFLUX_URL = "http://192.168.0.106:8086"
INFLUX_DB = "perfectly_snug"
OUTPUT_DIR = Path(__file__).resolve().parent.parent / "ml" / "state"

# Sleep window: 9 PM to 11 AM local (EST = UTC-5)
SLEEP_START_HOUR_LOCAL = 21  # 9 PM
SLEEP_END_HOUR_LOCAL = 11    # 11 AM next day
UTC_OFFSET = 5               # EST


def _parse_ts(ts_str):
    """Parse an InfluxDB timestamp (with or without Z suffix)."""
    return datetime.fromisoformat(ts_str.replace("Z", "+00:00"))


def influx_query(query, host=None):
    """Run an InfluxDB query and return results."""
    base = host or INFLUX_URL
    url = f"{base}/query?{urlencode({'db': INFLUX_DB, 'q': query})}"
    req = Request(url)
    with urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read())
    results = data.get("results", [{}])[0]
    series = results.get("series", [])
    if not series:
        return []
    cols = series[0]["columns"]
    return [dict(zip(cols, row)) for row in series[0]["values"]]


def pull_health_metric(entity_id, start_str, end_str, host=None, group_by="5m"):
    """Pull an input_number or input_text entity's history from InfluxDB."""
    # input_numbers are stored under their unit_of_measurement
    # But we need to try multiple measurement names since HA stores
    # input_number values under different measurements depending on config

    # Try common measurement names for input_numbers
    for measurement in ["bpm", "ms", "breaths/min", "°F", "%", ""]:
        if measurement:
            q = f"""
            SELECT mean(value) FROM "{measurement}"
            WHERE entity_id = '{entity_id}'
              AND time >= '{start_str}' AND time <= '{end_str}'
            GROUP BY time({group_by}) fill(previous)
            """
        else:
            # Try without measurement (state-based)
            q = f"""
            SELECT mean(value) FROM "state"
            WHERE entity_id = '{entity_id}'
              AND time >= '{start_str}' AND time <= '{end_str}'
            GROUP BY time({group_by}) fill(previous)
            """
        rows = influx_query(q, host)
        valid = [r for r in rows if r.get("mean") is not None]
        if valid:
            return valid
    return []


def pull_sleep_stage(start_str, end_str, host=None, group_by="5m"):
    """Pull sleep stage text from InfluxDB.
    
    input_text entities are stored as their own measurement
    with a 'state' column containing the text value.
    """
    q = f"""
    SELECT state FROM "input_text.apple_health_sleep_stage"
    WHERE time >= '{start_str}' AND time <= '{end_str}'
    ORDER BY time ASC
    """
    rows = influx_query(q, host)
    return rows


def recover_night(date_str, host=None):
    """Recover training data for one night."""
    d = datetime.strptime(date_str, "%Y-%m-%d")

    # Night window: 9 PM EST to 11 AM EST next day
    start_utc = d + timedelta(hours=SLEEP_START_HOUR_LOCAL + UTC_OFFSET)
    end_utc = d + timedelta(days=1, hours=SLEEP_END_HOUR_LOCAL + UTC_OFFSET)
    start_str = start_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
    end_str = end_utc.strftime("%Y-%m-%dT%H:%M:%SZ")

    print(f"\n{'='*60}")
    print(f"Night of {date_str} ({start_str} → {end_str})")
    print(f"{'='*60}")

    # Pull HR, HRV, respiratory rate
    hr_data = pull_health_metric("apple_health_hr_avg", start_str, end_str, host)
    hrv_data = pull_health_metric("apple_health_hrv", start_str, end_str, host)
    rr_data = pull_health_metric("apple_health_respiratory_rate", start_str, end_str, host)
    stage_data = pull_sleep_stage(start_str, end_str, host)

    print(f"  HR points:    {len(hr_data)}")
    print(f"  HRV points:   {len(hrv_data)}")
    print(f"  RR points:    {len(rr_data)}")
    print(f"  Stage points: {len(stage_data)}")

    if not hr_data or not stage_data:
        print("  ⚠️  Insufficient data for this night — skipping")
        return []

    # Build time-aligned lookup dicts (keyed by timestamp)
    def to_lookup(data, value_key="mean"):
        lookup = {}
        for row in data:
            ts = row.get("time", "")
            val = row.get(value_key)
            if ts and val is not None:
                lookup[ts] = val
        return lookup

    hr_lookup = to_lookup(hr_data)
    hrv_lookup = to_lookup(hrv_data)
    rr_lookup = to_lookup(rr_data)

    # Stage lookup is special — uses "state" key from input_text measurement
    stage_lookup = {}
    for row in stage_data:
        ts = row.get("time", "")
        val = row.get("state") or row.get("last") or row.get("value")
        if ts and val and val not in ("unknown", "unavailable", "", None):
            stage_lookup[ts] = val

    if not stage_lookup:
        print("  ⚠️  No valid sleep stages found — skipping")
        return []

    # Compute baselines from first 30 min of available HR/HRV
    hr_vals = list(hr_lookup.values())[:6]  # First 30 min
    hrv_vals = list(hrv_lookup.values())[:6]
    rr_vals = list(rr_lookup.values())[:6]

    if not hr_vals:
        print("  ⚠️  No HR data — skipping")
        return []

    hr_baseline = sum(hr_vals) / len(hr_vals)
    hrv_baseline = sum(hrv_vals) / len(hrv_vals) if hrv_vals else None
    rr_baseline = sum(rr_vals) / len(rr_vals) if rr_vals else None

    print(f"  Baselines: HR={hr_baseline:.1f}bpm"
          f"{f', HRV={hrv_baseline:.1f}ms' if hrv_baseline else ''}"
          f"{f', RR={rr_baseline:.1f}br/min' if rr_baseline else ''}")

    # Build training samples by finding nearest HR/HRV for each stage point
    # Stage changes are event-based (sparse), HR/HRV are bucketed every 5m.
    # For each stage event, find the closest HR/HRV reading.
    hr_times = sorted(hr_lookup.keys())
    samples = []
    bedtime_dt = start_utc

    for ts, stage in sorted(stage_lookup.items()):
        if stage not in ("deep", "core", "rem", "awake"):
            continue

        # Find closest HR reading by timestamp
        hr = hr_lookup.get(ts)
        hrv = hrv_lookup.get(ts)

        # If no exact match, find nearest bucket
        if hr is None and hr_times:
            nearest = min(hr_times, key=lambda t: abs(
                _parse_ts(t).timestamp() - _parse_ts(ts).timestamp()
            ))
            # Only use if within 10 minutes
            if abs(_parse_ts(nearest).timestamp() - _parse_ts(ts).timestamp()) < 600:
                hr = hr_lookup.get(nearest)
                hrv = hrv_lookup.get(nearest)
                rr_key = nearest
            else:
                continue
        else:
            rr_key = ts

        if hr is None or hr < 30:
            continue

        try:
            ts_dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            hours_in = (ts_dt - bedtime_dt.replace(
                tzinfo=ts_dt.tzinfo)).total_seconds() / 3600
        except (ValueError, TypeError):
            hours_in = 0.0

        if hours_in < 0 or hours_in > 14:
            continue

        hr_pct = (hr - hr_baseline) / hr_baseline if hr_baseline else 0
        hrv_pct = ((hrv - hrv_baseline) / hrv_baseline
                   if hrv and hrv_baseline and hrv_baseline > 0 else 0)

        sample = {
            "stage": stage,
            "hr": round(hr, 1),
            "hrv": round(hrv, 1) if hrv else 0,
            "hr_pct": round(hr_pct, 4),
            "hrv_pct": round(hrv_pct, 4),
            "hours_in": round(hours_in, 2),
            "source": "influxdb_recovery",
            "night": date_str,
        }

        rr = rr_lookup.get(rr_key) or rr_lookup.get(ts)
        if rr and rr_baseline and rr_baseline > 0:
            sample["resp_rate"] = round(rr, 1)
            sample["resp_rate_pct"] = round(
                (rr - rr_baseline) / rr_baseline, 4)

        samples.append(sample)

    # Deduplicate by timestamp
    seen = set()
    unique = []
    for s in samples:
        key = (s["night"], s["hours_in"], s["stage"])
        if key not in seen:
            seen.add(key)
            unique.append(s)

    from collections import Counter
    dist = Counter(s["stage"] for s in unique)
    print(f"  Recovered {len(unique)} training samples: {dict(dist)}")

    return unique


def main():
    parser = argparse.ArgumentParser(
        description="Recover training data from InfluxDB history")
    parser.add_argument(
        "--influx-host", default="192.168.0.106",
        help="InfluxDB host IP (default: 192.168.0.106)")
    parser.add_argument(
        "--start-date", default="2026-03-05",
        help="First night to recover (default: 2026-03-05)")
    parser.add_argument(
        "--end-date", default=None,
        help="Last night to recover (default: yesterday)")
    parser.add_argument(
        "--merge-state", action="store_true",
        help="Merge recovered data into controller_state.json")
    args = parser.parse_args()

    host = f"http://{args.influx_host}:8086"

    # Determine date range
    start = datetime.strptime(args.start_date, "%Y-%m-%d")
    if args.end_date:
        end = datetime.strptime(args.end_date, "%Y-%m-%d")
    else:
        end = datetime.now() - timedelta(days=1)

    all_samples = []
    current = start
    while current <= end:
        date_str = current.strftime("%Y-%m-%d")
        try:
            night_samples = recover_night(date_str, host)
            all_samples.extend(night_samples)
        except Exception as e:
            print(f"  ❌ Error: {e}")
        current += timedelta(days=1)

    print(f"\n{'='*60}")
    print(f"TOTAL: {len(all_samples)} training samples recovered")
    print(f"{'='*60}")

    if not all_samples:
        print("No data recovered. Check InfluxDB connectivity and entity names.")
        return

    # Stage distribution
    from collections import Counter
    dist = Counter(s["stage"] for s in all_samples)
    for stage, count in sorted(dist.items()):
        pct = count / len(all_samples) * 100
        print(f"  {stage:8s}: {count:4d} ({pct:.0f}%)")

    # Save recovered data
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    output_file = OUTPUT_DIR / "recovered_training_data.json"
    output_file.write_text(json.dumps(all_samples, indent=2))
    print(f"\nSaved to {output_file}")

    # Optionally merge into controller_state.json
    if args.merge_state:
        state_file = OUTPUT_DIR / "controller_state.json"
        if state_file.exists():
            state = json.loads(state_file.read_text())
            for zone in state:
                existing = state[zone].get("state", {}).get(
                    "stage_training_data", [])
                # Deduplicate by (night, hours_in, stage)
                existing_keys = {
                    (s.get("night", ""), s.get("hours_in"), s.get("stage"))
                    for s in existing
                }
                new_samples = [
                    s for s in all_samples
                    if (s.get("night"), s.get("hours_in"), s.get("stage"))
                    not in existing_keys
                ]
                if "state" not in state[zone]:
                    state[zone]["state"] = {}
                combined = existing + new_samples
                # Keep last 500 samples
                state[zone]["state"]["stage_training_data"] = combined[-500:]
                print(f"[{zone}] Merged: {len(existing)} existing "
                      f"+ {len(new_samples)} new "
                      f"= {len(combined)} total "
                      f"(capped at 500)")
            state_file.write_text(json.dumps(state, indent=2))
            print(f"Updated {state_file}")
        else:
            print(f"No {state_file} found — use recovered_training_data.json "
                  f"directly with train_stage_classifier.py")


if __name__ == "__main__":
    main()
