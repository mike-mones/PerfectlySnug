#!/usr/bin/env python3
"""
End-to-End Pipeline Test — SleepSync + PerfectlySnug
=====================================================

Tests every layer of the sleep temperature control pipeline.
Run during the day to catch issues BEFORE tonight.

Usage:
    source ~/.zshenv
    python3 tools/test_full_pipeline.py          # Automated layers 1-7
    python3 tools/test_full_pipeline.py --live    # Also waits for real SleepSync data

Layers:
    1. HA API connectivity
    2. Webhook -> HA entities (sends test payload, verifies entities update)
    3. Pre-cool automation trigger (fires it, verifies topper goes to -10)
    4. Topper hardware reachable + sensors valid
    5. AppDaemon controller health
    6. SleepSync app installation check
    7. All required HA entities exist
    8. (--live) Toggle Sleep Focus, verify real data flows within 2 min
"""

import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime

# -- Configuration -------------------------------------------------------

HA_URL = os.environ.get("HA_URL", "").rstrip("/")
HA_TOKEN = os.environ.get("HA_TOKEN", "")
TOPPER_LEFT_IP = "192.168.0.159"
WEBHOOK_ID = "apple_health_import"

# Test markers -- distinctive values for verification
TEST_HR = 99.9
TEST_HRV = 88.8
TEST_STAGE = "deep"


class C:
    G = "\033[92m"   # green
    R = "\033[91m"   # red
    Y = "\033[93m"   # yellow
    B = "\033[96m"   # cyan
    BOLD = "\033[1m"
    END = "\033[0m"


results = {"pass": 0, "fail": 0, "warn": 0, "details": []}


def p(msg):
    print(f"  {C.G}PASS{C.END}  {msg}")
    results["pass"] += 1
    results["details"].append(("PASS", msg))


def f(msg):
    print(f"  {C.R}FAIL{C.END}  {msg}")
    results["fail"] += 1
    results["details"].append(("FAIL", msg))


def w(msg):
    print(f"  {C.Y}WARN{C.END}  {msg}")
    results["warn"] += 1
    results["details"].append(("WARN", msg))


def info(msg):
    print(f"  {C.B}i{C.END}  {msg}")


def section(title):
    print(f"\n{C.BOLD}{'=' * 60}{C.END}")
    print(f"{C.BOLD}  Layer {title}{C.END}")
    print(f"{C.BOLD}{'=' * 60}{C.END}")


def ha_get(path):
    """GET from HA API. Returns parsed JSON or None."""
    req = urllib.request.Request(
        f"{HA_URL}{path}",
        headers={"Authorization": f"Bearer {HA_TOKEN}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read())
    except Exception:
        return None


def ha_post(path, data=None, timeout=30):
    """POST to HA API. Returns (status_code, body_text)."""
    body = json.dumps(data or {}).encode()
    req = urllib.request.Request(
        f"{HA_URL}{path}",
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {HA_TOKEN}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()
    except Exception as e:
        return 0, f"{type(e).__name__}: {e}"


def webhook_post(payload):
    """POST to webhook (no auth). Returns HTTP status."""
    body = json.dumps(payload).encode()
    req = urllib.request.Request(
        f"{HA_URL}/api/webhook/{WEBHOOK_ID}",
        data=body,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status
    except urllib.error.HTTPError as e:
        return e.code
    except Exception:
        return 0


def get_state(entity_id):
    """Read a single entity state string. Returns None on error."""
    data = ha_get(f"/api/states/{entity_id}")
    if data:
        return data.get("state")
    return None


# ================================================================
# Layer 1: HA API Connectivity
# ================================================================

def test_layer1():
    section("1 -- HA API Connectivity")

    if not HA_URL or not HA_TOKEN:
        f("HA_URL or HA_TOKEN not set -- run: source ~/.zshenv")
        return False

    data = ha_get("/api/")
    if data and "message" in data:
        p(f"HA API reachable ({data['message']})")
    else:
        f(f"HA API unreachable at {HA_URL}")
        return False

    config = ha_get("/api/config")
    if config:
        p(f"HA {config.get('version', '?')}, loc={config.get('location_name', '?')}")
    else:
        w("Could not read HA config")

    return True


# ================================================================
# Layer 2: Webhook -> HA Entities
# ================================================================

def test_layer2():
    section("2 -- Webhook -> HA Entities")

    # Save current values to restore
    hr_before = get_state("input_number.apple_health_hr_avg")
    hrv_before = get_state("input_number.apple_health_hrv")
    stage_before = get_state("input_text.apple_health_sleep_stage")

    if hr_before is None:
        f("input_number.apple_health_hr_avg does not exist")
        return False

    info(f"Before: HR={hr_before}, HRV={hrv_before}, Stage={stage_before}")

    # Exact payload format that SleepSync sends
    payload = {
        "data": {
            "metrics": [
                {
                    "name": "heart_rate",
                    "data": [{"Avg": TEST_HR, "Min": TEST_HR, "Max": TEST_HR}],
                },
                {
                    "name": "heart_rate_variability",
                    "data": [{"qty": TEST_HRV}],
                },
                {
                    "name": "sleep_stage",
                    "data": [{"stage": TEST_STAGE}],
                },
            ]
        }
    }

    status = webhook_post(payload)
    if status == 200:
        p("Webhook accepted (HTTP 200)")
    else:
        f(f"Webhook rejected: HTTP {status}")
        return False

    time.sleep(3)

    # Check HR
    hr_after = get_state("input_number.apple_health_hr_avg")
    if hr_after and abs(float(hr_after) - TEST_HR) < 0.5:
        p(f"HR updated: {hr_before} -> {hr_after}")
    else:
        f(f"HR NOT updated: {hr_after} (expected {TEST_HR})")

    # Check HRV
    hrv_after = get_state("input_number.apple_health_hrv")
    if hrv_after and abs(float(hrv_after) - TEST_HRV) < 0.5:
        p(f"HRV updated: {hrv_before} -> {hrv_after}")
    else:
        f(f"HRV NOT updated: {hrv_after} (expected {TEST_HRV})")

    # Check sleep stage
    stage_after = get_state("input_text.apple_health_sleep_stage")
    if stage_after == TEST_STAGE:
        p(f"Sleep stage updated: {stage_before} -> {stage_after}")
    elif stage_after is None:
        f("input_text.apple_health_sleep_stage entity missing")
    else:
        f(f"Sleep stage NOT updated: '{stage_after}' (expected '{TEST_STAGE}')")

    # Restore original values
    try:
        if hr_before and hr_before not in ("unknown", "unavailable"):
            ha_post("/api/services/input_number/set_value", {
                "entity_id": "input_number.apple_health_hr_avg",
                "value": float(hr_before),
            })
        if hrv_before and hrv_before not in ("unknown", "unavailable"):
            ha_post("/api/services/input_number/set_value", {
                "entity_id": "input_number.apple_health_hrv",
                "value": float(hrv_before),
            })
        if stage_before and stage_before not in ("unknown", "unavailable"):
            ha_post("/api/services/input_text/set_value", {
                "entity_id": "input_text.apple_health_sleep_stage",
                "value": stage_before,
            })
        info("Restored original entity values")
    except Exception:
        w("Could not fully restore original values")

    return True


# ================================================================
# Layer 3: Pre-Cool Automation
# ================================================================

def test_layer3():
    section("3 -- Pre-Cool Automation")

    automations = {
        "automation.topper_pre_cool_start_9_pm": "Pre-cool (9 PM)",
        "automation.topper_morning_off_9_30_am": "Morning off (9:30 AM)",
        "automation.topper_vacation_mode_not_home_by_1_am": "Vacation mode",
    }
    for eid, label in automations.items():
        st = get_state(eid)
        if st == "on":
            p(f"{label}: enabled")
        elif st:
            f(f"{label}: state='{st}' (expected 'on')")
        else:
            f(f"{label}: entity not found")

    # Save current topper temps
    presets = ["bedtime", "sleep", "wake"]
    saved = {}
    for preset in presets:
        eid = f"number.smart_topper_left_side_{preset}_temperature"
        saved[preset] = get_state(eid)
        if saved[preset] is not None:
            info(f"  Current {preset}: {saved[preset]}")
        else:
            f(f"  Cannot read {eid}")
            return False

    # Trigger pre-cool automation
    # The automation has a 60s verification delay. The Nabu Casa proxy may
    # drop the connection before that completes, so we accept any response
    # (including disconnect) and verify by checking entity states instead.
    info("Triggering pre-cool...")
    status, _body = ha_post("/api/services/automation/trigger", {
        "entity_id": "automation.topper_pre_cool_start_9_pm",
    }, timeout=10)
    if status == 200:
        p("Pre-cool triggered (API confirmed)")
    elif status == 0 and "timed out" in _body.lower():
        # Expected: automation blocks on 60s delay, our short timeout fires first
        p("Pre-cool triggered (fire-and-forget, automation still running)")
    elif status == 0:
        # Nabu Casa proxy disconnect — automation was still accepted
        p(f"Pre-cool triggered (proxy disconnected, expected with 60s delay)")
    else:
        f(f"Trigger failed: HTTP {status} — {_body}")
        return False

    time.sleep(5)

    # Verify all presets at -10
    all_ok = True
    for preset in presets:
        eid = f"number.smart_topper_left_side_{preset}_temperature"
        val = get_state(eid)
        if val is not None:
            try:
                fval = float(val)
                if fval == -10.0:
                    p(f"  {preset}: {fval}")
                else:
                    f(f"  {preset}: {fval} (expected -10)")
                    all_ok = False
            except ValueError:
                f(f"  {preset}: '{val}' (not a number)")
                all_ok = False
        else:
            f(f"  Cannot read {eid}")
            all_ok = False

    # Restore if needed
    needs_restore = any(
        saved.get(k) not in (None, "-10", "-10.0")
        for k in presets
    )
    if needs_restore:
        info("Restoring pre-trigger temps...")
        for preset in presets:
            v = saved[preset]
            if v and v not in ("unavailable", "unknown"):
                ha_post("/api/services/number/set_value", {
                    "entity_id": f"number.smart_topper_left_side_{preset}_temperature",
                    "value": float(v),
                })
        info("Restored")

    return all_ok


# ================================================================
# Layer 4: Topper Hardware
# ================================================================

def test_layer4():
    section("4 -- Topper Hardware")

    try:
        sock = socket.create_connection((TOPPER_LEFT_IP, 80), timeout=5)
        sock.close()
        p(f"Topper reachable at {TOPPER_LEFT_IP}:80")
    except Exception as e:
        f(f"Topper unreachable: {e}")
        return False

    climate = ha_get("/api/states/climate.smart_topper_left_side_left_side")
    if climate:
        p(f"Climate entity: {climate.get('state', '?')}")
    else:
        f("Climate entity not found")

    sensors_ok = 0
    for pos in ("right", "center", "left"):
        val = get_state(f"sensor.smart_topper_left_side_body_sensor_{pos}")
        if val:
            try:
                temp = float(val)
                if 50 < temp < 120:
                    sensors_ok += 1
                    info(f"  body_{pos}: {temp:.1f}F")
                else:
                    w(f"  body_{pos}: {temp:.1f}F (odd range)")
            except ValueError:
                w(f"  body_{pos}: '{val}'")
        else:
            w(f"  body_{pos}: not found")

    if sensors_ok == 3:
        p("All 3 body sensors valid")
    elif sensors_ok > 0:
        w(f"{sensors_ok}/3 body sensors valid")
    else:
        f("No body sensors valid")

    amb = get_state("sensor.smart_topper_left_side_ambient_temperature")
    if amb:
        try:
            p(f"Ambient: {float(amb):.1f}F")
        except ValueError:
            w(f"Ambient: '{amb}'")

    return True


# ================================================================
# Layer 5: AppDaemon Controller
# ================================================================

def test_layer5():
    section("5 -- AppDaemon Controller")

    st = get_state("update.appdaemon_update")
    if st is not None:
        p(f"AppDaemon add-on: update state={st}")
    else:
        w("AppDaemon status unknown via API")
        info("Check: ha addons logs a0d7b954_appdaemon | tail -30")

    needed = [
        "input_number.apple_health_hr_avg",
        "input_number.apple_health_hrv",
        "input_text.apple_health_sleep_stage",
        "number.smart_topper_left_side_bedtime_temperature",
        "number.smart_topper_left_side_sleep_temperature",
        "number.smart_topper_left_side_wake_temperature",
    ]
    missing = [e for e in needed if get_state(e) is None]
    if not missing:
        p(f"All {len(needed)} controller entities exist")
    else:
        for m in missing:
            f(f"Missing: {m}")

    return True


# ================================================================
# Layer 6: SleepSync App
# ================================================================

def test_layer6():
    section("6 -- SleepSync App")

    try:
        result = subprocess.run(
            ["xcrun", "xctrace", "list", "devices"],
            capture_output=True, text=True, timeout=15,
        )
        out = result.stdout + result.stderr
        if "iPhone" in out:
            p("iPhone detected")
        else:
            w("iPhone not detected")
        if "Apple Watch" in out:
            p("Apple Watch detected")
        else:
            w("Apple Watch not detected")
    except Exception as e:
        w(f"Device check failed: {e}")

    # Check build freshness
    derived = os.path.expanduser("~/Library/Developer/Xcode/DerivedData")
    if os.path.isdir(derived):
        for d in os.listdir(derived):
            if d.startswith("SleepSync-"):
                app = os.path.join(d, "Build/Products/Debug-iphoneos/SleepSyncPhone.app")
                full = os.path.join(derived, app)
                if os.path.exists(full):
                    age_h = (time.time() - os.path.getmtime(full)) / 3600
                    if age_h < 24:
                        p(f"SleepSyncPhone.app built {age_h:.1f}h ago")
                    else:
                        w(f"SleepSyncPhone.app built {age_h:.0f}h ago -- may be stale")
                else:
                    w("SleepSyncPhone.app not found in DerivedData")
                break
        else:
            w("No SleepSync DerivedData found")

    try:
        result = subprocess.run(
            ["shortcuts", "list"], capture_output=True, text=True, timeout=15,
        )
        if result.returncode == 0:
            count = len(result.stdout.strip().split("\n")) if result.stdout.strip() else 0
            p(f"Shortcuts CLI works ({count} shortcuts)")
        else:
            w("Shortcuts CLI error")
    except Exception:
        w("Shortcuts CLI not available")

    return True


# ================================================================
# Layer 7: All Required Entities
# ================================================================

def test_layer7():
    section("7 -- Required HA Entities")

    entities = {
        "input_number.apple_health_hr_avg": "HR avg",
        "input_number.apple_health_hr_min": "HR min",
        "input_number.apple_health_hr_max": "HR max",
        "input_number.apple_health_hrv": "HRV",
        "input_number.apple_health_resting_hr": "Resting HR",
        "input_number.apple_health_wrist_temp": "Wrist temp",
        "input_number.apple_health_respiratory_rate": "Resp rate",
        "input_number.apple_health_spo2": "SpO2",
        "input_number.apple_health_sleep_deep_hrs": "Deep hrs",
        "input_number.apple_health_sleep_rem_hrs": "REM hrs",
        "input_number.apple_health_sleep_core_hrs": "Core hrs",
        "input_number.apple_health_sleep_awake_hrs": "Awake hrs",
        "input_number.apple_health_breathing_disturbances": "Breathing",
        "input_text.apple_health_sleep_stage": "Sleep stage",
        "number.smart_topper_left_side_bedtime_temperature": "Topper bed",
        "number.smart_topper_left_side_sleep_temperature": "Topper sleep",
        "number.smart_topper_left_side_wake_temperature": "Topper wake",
        "climate.smart_topper_left_side_left_side": "Climate",
        "sensor.smart_topper_left_side_body_sensor_right": "Body R",
        "sensor.smart_topper_left_side_body_sensor_center": "Body C",
        "sensor.smart_topper_left_side_body_sensor_left": "Body L",
        "sensor.smart_topper_left_side_ambient_temperature": "Ambient",
        "switch.smart_topper_left_side_running": "Running",
        "person.michael_mones": "Person",
        "automation.topper_pre_cool_start_9_pm": "Pre-cool",
        "automation.topper_morning_off_9_30_am": "Morning-off",
        "automation.topper_vacation_mode_not_home_by_1_am": "Vacation",
    }

    ok = 0
    missing = []
    for eid, label in entities.items():
        val = get_state(eid)
        if val is not None and val != "unavailable":
            ok += 1
        else:
            missing.append((eid, label, val))

    if not missing:
        p(f"All {len(entities)} entities present and available")
    else:
        p(f"{ok}/{len(entities)} entities OK")
        for eid, label, val in missing:
            f(f"  {label}: {eid} = {val or 'NOT FOUND'}")

    home = get_state("person.michael_mones")
    if home == "home":
        p("Mike is home -- pre-cool WILL fire at 9 PM")
    else:
        w(f"Mike is '{home}' -- pre-cool may not fire (requires 'home')")

    return len(missing) == 0


# ================================================================
# Layer 8 (--live): Wait for Real SleepSync Data
# ================================================================

def test_layer8():
    section("8 -- LIVE: Real SleepSync Data Flow")

    print(f"""
  {C.BOLD}Manual steps:{C.END}
  1. On iPhone: Settings -> Focus -> Sleep -> toggle ON
  2. This test watches HA entities for 2 minutes
  3. If SleepSync works, HR data should appear within ~60s
  4. When done, toggle Sleep Focus back OFF

  {C.Y}Press Enter when Sleep Focus is ON...{C.END}""")

    input()
    print()

    hr_start = get_state("input_number.apple_health_hr_avg")
    hrv_start = get_state("input_number.apple_health_hrv")
    info(f"HR before: {hr_start}, HRV before: {hrv_start}")

    timeout = 120
    start = time.time()

    while time.time() - start < timeout:
        elapsed = int(time.time() - start)
        hr_now = get_state("input_number.apple_health_hr_avg")
        hrv_now = get_state("input_number.apple_health_hrv")
        stage_now = get_state("input_text.apple_health_sleep_stage")

        # Check if HR changed (from a real value, not our test marker)
        if hr_now != hr_start and hr_now not in (None, "0", "0.0", "unknown", "unavailable"):
            p(f"HR data arrived after {elapsed}s: {hr_start} -> {hr_now}")
            if hrv_now and hrv_now != hrv_start:
                p(f"HRV also updated: {hrv_start} -> {hrv_now}")
            if stage_now and stage_now not in ("unknown", "unavailable"):
                p(f"Sleep stage: {stage_now}")
            return True

        sys.stdout.write(f"\r  Waiting... {elapsed}s/{timeout}s (HR={hr_now})   ")
        sys.stdout.flush()
        time.sleep(10)

    print()
    f(f"No new HR data in {timeout}s")
    info("Troubleshooting:")
    info("  1. Is Sleep Focus actually ON?")
    info("  2. Is the watch on your wrist with HR readings?")
    info("  3. Open SleepSync on iPhone -- does it show 'Health Observer Active'?")
    info("  4. Check Console.app on Mac, filter by 'SleepSync'")
    return False


# ================================================================
# MAIN
# ================================================================

def main():
    live = "--live" in sys.argv

    print(f"\n{C.BOLD}{'=' * 60}{C.END}")
    print(f"{C.BOLD}  SleepSync + PerfectlySnug -- Full Pipeline Test{C.END}")
    print(f"{C.BOLD}{'=' * 60}{C.END}")
    print(f"  Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    url_display = (HA_URL[:40] + "...") if len(HA_URL) > 40 else HA_URL
    print(f"  HA:   {url_display or 'NOT SET'}")
    print(f"  Mode: {'LIVE' if live else 'AUTOMATED'}")

    if not HA_URL or not HA_TOKEN:
        print(f"\n{C.R}HA_URL and HA_TOKEN required. Run: source ~/.zshenv{C.END}")
        sys.exit(1)

    if not test_layer1():
        sys.exit(1)

    test_layer2()
    test_layer3()
    test_layer4()
    test_layer5()
    test_layer6()
    test_layer7()

    if live:
        test_layer8()

    # Summary
    print(f"\n{C.BOLD}{'=' * 60}{C.END}")
    print(f"{C.BOLD}  RESULTS{C.END}")
    print(f"{C.BOLD}{'=' * 60}{C.END}")
    total = results["pass"] + results["fail"] + results["warn"]
    print(f"  {C.G}Passed:   {results['pass']}{C.END}")
    print(f"  {C.R}Failed:   {results['fail']}{C.END}")
    print(f"  {C.Y}Warnings: {results['warn']}{C.END}")
    print(f"  Total:    {total}")

    if results["fail"] > 0:
        print(f"\n  {C.R}{C.BOLD}{results['fail']} FAILURE(S) -- FIX BEFORE TONIGHT{C.END}")
        print(f"\n  Failed:")
        for st, msg in results["details"]:
            if st == "FAIL":
                print(f"    - {msg}")
        sys.exit(1)
    elif results["warn"] > 0:
        print(f"\n  {C.Y}Some warnings -- review above{C.END}")
    else:
        print(f"\n  {C.G}{C.BOLD}All checks passed{C.END}")
    print()


if __name__ == "__main__":
    main()
