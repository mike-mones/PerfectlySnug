"""
Webhook round-trip test — verifies the full pipeline from POST to entity update.

Tests WITHOUT needing the iPhone or watch. Uses the HA API directly.

Usage:
    export HA_TOKEN="..." HA_URL="https://...nabu.casa"
    python3 tools/test_webhook_roundtrip.py

    # Or with local URL:
    export HA_TOKEN="..." HA_URL="http://192.168.0.106:8123"
    python3 tools/test_webhook_roundtrip.py --local
"""

import json
import os
import sys
import time
import urllib.request
import urllib.error

HA_URL = os.environ.get("HA_URL", "https://71gpwlkh7etf6xbve4xol5rdbfrovjt6.ui.nabu.casa")
HA_TOKEN = os.environ.get("HA_TOKEN", "")
WEBHOOK_ID = "apple_health_import"
STAGE_ENTITY = "input_text.apple_health_sleep_stage"

PASS = "\033[92m✓\033[0m"
FAIL = "\033[91m✗\033[0m"
results = []


def check(name, condition, detail=""):
    results.append((name, condition))
    status = PASS if condition else FAIL
    suffix = f" — {detail}" if detail else ""
    print(f"  {status} {name}{suffix}")


def api_get(path):
    """GET from HA REST API."""
    url = f"{HA_URL}{path}"
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {HA_TOKEN}",
        "Content-Type": "application/json",
    })
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read())


def api_post(path, data):
    """POST to HA REST API."""
    url = f"{HA_URL}{path}"
    payload = json.dumps(data).encode()
    req = urllib.request.Request(url, data=payload, headers={
        "Authorization": f"Bearer {HA_TOKEN}",
        "Content-Type": "application/json",
    }, method="POST")
    with urllib.request.urlopen(req, timeout=15) as resp:
        return resp.status, resp.read()


def webhook_post(data):
    """POST directly to the webhook endpoint (no auth needed)."""
    url = f"{HA_URL}/api/webhook/{WEBHOOK_ID}"
    payload = json.dumps(data).encode()
    req = urllib.request.Request(url, data=payload, headers={
        "Content-Type": "application/json",
    }, method="POST")
    with urllib.request.urlopen(req, timeout=15) as resp:
        return resp.status


def main():
    print("=" * 60)
    print("Webhook Round-Trip Test")
    print("=" * 60)

    if not HA_TOKEN:
        print(f"  {FAIL} HA_TOKEN not set")
        sys.exit(1)

    use_local = "--local" in sys.argv
    local_url = "http://192.168.0.106:8123"

    # --- Test 1: API connectivity ---
    print("\n1. API connectivity:")
    try:
        resp = api_get("/api/")
        check("HA API reachable", resp.get("message") == "API running.")
    except Exception as e:
        check("HA API reachable", False, str(e))
        print("\nCannot reach HA — aborting.")
        sys.exit(1)

    # --- Test 2: Read current sleep stage ---
    print("\n2. Current sleep stage entity:")
    try:
        state = api_get(f"/api/states/{STAGE_ENTITY}")
        old_stage = state["state"]
        old_changed = state["last_changed"]
        check("Entity exists", True, f"current='{old_stage}'")
    except Exception as e:
        check("Entity exists", False, str(e))
        sys.exit(1)

    # --- Test 3: POST to webhook ---
    print("\n3. Webhook POST (sleep stage):")
    # Use a test stage we can detect
    test_stage = "deep" if old_stage != "deep" else "core"
    payload = {
        "data": {
            "metrics": [{
                "name": "sleep_stage",
                "data": [{"stage": test_stage}]
            }]
        }
    }

    # Try local webhook first if requested, otherwise use HA_URL
    webhook_url = local_url if use_local else HA_URL
    try:
        url = f"{webhook_url}/api/webhook/{WEBHOOK_ID}"
        req_payload = json.dumps(payload).encode()
        req = urllib.request.Request(url, data=req_payload, headers={
            "Content-Type": "application/json",
        }, method="POST")
        resp = urllib.request.urlopen(req, timeout=15)
        status = resp.status
        check("Webhook accepts POST", 200 <= status < 300, f"HTTP {status}")
    except urllib.error.URLError as e:
        # If local fails, try via HA_URL
        if use_local:
            check("Webhook accepts POST (local)", False, str(e))
            print("    Trying via Nabu Casa...")
            try:
                status = webhook_post(payload)
                check("Webhook accepts POST (nabu casa)", 200 <= status < 300, f"HTTP {status}")
            except Exception as e2:
                check("Webhook accepts POST (nabu casa)", False, str(e2))
                sys.exit(1)
        else:
            check("Webhook accepts POST", False, str(e))
            sys.exit(1)

    # --- Test 4: Verify entity updated ---
    print("\n4. Verify entity updated:")
    # Wait for automation to process
    time.sleep(2)
    try:
        state = api_get(f"/api/states/{STAGE_ENTITY}")
        new_stage = state["state"]
        new_changed = state["last_changed"]
        stage_matches = new_stage == test_stage
        did_update = new_changed != old_changed
        check("Entity value matches", stage_matches,
              f"expected='{test_stage}', got='{new_stage}'")
        check("Entity timestamp updated", did_update,
              f"was={old_changed[:19]}, now={new_changed[:19]}")
    except Exception as e:
        check("Entity readback", False, str(e))

    # --- Test 5: Restore original stage ---
    print("\n5. Restore original stage:")
    try:
        restore_payload = {
            "data": {
                "metrics": [{
                    "name": "sleep_stage",
                    "data": [{"stage": old_stage if old_stage not in ("", "unknown") else "unknown"}]
                }]
            }
        }
        url = f"{webhook_url}/api/webhook/{WEBHOOK_ID}"
        req = urllib.request.Request(url,
            data=json.dumps(restore_payload).encode(),
            headers={"Content-Type": "application/json"},
            method="POST")
        resp = urllib.request.urlopen(req, timeout=15)
        check("Restored original stage", True, f"→ '{old_stage}'")
    except Exception as e:
        check("Restore", False, str(e))

    # --- Summary ---
    passed = sum(1 for _, ok in results if ok)
    failed = sum(1 for _, ok in results if not ok)
    print(f"\n{'=' * 60}")
    print(f"Results: {passed} passed, {failed} failed")
    if failed:
        print("\nFailed:")
        for name, ok in results:
            if not ok:
                print(f"  {FAIL} {name}")
        sys.exit(1)
    else:
        print("Webhook pipeline is working end-to-end.")
        sys.exit(0)


if __name__ == "__main__":
    main()
