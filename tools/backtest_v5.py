#!/usr/bin/env python3
"""
PerfectlySnug Controller v5 Backtester
=======================================
Replays controller logic against historical data to evaluate
whether proposed parameter changes would reduce manual overrides.

Usage:
    cd PerfectlySnug
    python3 tools/backtest_v5.py
"""

import csv
import io
import math
import subprocess
import sys
from collections import defaultdict
from datetime import datetime, timedelta

# ── Database (via SSH + psql) ────────────────────────────────────────────
SSH_HOST = "macmini"
DB_CMD = 'PGPASSWORD=sleepsync_local psql -U sleepsync -h localhost -d sleepdata --csv -c'


def query_pg(sql):
    """Run SQL against sleepdata via SSH and return CSV rows as dicts."""
    # Use subprocess list form to avoid shell quoting issues
    remote_cmd = f'PGPASSWORD=sleepsync_local psql -U sleepsync -h localhost -d sleepdata --csv -c "{sql}"'
    result = subprocess.run(
        ["ssh", SSH_HOST, remote_cmd],
        capture_output=True, text=True, timeout=30
    )
    if result.returncode != 0:
        print(f"DB ERROR: {result.stderr}", file=sys.stderr)
        sys.exit(1)
    reader = csv.DictReader(io.StringIO(result.stdout))
    return list(reader)


# ── Controller Constants (from v5) ──────────────────────────────────────
L1_TO_BLOWER_PCT = {
    -10: 100, -9: 87, -8: 75, -7: 65, -6: 50,
    -5: 41, -4: 33, -3: 26, -2: 20, -1: 10, 0: 0,
}
CYCLE_SETTINGS = {1: -10, 2: -9, 3: -8, 4: -7, 5: -6, 6: -5}
CYCLE_DURATION_MIN = 90


def l1_to_blower(val):
    val = max(-10, min(0, int(round(val))))
    return L1_TO_BLOWER_PCT[val]


def blower_to_l1(pct):
    pct = max(0, min(100, int(round(pct))))
    return min(L1_TO_BLOWER_PCT, key=lambda l1: (abs(L1_TO_BLOWER_PCT[l1] - pct), l1))


def get_cycle_num(elapsed_min):
    return min(max(1, int(elapsed_min / CYCLE_DURATION_MIN) + 1), 6)


def room_comp_blower(room_temp, ref_temp, comp_rate, cold_extra=3.0):
    """Room temperature compensation in blower % space."""
    if room_temp is None:
        return 0
    if room_temp > ref_temp:
        return round((room_temp - ref_temp) * comp_rate)
    elif room_temp < ref_temp:
        comp = (ref_temp - room_temp) * comp_rate
        if room_temp < 63.0:
            comp += (63.0 - room_temp) * cold_extra
        return -round(comp)
    return 0


# ── Scenario Definitions ─────────────────────────────────────────────────

class Scenario:
    def __init__(self, name, learning_max=30, learning_decay=0.7,
                 room_ref=68.0, room_comp_rate=4.0,
                 use_learning=True, use_actual_learned=False,
                 min_elapsed_for_learning=0):
        self.name = name
        self.learning_max = learning_max
        self.learning_decay = learning_decay
        self.room_ref = room_ref
        self.room_comp_rate = room_comp_rate
        self.use_learning = use_learning
        self.use_actual_learned = use_actual_learned
        self.min_elapsed_for_learning = min_elapsed_for_learning
        self.learned = {}
        self._override_history = defaultdict(list)

    def compute_setting(self, elapsed_min, room_temp, learned_adj_override=None):
        """Compute what the controller WOULD set."""
        cycle_num = get_cycle_num(elapsed_min)
        base_setting = CYCLE_SETTINGS.get(cycle_num, -5)
        base_blower = l1_to_blower(base_setting)
        target_blower = base_blower

        # Learning adjustment
        if self.use_learning:
            if self.use_actual_learned and learned_adj_override is not None:
                adj = max(-self.learning_max, min(self.learning_max, learned_adj_override))
            else:
                adj = self.learned.get(cycle_num, 0)
            target_blower += adj

        # Room compensation
        rc = room_comp_blower(room_temp, self.room_ref, self.room_comp_rate)
        target_blower += rc

        target_blower = max(0, min(100, round(target_blower)))
        return blower_to_l1(target_blower), cycle_num

    def update_learning_from_night(self, night_overrides):
        """Update learned adjustments after a night. night_overrides: [(elapsed, user_l1, ctrl_l1)]"""
        if not self.use_learning or self.use_actual_learned:
            return

        filtered = [(e, o, c) for e, o, c in night_overrides
                    if e >= self.min_elapsed_for_learning]
        if not filtered:
            return

        # Last override per cycle for this night
        cycle_deltas = {}
        for elapsed_min, override_l1, ctrl_l1 in filtered:
            cycle = get_cycle_num(elapsed_min)
            delta = l1_to_blower(override_l1) - l1_to_blower(ctrl_l1)
            cycle_deltas[cycle] = delta

        for cycle, delta in cycle_deltas.items():
            self._override_history[cycle].append(delta)

        # Recompute learned from full history with decay
        for cycle, deltas in self._override_history.items():
            if not deltas:
                continue
            weighted_sum = 0
            weight_total = 0
            for i, d in enumerate(reversed(deltas)):
                w = self.learning_decay ** i
                weighted_sum += d * w
                weight_total += w
            avg_delta = weighted_sum / weight_total if weight_total else 0
            adj = max(-self.learning_max, min(self.learning_max, round(avg_delta)))
            if adj != 0:
                self.learned[cycle] = adj
            elif cycle in self.learned:
                del self.learned[cycle]


def make_scenarios():
    return {
        "A": Scenario("A: Current (baseline)", learning_max=30, learning_decay=0.7,
                      room_ref=68.0, room_comp_rate=4.0, use_actual_learned=True),
        "B": Scenario("B: Reset + Conservative", learning_max=15, learning_decay=0.85,
                      room_ref=70.0, room_comp_rate=4.0, min_elapsed_for_learning=5),
        "C": Scenario("C: Reset + Aggr Room Comp", learning_max=15, learning_decay=0.85,
                      room_ref=70.0, room_comp_rate=6.0, min_elapsed_for_learning=5),
        "D": Scenario("D: No Learning (baselines+room)", room_ref=70.0, room_comp_rate=5.0,
                      use_learning=False),
    }


# ── Data Loading & Processing ────────────────────────────────────────────

def load_data():
    """Load v5 left-zone readings via SSH+psql."""
    sql = ("SELECT ts, elapsed_min, room_temp_f, phase, setting, effective, "
           "learned_adj, action, baseline, body_avg_f, ambient_f "
           "FROM controller_readings WHERE zone='left' "
           "AND controller_version='v5_rc_off' "
           "AND action NOT IN ('empty_bed', 'passive') ORDER BY ts")
    rows = query_pg(sql)
    readings = []
    for r in rows:
        readings.append({
            "ts": datetime.fromisoformat(r["ts"]),
            "elapsed_min": float(r["elapsed_min"]) if r["elapsed_min"] else 0,
            "room_temp": float(r["room_temp_f"]) if r["room_temp_f"] else None,
            "phase": r["phase"],
            "setting": int(r["setting"]) if r["setting"] else None,
            "effective": int(r["effective"]) if r["effective"] else None,
            "learned_adj": float(r["learned_adj"]) if r["learned_adj"] else None,
            "action": r["action"],
            "baseline": int(r["baseline"]) if r["baseline"] else None,
            "body_avg": float(r["body_avg_f"]) if r["body_avg_f"] else None,
            "ambient": float(r["ambient_f"]) if r["ambient_f"] else None,
        })
    return readings


def group_into_nights(readings, gap_hours=2):
    if not readings:
        return []
    nights = []
    current = [readings[0]]
    for i in range(1, len(readings)):
        gap = (readings[i]["ts"] - readings[i-1]["ts"]).total_seconds() / 3600
        if gap > gap_hours:
            if len(current) >= 5:
                nights.append(current)
            current = [readings[i]]
        else:
            current.append(readings[i])
    if len(current) >= 5:
        nights.append(current)
    return nights


def identify_overrides(night):
    """Find override events. Returns [(index, elapsed_min, override_l1, controller_l1)]."""
    overrides = []
    for i, r in enumerate(night):
        if r["action"] == "override":
            if r["setting"] is not None and r["effective"] is not None:
                overrides.append((i, r["elapsed_min"], r["setting"], r["effective"]))
    return overrides


def get_user_preference_timeline(night, overrides):
    """Build user's preferred setting at each reading."""
    prefs = [None] * len(night)
    override_points = [(idx, oval) for idx, _, oval, _ in overrides]

    if not override_points:
        for i in range(len(night)):
            prefs[i] = night[i]["setting"]
        return prefs

    # Before first override: assume current setting is fine
    first_idx = override_points[0][0]
    for i in range(first_idx):
        prefs[i] = night[i]["setting"]

    # After each override: preference = override value until next
    for oidx in range(len(override_points)):
        start_idx = override_points[oidx][0]
        end_idx = override_points[oidx + 1][0] if oidx + 1 < len(override_points) else len(night)
        val = override_points[oidx][1]
        for i in range(start_idx, end_idx):
            prefs[i] = val

    return prefs


def simulate_night(scenario, night, overrides):
    """Simulate controller for one night. Returns metrics."""
    prefs = get_user_preference_timeline(night, overrides)
    predicted_overrides = 0
    deviations = []

    for i, r in enumerate(night):
        learned_adj_override = None
        if scenario.use_actual_learned and r["learned_adj"] is not None:
            learned_adj_override = int(r["learned_adj"])

        sim_setting, _ = scenario.compute_setting(
            r["elapsed_min"], r["room_temp"], learned_adj_override)

        pref = prefs[i]
        if pref is not None:
            dev = sim_setting - pref
            deviations.append(dev)
            if abs(dev) > 1:
                predicted_overrides += 1

    return {"predicted_overrides": predicted_overrides, "deviations": deviations}


# ── Main ─────────────────────────────────────────────────────────────────

def run_backtest():
    print("=" * 78)
    print("  PERFECTLYSNUG v5 CONTROLLER BACKTEST")
    print("  Replaying 14 nights of historical data against 4 parameter scenarios")
    print("=" * 78)
    print()

    print("Loading data from PostgreSQL via SSH...")
    readings = load_data()
    print(f"  Loaded {len(readings)} readings")

    nights = group_into_nights(readings)
    print(f"  Grouped into {len(nights)} night sessions")
    print()

    # Night summary
    print(f"{'#':>3} {'Date':<12} {'Rdgs':>5} {'Dur':>6} {'Ovr':>4} {'AvgRoom':>7} {'MinRoom':>7}")
    print("-" * 52)
    total_actual = 0
    for i, night in enumerate(nights):
        date = night[0]["ts"].strftime("%Y-%m-%d")
        dur = (night[-1]["ts"] - night[0]["ts"]).total_seconds() / 3600
        ovrs = identify_overrides(night)
        total_actual += len(ovrs)
        rtemps = [r["room_temp"] for r in night if r["room_temp"] is not None]
        avg_r = sum(rtemps) / len(rtemps) if rtemps else 0
        min_r = min(rtemps) if rtemps else 0
        print(f"{i+1:>3} {date:<12} {len(night):>5} {dur:>5.1f}h {len(ovrs):>4} {avg_r:>6.1f}F {min_r:>6.1f}F")
    print(f"{'TOT':>3} {'':12} {len(readings):>5} {'':>6} {total_actual:>4}")
    print()

    # Run scenarios
    scenarios = make_scenarios()
    results = {}
    n = len(nights)

    for key in ["A", "B", "C", "D"]:
        scenario = scenarios[key]
        night_results = []
        total_pred = 0
        total_devs = []
        ovr_free = 0

        for i, night in enumerate(nights):
            overrides = identify_overrides(night)
            sim = simulate_night(scenario, night, overrides)
            night_results.append({
                "date": night[0]["ts"].strftime("%Y-%m-%d"),
                "actual": len(overrides),
                "predicted": sim["predicted_overrides"],
                "learned": dict(scenario.learned),
            })
            total_pred += sim["predicted_overrides"]
            total_devs.extend(sim["deviations"])
            if sim["predicted_overrides"] == 0:
                ovr_free += 1

            # Update learning after each night
            if scenario.use_learning and not scenario.use_actual_learned:
                ovr_data = [(e, o, c) for _, e, o, c in overrides]
                scenario.update_learning_from_night(ovr_data)

        total_readings = len(total_devs)
        avg_abs = sum(abs(d) for d in total_devs) / total_readings if total_readings else 0
        mean_dev = sum(total_devs) / total_readings if total_readings else 0
        comfort = 100 * (1 - total_pred / total_readings) if total_readings else 0

        results[key] = {
            "scenario": scenario, "nights": night_results,
            "total_pred": total_pred, "ovr_free": ovr_free,
            "avg_abs": avg_abs, "mean_dev": mean_dev,
            "total_readings": total_readings, "comfort": comfort,
        }

    # Summary table
    print("=" * 78)
    print("RESULTS SUMMARY")
    print("=" * 78)
    print(f"{'Scenario':<33} {'PredOvr':>7} {'OvrFree':>8} {'AvgDev':>7} {'MnDev':>7} {'Comfort':>8}")
    print("-" * 78)
    for key in ["A", "B", "C", "D"]:
        r = results[key]
        print(f"  {r['scenario'].name:<31} {r['total_pred']:>7} "
              f"{r['ovr_free']:>3}/{n:<3} "
              f"{r['avg_abs']:>6.2f} {r['mean_dev']:>+6.2f} "
              f"{r['comfort']:>7.1f}%")
    print()
    print(f"  Actual override events in data: {total_actual}")
    print(f"  PredOvr = readings where |sim - user_pref| > 1 L1 step")
    print(f"  Comfort = % readings within ±1 of user preference")
    print()

    # Per-night breakdown
    print("PER-NIGHT PREDICTED OVERRIDES (# readings out-of-comfort)")
    print("-" * 65)
    print(f"{'#':>3} {'Date':<12} {'Actual':>6}   {'A':>5} {'B':>5} {'C':>5} {'D':>5}")
    print("-" * 65)
    for i in range(n):
        actual = len(identify_overrides(nights[i]))
        row = f"{i+1:>3} {nights[i][0]['ts'].strftime('%Y-%m-%d'):<12} {actual:>6}  "
        for key in ["A", "B", "C", "D"]:
            pred = results[key]["nights"][i]["predicted"]
            row += f" {pred:>5}"
        print(row)
    print()

    # Learning evolution for B
    print("LEARNING EVOLUTION — Scenario B (Conservative)")
    print("-" * 55)
    for nr in results["B"]["nights"]:
        learned_str = ", ".join(f"c{k}:{v:+d}%" for k, v in sorted(nr["learned"].items()))
        print(f"  Night {nr['date']}: {learned_str or '(empty)'}")
    print()

    # Deviation distribution
    print("DEVIATION DISTRIBUTION (sim - preferred, L1 steps)")
    print("-" * 55)
    for key in ["A", "B", "C", "D"]:
        r = results[key]
        devs = []
        for nr in r["nights"]:
            devs.extend([])  # We need to recalculate...
        # Use total_devs from simulation
        bins = defaultdict(int)
        # Re-run to get devs per scenario (use stored total_devs concept)
    # Actually let's just print a simpler version
    for key in ["A", "B", "C", "D"]:
        r = results[key]
        print(f"\n  {r['scenario'].name}:")
        print(f"    Avg |deviation|: {r['avg_abs']:.2f} L1 steps")
        print(f"    Mean deviation:  {r['mean_dev']:+.2f} (+ = too cold, - = too warm)")
        print(f"    Comfort rate:    {r['comfort']:.1f}%")
    print()

    # Statistical power analysis
    print("=" * 78)
    print("STATISTICAL POWER ANALYSIS")
    print("How many nights needed to confirm improvement (95% confidence)?")
    print("=" * 78)
    print()

    nights_with_ovr = sum(1 for night in nights if len(identify_overrides(night)) > 0)
    baseline_success = (n - nights_with_ovr) / n
    print(f"  Baseline: {n - nights_with_ovr}/{n} override-free = {baseline_success:.1%}")
    print()

    for key in ["B", "C", "D"]:
        r = results[key]
        pred_success = r["ovr_free"] / n
        print(f"  {r['scenario'].name}")
        print(f"    Predicted override-free: {r['ovr_free']}/{n} = {pred_success:.1%}")

        if pred_success > baseline_success:
            p1 = max(0.01, baseline_success)
            p2 = min(0.99, pred_success)
            effect = p2 - p1
            if effect > 0:
                z_a, z_80, z_90 = 1.96, 0.84, 1.28
                n_80 = math.ceil(((z_a + z_80)**2 * (p1*(1-p1) + p2*(1-p2))) / effect**2)
                n_90 = math.ceil(((z_a + z_90)**2 * (p1*(1-p1) + p2*(1-p2))) / effect**2)
                print(f"    → {n_80} nights needed (80% power)")
                print(f"    → {n_90} nights needed (90% power)")
                print(f"    → ~{math.ceil(n_80 / 7)} weeks of data collection")
        elif pred_success <= baseline_success:
            print(f"    → No improvement over baseline")
        print()

    # Recommendation
    best_key = min(["A", "B", "C", "D"],
                   key=lambda k: (results[k]["total_pred"], results[k]["avg_abs"]))
    best = results[best_key]
    print("=" * 78)
    print("RECOMMENDATION")
    print("=" * 78)
    print(f"\n  Best: {best['scenario'].name}")
    print(f"    Comfort: {best['comfort']:.1f}%")
    print(f"    Override-free nights: {best['ovr_free']}/{n}")
    if best_key != "A" and results["A"]["total_pred"] > 0:
        imp = results["A"]["total_pred"] - best["total_pred"]
        pct = 100 * imp / results["A"]["total_pred"]
        print(f"    {imp} fewer discomfort readings ({pct:.0f}% reduction vs current)")
    s = best["scenario"]
    print(f"\n  Parameters:")
    print(f"    LEARNING_MAX_BLOWER_ADJ = {s.learning_max}")
    print(f"    LEARNING_DECAY = {s.learning_decay}")
    print(f"    ROOM_BLOWER_REFERENCE_F = {s.room_ref}")
    print(f"    ROOM_BLOWER_COLD_COMP_PER_F = {s.room_comp_rate}")
    print(f"    MIN_ELAPSED_FOR_LEARNING = {s.min_elapsed_for_learning} min")
    print(f"    use_learning = {s.use_learning}")
    print()


if __name__ == "__main__":
    run_backtest()
