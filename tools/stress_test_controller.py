#!/usr/bin/env python3
"""
Live Controller Stress Test
============================

Injects simulated sleep stages, heart rate, and HRV into HA entities,
then watches what the AppDaemon controller does to the topper settings.

This tests the REAL deployed controller — not a local simulation.
We're pushing fake data through the actual pipeline and watching real outcomes.

Usage:
    python3 tools/stress_test_controller.py [--scenario SCENARIO]

Scenarios:
    full_night  — Simulate a full night (compressed to ~5 min)
    deep_only   — Stay in deep sleep (should cool aggressively)
    rem_only    — Stay in REM sleep (should warm slightly)
    hot_sleeper — Body temp runs high (82-87°F, should cool hard)
    restless    — Frequent awake periods (should freeze settings)
    stage_cycle — Rapid stage cycling every 30s (tests transition logic)
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

HA_URL = os.environ["HA_URL"]
HA_TOKEN = os.environ["HA_TOKEN"]
ET = timezone(timedelta(hours=-4))

session = requests.Session()
session.headers.update({
    "Authorization": f"Bearer {HA_TOKEN}",
    "Content-Type": "application/json",
})
retry = Retry(total=3, backoff_factor=1, status_forcelist=[502, 503, 504])
session.mount("https://", HTTPAdapter(max_retries=retry))

# Entities we'll inject data into
STAGE_ENTITY = "input_text.apple_health_sleep_stage"
HR_ENTITY = "input_number.apple_health_hr_avg"
HRV_ENTITY = "input_number.apple_health_hrv"

# Entities we watch for controller output
SLEEP_TEMP = "number.smart_topper_left_side_sleep_temperature"
BEDTIME_TEMP = "number.smart_topper_left_side_bedtime_temperature"
WAKE_TEMP = "number.smart_topper_left_side_wake_temperature"
RUNNING = "switch.smart_topper_left_side_running"
BODY_CENTER = "sensor.smart_topper_left_side_body_sensor_center"
CLIMATE = "climate.smart_topper_left_side_left_side"


def set_entity(entity_id, value):
    """Set an input entity's state via the HA API."""
    domain = entity_id.split(".")[0]
    if domain == "input_text":
        service = "input_text/set_value"
        data = {"entity_id": entity_id, "value": str(value)}
    elif domain == "input_number":
        service = "input_number/set_value"
        data = {"entity_id": entity_id, "value": float(value)}
    else:
        raise ValueError(f"Can't set entity type: {domain}")

    r = session.post(
        f"{HA_URL}/api/services/{service}",
        json=data,
        timeout=15,
    )
    return r.status_code == 200


def get_state(entity_id):
    """Get an entity's current state."""
    r = session.get(f"{HA_URL}/api/states/{entity_id}", timeout=15)
    if r.status_code == 200:
        return r.json()["state"]
    return None


def read_topper_state():
    """Read the current topper settings and status."""
    return {
        "sleep_temp": get_state(SLEEP_TEMP),
        "bedtime_temp": get_state(BEDTIME_TEMP),
        "wake_temp": get_state(WAKE_TEMP),
        "running": get_state(RUNNING),
        "body_center": get_state(BODY_CENTER),
        "climate": get_state(CLIMATE),
    }


def print_state(label, state, injected_stage, injected_hr, injected_hrv):
    """Print the current simulation state."""
    now = datetime.now(ET).strftime("%H:%M:%S")
    print(
        f"  [{now}] {label:20s} | "
        f"injected: stage={injected_stage:8s} HR={injected_hr:5.0f} HRV={injected_hrv:5.1f} | "
        f"topper: sleep={state['sleep_temp']:>3s} bedtime={state['bedtime_temp']:>3s} "
        f"wake={state['wake_temp']:>3s} | "
        f"running={state['running']:>3s} climate={state['climate']:>5s} | "
        f"body={state['body_center']:>7s}°F"
    )


# ── Scenario Definitions ─────────────────────────────────────────────

def scenario_full_night():
    """Simulate a compressed full night with realistic stage progression.
    Real night has ~5 sleep cycles of ~90 min each.
    We compress each cycle to ~60 seconds for a ~5 min total test.
    """
    # Each cycle: light(core) → deep → core → REM → brief awake
    cycles = [
        # Cycle 1: lots of deep
        ("core", 65, 45, 10),  ("deep", 58, 42, 12),
        ("core", 62, 44, 11),  ("rem", 55, 50, 15),
        ("awake", 68, 38, 8),
        # Cycle 2: moderate deep
        ("core", 63, 43, 10),  ("deep", 57, 41, 13),
        ("core", 61, 44, 12),  ("rem", 54, 52, 16),
        ("awake", 70, 36, 7),
        # Cycle 3: less deep, more REM
        ("core", 64, 42, 11),  ("deep", 59, 40, 12),
        ("rem", 53, 55, 18),   ("rem", 52, 56, 19),
        ("awake", 72, 35, 6),
        # Cycle 4: mostly REM
        ("core", 63, 43, 11),  ("rem", 54, 54, 17),
        ("rem", 52, 58, 20),
        ("awake", 73, 34, 6),
        # Cycle 5: light sleep → wake
        ("core", 66, 42, 10),  ("core", 68, 40, 9),
        ("awake", 75, 32, 5),
    ]
    return cycles


def scenario_deep_only():
    """Stay in deep sleep for the entire test. Controller should cool aggressively."""
    return [("deep", 56, 40, 14)] * 15


def scenario_rem_only():
    """Stay in REM for entire test. Controller should be warmest sleeping temp."""
    return [("rem", 53, 55, 18)] * 15


def scenario_hot_sleeper():
    """Body temp runs high. Controller should push cooling harder."""
    return [
        ("core", 72, 44, 10),  # HR high = hot
        ("deep", 68, 42, 12),
        ("core", 70, 45, 11),
        ("deep", 66, 43, 13),
        ("core", 68, 46, 12),
        ("rem", 64, 50, 14),
        ("core", 70, 44, 11),
        ("deep", 67, 42, 13),
        ("core", 69, 47, 12),
        ("rem", 65, 52, 15),
    ]


def scenario_restless():
    """Frequent awake periods. Controller should freeze during awake."""
    return [
        ("core", 65, 44, 10),
        ("awake", 75, 32, 6),   # woke up
        ("core", 64, 43, 11),
        ("awake", 76, 30, 5),   # woke up again
        ("deep", 58, 40, 14),
        ("awake", 74, 33, 6),   # another wake
        ("core", 63, 44, 11),
        ("rem", 55, 52, 16),
        ("awake", 72, 35, 7),   # restless
        ("core", 64, 43, 11),
        ("deep", 57, 41, 13),
        ("awake", 73, 34, 6),   # another
        ("rem", 54, 54, 17),
    ]


def scenario_stage_cycle():
    """Rapid stage cycling every step. Tests transition cooldown logic."""
    stages = ["core", "deep", "rem", "awake", "in_bed"]
    result = []
    for i in range(20):
        s = stages[i % len(stages)]
        hr = 60 + (i % 5) * 3
        hrv = 40 + (i % 4) * 5
        result.append((s, hr, hrv, 10))
    return result


SCENARIOS = {
    "full_night": scenario_full_night,
    "deep_only": scenario_deep_only,
    "rem_only": scenario_rem_only,
    "hot_sleeper": scenario_hot_sleeper,
    "restless": scenario_restless,
    "stage_cycle": scenario_stage_cycle,
}


def save_original_values():
    """Save current entity values so we can restore after test."""
    return {
        STAGE_ENTITY: get_state(STAGE_ENTITY),
        HR_ENTITY: get_state(HR_ENTITY),
        HRV_ENTITY: get_state(HRV_ENTITY),
    }


def restore_values(originals):
    """Restore original entity values after test."""
    for entity_id, value in originals.items():
        if value is not None:
            set_entity(entity_id, value)


def run_scenario(name, steps, step_interval=15):
    """
    Run a scenario: inject data at each step, wait for controller to react,
    and record the topper's response.

    step_interval: seconds between steps (controller runs every 180s,
    so we won't see changes every step — but we'll see the trend).
    """
    print(f"\n{'=' * 100}")
    print(f"  SCENARIO: {name}")
    print(f"  Steps: {len(steps)}, interval: {step_interval}s, "
          f"estimated time: {len(steps) * step_interval}s")
    print(f"  Controller loop: every 180s — injecting data faster to queue up inputs")
    print(f"{'=' * 100}\n")

    # Record baseline
    baseline = read_topper_state()
    print(f"  BASELINE: sleep={baseline['sleep_temp']} bedtime={baseline['bedtime_temp']} "
          f"wake={baseline['wake_temp']} running={baseline['running']} "
          f"body={baseline['body_center']}°F\n")

    history = []

    for i, step in enumerate(steps):
        stage, hr, hrv = step[0], step[1], step[2]

        # Inject the simulated data
        set_entity(STAGE_ENTITY, stage)
        set_entity(HR_ENTITY, hr)
        set_entity(HRV_ENTITY, hrv)

        # Small delay for HA to process
        time.sleep(1)

        # Read current topper state
        state = read_topper_state()
        print_state(f"Step {i+1:2d}/{len(steps)}", state, stage, hr, hrv)

        history.append({
            "step": i + 1,
            "stage": stage,
            "hr": hr,
            "hrv": hrv,
            "sleep_temp": state["sleep_temp"],
            "bedtime_temp": state["bedtime_temp"],
            "running": state["running"],
            "body": state["body_center"],
        })

        if i < len(steps) - 1:
            time.sleep(step_interval - 1)  # minus the 1s already waited

    # Summary
    print(f"\n{'─' * 100}")
    print(f"  RESULTS for {name}:")
    temps_seen = set()
    for h in history:
        temps_seen.add(h["sleep_temp"])

    temp_changes = 0
    for i in range(1, len(history)):
        if history[i]["sleep_temp"] != history[i-1]["sleep_temp"]:
            temp_changes += 1

    print(f"  Unique sleep temp values seen: {sorted(temps_seen)}")
    print(f"  Temperature changes during test: {temp_changes}")
    print(f"  Topper stayed running: {'✅' if all(h['running'] == 'on' for h in history) else '❌'}")

    # Check that awake stages didn't cause changes
    awake_changes = 0
    for i in range(1, len(history)):
        if history[i]["stage"] == "awake" and history[i]["sleep_temp"] != history[i-1]["sleep_temp"]:
            awake_changes += 1
    if any(h["stage"] == "awake" for h in history):
        print(f"  Settings changed during awake: {awake_changes} "
              f"({'✅ frozen as expected' if awake_changes == 0 else '⚠️ should be frozen'})")

    final = read_topper_state()
    print(f"\n  FINAL STATE: sleep={final['sleep_temp']} bedtime={final['bedtime_temp']} "
          f"wake={final['wake_temp']} running={final['running']}")
    print(f"{'─' * 100}\n")

    return history


def main():
    parser = argparse.ArgumentParser(description="Live controller stress test")
    parser.add_argument(
        "--scenario", "-s",
        choices=list(SCENARIOS.keys()) + ["all"],
        default="all",
        help="Which scenario to run (default: all)",
    )
    parser.add_argument(
        "--interval", "-i",
        type=int,
        default=12,
        help="Seconds between simulation steps (default: 12)",
    )
    args = parser.parse_args()

    print("=" * 100)
    print("  LIVE CONTROLLER STRESS TEST")
    print(f"  Time: {datetime.now(ET).strftime('%Y-%m-%d %I:%M %p ET')}")
    print("=" * 100)

    # Verify connectivity
    print("\n  Verifying HA connection...", end=" ")
    test = get_state(RUNNING)
    if test is None:
        print("FAILED — can't reach HA")
        sys.exit(1)
    print(f"OK (topper running={test})")

    # Save originals
    print("  Saving current entity values for restore...", end=" ")
    originals = save_original_values()
    print(f"OK (stage={originals[STAGE_ENTITY]}, hr={originals[HR_ENTITY]}, hrv={originals[HRV_ENTITY]})")

    scenarios_to_run = (
        list(SCENARIOS.keys()) if args.scenario == "all"
        else [args.scenario]
    )

    all_results = {}
    try:
        for name in scenarios_to_run:
            steps = SCENARIOS[name]()
            results = run_scenario(name, steps, step_interval=args.interval)
            all_results[name] = results
    except KeyboardInterrupt:
        print("\n\n  ⚠️  Interrupted!")
    finally:
        # ALWAYS restore original values
        print("\n  Restoring original entity values...", end=" ")
        restore_values(originals)
        print("OK")

    # Final summary
    print(f"\n{'=' * 100}")
    print("  OVERALL SUMMARY")
    print(f"{'=' * 100}")
    for name, results in all_results.items():
        temps = sorted(set(r["sleep_temp"] for r in results))
        changes = sum(1 for i in range(1, len(results))
                      if results[i]["sleep_temp"] != results[i-1]["sleep_temp"])
        always_on = all(r["running"] == "on" for r in results)
        print(f"  {name:15s}: temps={temps}, changes={changes}, always_on={'✅' if always_on else '❌'}")

    print(f"\n  NOTE: The controller runs every 180s. With {args.interval}s step intervals,")
    print(f"  you may not see changes every step. The controller queues up the latest")
    print(f"  injected state and acts on it at its next loop iteration.")
    print(f"  To see more controller reactions, increase --interval to 180+ seconds.")
    print(f"{'=' * 100}")


if __name__ == "__main__":
    main()
