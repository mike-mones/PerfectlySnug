"""
Analyze real body temperature vs setting correlation from InfluxDB.
Helps calibrate the PID controller trajectory targets.
"""

import json
from collections import defaultdict
from urllib.parse import urlencode
from urllib.request import Request, urlopen


INFLUX_URL = "http://192.168.0.106:8086"
INFLUX_DB = "perfectly_snug"


def influx_query(query):
    url = f"{INFLUX_URL}/query?{urlencode({'db': INFLUX_DB, 'q': query})}"
    with urlopen(Request(url), timeout=15) as resp:
        data = json.loads(resp.read())
    series = data["results"][0].get("series", [])
    if not series:
        return []
    cols = series[0]["columns"]
    return [dict(zip(cols, row)) for row in series[0]["values"]]


def analyze_night(date, start_utc, end_utc, zone="left"):
    """Pull and correlate body temp with actual setting for one night."""
    prefix = f"smart_topper_{zone}_side"

    body = influx_query(
        f'SELECT mean(value) FROM "°F" '
        f"WHERE entity_id = '{prefix}_body_sensor_center' "
        f"AND time >= '{start_utc}' AND time <= '{end_utc}' "
        f"GROUP BY time(5m) fill(previous)"
    )

    setting = influx_query(
        f'SELECT mean(value) FROM "number.{prefix}_bedtime_temperature" '
        f"WHERE time >= '{start_utc}' AND time <= '{end_utc}' "
        f"GROUP BY time(5m) fill(previous)"
    )

    ambient = influx_query(
        f'SELECT mean(value) FROM "°F" '
        f"WHERE entity_id = '{prefix}_ambient_temperature' "
        f"AND time >= '{start_utc}' AND time <= '{end_utc}' "
        f"GROUP BY time(5m) fill(previous)"
    )

    # Build lookup maps
    s_map = {r["time"]: r["mean"] for r in setting if r["mean"] is not None}
    a_map = {r["time"]: r["mean"] for r in ambient if r["mean"] is not None}

    # Correlate
    by_setting = defaultdict(list)
    timeline = []

    for r in body:
        if r["mean"] is None:
            continue
        t = r["time"]
        st = s_map.get(t)
        amb = a_map.get(t)
        if st is not None:
            by_setting[int(round(st))].append(r["mean"])
        timeline.append({
            "time": t,
            "body_f": round(r["mean"], 1),
            "setting": int(round(st)) if st is not None else None,
            "ambient_f": round(amb, 1) if amb is not None else None,
        })

    return by_setting, timeline


def main():
    # Night windows (EST → UTC, +5 hours)
    nights = [
        ("2026-03-09", "2026-03-10T02:00:00Z", "2026-03-10T14:00:00Z"),
        ("2026-03-08", "2026-03-09T02:00:00Z", "2026-03-09T14:00:00Z"),
        ("2026-03-07", "2026-03-08T02:00:00Z", "2026-03-08T14:00:00Z"),
        ("2026-03-06", "2026-03-07T02:00:00Z", "2026-03-07T14:00:00Z"),
        ("2026-03-05", "2026-03-06T02:00:00Z", "2026-03-06T14:00:00Z"),
    ]

    all_by_setting = defaultdict(list)

    for date, start, end in nights:
        print(f"\n{'=' * 50}")
        print(f"NIGHT: {date}")
        print(f"{'=' * 50}")

        by_setting, timeline = analyze_night(date, start, end)

        if not by_setting:
            print("  No correlated data")
            continue

        # Print timeline at 30-min intervals
        print(f"  {'Hr':>4} | {'BodyF':>6} | {'Setting':>7} | {'AmbF':>6}")
        for i, p in enumerate(timeline):
            if i % 6 != 0:  # Every 30 min
                continue
            hr = i * 5 / 60
            st = f"{p['setting']:+d}" if p["setting"] is not None else "  —"
            amb = f"{p['ambient_f']:.1f}" if p["ambient_f"] is not None else "  —"
            print(f"  {hr:4.1f} | {p['body_f']:6.1f} | {st:>7} | {amb:>6}")

        print(f"\n  Body Temp by Setting:")
        for st in sorted(by_setting.keys()):
            temps = by_setting[st]
            avg = sum(temps) / len(temps)
            print(f"    Setting {st:+d}: avg={avg:.1f}°F  "
                  f"range={min(temps):.1f}–{max(temps):.1f}°F  "
                  f"n={len(temps)}")
            all_by_setting[st].extend(temps)

    # Cross-night aggregate
    print(f"\n{'=' * 50}")
    print("ALL NIGHTS AGGREGATED — Body Temp by Setting")
    print(f"{'=' * 50}")
    for st in sorted(all_by_setting.keys()):
        temps = all_by_setting[st]
        avg = sum(temps) / len(temps)
        print(f"  Setting {st:+d}: avg={avg:.1f}°F  "
              f"range={min(temps):.1f}–{max(temps):.1f}°F  "
              f"n={len(temps)}")

    # Recommendation
    print(f"\n{'=' * 50}")
    print("TRAJECTORY RECOMMENDATIONS")
    print(f"{'=' * 50}")
    print("The controller target should match what the body sensor ACTUALLY reads")
    print("at the settings you find comfortable, NOT a theoretical ideal.")
    print()
    for st in sorted(all_by_setting.keys()):
        temps = all_by_setting[st]
        avg = sum(temps) / len(temps)
        if len(temps) >= 10:  # Only meaningful sample sizes
            print(f"  At setting {st:+d}, your body sensor averages {avg:.1f}°F")
            print(f"    → If {st:+d} feels right, target should be ~{avg:.0f}°F")


if __name__ == "__main__":
    main()
