"""
Pre-Sleep Checklist — Run before bed to verify everything works.

All green = safe to sleep with the controller active.
Any red = don't rely on the controller tonight.

Usage:
    export HA_TOKEN="..." HA_URL="https://...nabu.casa"
    python3 tools/pre_sleep_check.py
"""

import json
import os
import sys
import urllib.request
import urllib.error
from datetime import datetime, timezone

HA_URL = os.environ.get("HA_URL", "https://71gpwlkh7etf6xbve4xol5rdbfrovjt6.ui.nabu.casa")
HA_TOKEN = os.environ.get("HA_TOKEN", "")

PASS = "\033[92m✓\033[0m"
FAIL = "\033[91m✗\033[0m"
WARN = "\033[93m⚠\033[0m"
results = []


def check(name, condition, detail=""):
    results.append((name, condition))
    status = PASS if condition else FAIL
    suffix = f" — {detail}" if detail else ""
    print(f"  {status} {name}{suffix}")


def warn(name, detail=""):
    suffix = f" — {detail}" if detail else ""
    print(f"  {WARN} {name}{suffix}")


def api_get(path):
    url = f"{HA_URL}{path}"
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {HA_TOKEN}",
        "Content-Type": "application/json",
    })
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read())


def main():
    print("=" * 60)
    print("Pre-Sleep Checklist")
    print(f"{datetime.now().strftime('%Y-%m-%d %H:%M')} — {HA_URL}")
    print("=" * 60)

    if not HA_TOKEN:
        print(f"  {FAIL} HA_TOKEN not set")
        sys.exit(1)

    # 1. HA connectivity
    print("\n1. Home Assistant:")
    try:
        resp = api_get("/api/")
        check("API reachable", resp.get("message") == "API running.")
    except Exception as e:
        check("API reachable", False, str(e))
        print("\n  Cannot connect to HA — all checks will fail.")
        sys.exit(1)

    # 2. Topper sensors
    print("\n2. Smart Topper sensors:")
    try:
        sensors = {
            "body_center": "sensor.smart_topper_left_side_body_sensor_center",
            "topper_ambient": "sensor.smart_topper_left_side_ambient_temperature",
            "room_temp": "sensor.superior_6000s_temperature",
            "run_progress": "sensor.smart_topper_left_side_run_progress",
        }
        for name, entity_id in sensors.items():
            state = api_get(f"/api/states/{entity_id}")
            val = state["state"]
            changed = state["last_changed"][:19]
            is_fresh = val not in ("unavailable", "unknown", "")
            check(f"{name}", is_fresh, f"{val} (updated {changed})")
    except Exception as e:
        check("Topper sensors", False, str(e))

    # 3. Controller (AppDaemon)
    print("\n3. AppDaemon controller:")
    try:
        # Check if bedtime/sleep/wake entities are accessible
        for phase in ["bedtime", "sleep", "wake"]:
            entity_id = f"number.smart_topper_left_side_{phase}_temperature"
            state = api_get(f"/api/states/{entity_id}")
            val = state["state"]
            check(f"{phase} setting readable", val not in ("unavailable", ""), f"{val}")
    except Exception as e:
        check("Controller entities", False, str(e))

    # 4. Sleep stage entity
    print("\n4. Sleep stage pipeline:")
    try:
        state = api_get(f"/api/states/input_text.apple_health_sleep_stage")
        stage = state["state"]
        changed = state["last_changed"][:19]
        check("Sleep stage entity exists", True, f"'{stage}' (changed {changed})")

        # Check staleness
        changed_dt = datetime.fromisoformat(state["last_changed"].replace("Z", "+00:00"))
        age_hours = (datetime.now(timezone.utc) - changed_dt).total_seconds() / 3600
        if age_hours > 24:
            warn("Sleep stage is stale", f"{age_hours:.0f} hours old — iOS app may not be sending")
        elif age_hours > 12:
            warn("Sleep stage somewhat stale", f"{age_hours:.0f} hours old")
    except Exception as e:
        check("Sleep stage entity", False, str(e))

    # 5. Webhook test
    print("\n5. Webhook round-trip:")
    try:
        # POST a test stage
        test_payload = json.dumps({
            "data": {"metrics": [{"name": "sleep_stage", "data": [{"stage": "core"}]}]}
        }).encode()
        url = f"{HA_URL}/api/webhook/apple_health_import"
        req = urllib.request.Request(url, data=test_payload, headers={
            "Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=15) as resp:
            check("Webhook accepts POST", resp.status == 200)

        # Verify it updated
        import time
        time.sleep(2)
        state = api_get(f"/api/states/input_text.apple_health_sleep_stage")
        updated = state["state"] == "core"
        check("Webhook updates entity", updated,
              f"expected='core', got='{state['state']}'")

        # Restore
        restore = json.dumps({
            "data": {"metrics": [{"name": "sleep_stage", "data": [{"stage": stage}]}]}
        }).encode()
        req2 = urllib.request.Request(url, data=restore, headers={
            "Content-Type": "application/json"}, method="POST")
        urllib.request.urlopen(req2, timeout=15)
    except Exception as e:
        check("Webhook", False, str(e))

    # Summary
    passed = sum(1 for _, ok in results if ok)
    failed = sum(1 for _, ok in results if not ok)
    print(f"\n{'=' * 60}")
    if failed == 0:
        print(f"\033[92mALL {passed} CHECKS PASSED — safe to sleep.\033[0m")
    else:
        print(f"\033[91m{failed} FAILED, {passed} passed — DO NOT rely on controller tonight.\033[0m")
        print("\nFailed:")
        for name, ok in results:
            if not ok:
                print(f"  {FAIL} {name}")
    print("=" * 60)
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
