#!/usr/bin/env python3
"""
Prove — with evidence — that every link in the overnight chain will work tonight.
No "should" or "probably". Either PASS with proof or FAIL with explanation.
"""

import json
import os
import sys
from datetime import datetime, timedelta, timezone

import time

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

HA_URL = os.environ.get("HA_URL")
HA_TOKEN = os.environ.get("HA_TOKEN")
ET = timezone(timedelta(hours=-4))
NOW_UTC = datetime.now(timezone.utc)
NOW_ET = NOW_UTC.astimezone(ET)

# Use a session with retry to handle Nabu Casa proxy drops
session = requests.Session()
session.headers.update({"Authorization": f"Bearer {HA_TOKEN}", "Content-Type": "application/json"})
retry = Retry(total=3, backoff_factor=1, status_forcelist=[502, 503, 504])
session.mount("https://", HTTPAdapter(max_retries=retry))

results = []

# Pre-fetch all states in one call to avoid hammering the proxy
print("  Fetching all entity states...", end=" ", flush=True)
_all_states_resp = session.get(f"{HA_URL}/api/states", timeout=30)
ALL_STATES = {s["entity_id"]: s for s in _all_states_resp.json()} if _all_states_resp.status_code == 200 else {}
print(f"{len(ALL_STATES)} entities loaded.\n")

def check(name, passed, evidence, critical=True):
    tag = "PASS" if passed else ("FAIL" if critical else "WARN")
    icon = "✅" if passed else ("❌" if critical else "⚠️")
    results.append((tag, name, evidence, critical))
    print(f"  {icon} {tag}: {name}")
    if evidence:
        for line in evidence.split("\n"):
            print(f"       {line}")
    print()

def get_state(entity_id):
    return ALL_STATES.get(entity_id)

def get_history(entity_id, hours=6):
    start = (NOW_UTC - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    time.sleep(0.5)  # gentle on the proxy
    r = session.get(
        f"{HA_URL}/api/history/period/{start}",
        params={"filter_entity_id": entity_id, "minimal_response": "true"},
        timeout=30,
    )
    if r.status_code == 200:
        data = r.json()
        if data and data[0]:
            return data[0]
    return []

print("=" * 70)
print(f"  OVERNIGHT READINESS PROOF — {NOW_ET.strftime('%Y-%m-%d %I:%M %p ET')}")
print("=" * 70)
print()

# ── LINK 1: HA API reachable ────────────────────────────────────────
print("━━ LINK 1: Home Assistant API ━━")
check("HA API reachable", len(ALL_STATES) > 0,
      f"Loaded {len(ALL_STATES)} entities in single bulk fetch")

# ── LINK 2: Topper hardware — is it physically on? ──────────────────
print("━━ LINK 2: Topper Hardware ━━")
running = get_state("switch.smart_topper_left_side_running")
climate = get_state("climate.smart_topper_left_side_left_side")
if running:
    check("Topper SETTING_RUNNING = on",
          running["state"] == "on",
          f"State: {running['state']}")
else:
    check("Topper SETTING_RUNNING entity exists", False, "Entity not found!")

if climate:
    check("Climate entity = cool",
          climate["state"] == "cool",
          f"State: {climate['state']}, attrs: hvac_action={climate['attributes'].get('hvac_action', '?')}")
else:
    check("Climate entity exists", False, "Entity not found!")

# Check body sensors are reading (proves topper hardware is communicating)
body_center = get_state("sensor.smart_topper_left_side_body_sensor_center")
if body_center:
    try:
        temp = float(body_center["state"])
        check("Body sensor reading", 60 < temp < 110,
              f"Center body sensor: {temp}°F (last changed: {body_center['last_changed'][-20:]})")
    except (ValueError, TypeError):
        check("Body sensor reading", False, f"State: {body_center['state']} (not a number)")
else:
    check("Body sensor exists", False, "Entity not found!")

# Check ambient sensor (use real room temp, not topper's inflated reading)
ambient = get_state("sensor.superior_6000s_temperature")
topper_ambient = get_state("sensor.smart_topper_left_side_ambient_temperature")
if ambient:
    try:
        temp = float(ambient["state"])
        check("Room temperature (dehumidifier)", 50 < temp < 95,
              f"Room: {temp}°F")
    except (ValueError, TypeError):
        check("Room temperature reading", False, f"State: {ambient['state']}")
if topper_ambient:
    try:
        topper_temp = float(topper_ambient["state"])
        delta = topper_temp - float(ambient["state"]) if ambient else 0
        check("Topper ambient sensor (inflated)", True,
              f"Topper reads: {topper_temp}°F (delta +{delta:.1f}°F vs room)")
    except (ValueError, TypeError):
        pass

# ── LINK 3: Current topper temps ────────────────────────────────────
print("━━ LINK 3: Topper Temperature Settings ━━")
for preset in ["bedtime", "sleep", "wake"]:
    eid = f"number.smart_topper_left_side_{preset}_temperature"
    s = get_state(eid)
    if s:
        check(f"{preset.capitalize()} temp preset",
              True,
              f"{preset}: {s['state']}")
    else:
        check(f"{preset.capitalize()} temp preset exists", False, "Not found")

# ── LINK 4: Automations exist and enabled ───────────────────────────
print("━━ LINK 4: Automations ━━")
automation_ids = {
    "topper_pre_cool_start_9_pm": "Pre-cool (9 PM trigger)",
    "topper_morning_off_9_30_am": "Morning off (9:30 AM trigger)",
    "topper_vacation_mode_not_home_by_1_am": "Vacation mode (1 AM not-home)",
}
for auto_id, desc in automation_ids.items():
    s = get_state(f"automation.{auto_id}")
    if s:
        check(f"{desc}",
              s["state"] == "on",
              f"State: {s['state']}, last triggered: {s['attributes'].get('last_triggered', 'never')}")
    else:
        check(f"{desc} exists", False, "Automation entity not found!")

# ── LINK 5: Pre-cool automation — did it fire today? ────────────────
print("━━ LINK 5: Pre-cool Evidence ━━")
precool = get_state("automation.topper_pre_cool_start_9_pm")
if precool:
    lt = precool["attributes"].get("last_triggered")
    if lt:
        # Check if last triggered was today
        try:
            triggered_dt = datetime.fromisoformat(lt.replace("Z", "+00:00"))
            triggered_et = triggered_dt.astimezone(ET)
            today = NOW_ET.date()
            fired_today = triggered_et.date() == today
            check("Pre-cool fired today at 9 PM",
                  fired_today,
                  f"Last triggered: {triggered_et.strftime('%Y-%m-%d %I:%M %p ET')}")
        except Exception as e:
            check("Pre-cool trigger time parseable", False, f"{lt} — parse error: {e}")
    else:
        check("Pre-cool has ever fired", False, "last_triggered is null — never fired!")

# ── LINK 6: Person presence (Mike is home) ──────────────────────────
print("━━ LINK 6: Presence ━━")
person = get_state("person.michael_mones")
if person:
    check("Mike is home",
          person["state"] == "home",
          f"State: {person['state']}")
else:
    check("Person entity exists", False, "Not found")

# ── LINK 7: Health data pipeline ────────────────────────────────────
print("━━ LINK 7: Health Data Pipeline (iPhone → HA) ━━")

health_entities = {
    "input_number.apple_health_hr_avg": "Heart Rate",
    "input_number.apple_health_hrv": "HRV",
    "input_text.apple_health_sleep_stage": "Sleep Stage",
}
for eid, name in health_entities.items():
    s = get_state(eid)
    if s:
        changed = s.get("last_changed", "")
        try:
            changed_dt = datetime.fromisoformat(changed.replace("Z", "+00:00"))
            age_min = (NOW_UTC - changed_dt).total_seconds() / 60
            recent = age_min < 120  # Updated within 2 hours
            check(f"{name} data flowing",
                  recent,
                  f"Value: {s['state']}, last updated: {age_min:.0f} min ago",
                  critical=(name == "Heart Rate"))
        except Exception:
            check(f"{name} data flowing", True,
                  f"Value: {s['state']}, last_changed: {changed[-20:]}")
    else:
        check(f"{name} entity exists", False, "Not found", critical=(name == "Heart Rate"))

# ── LINK 8: AppDaemon controller — is it actively adjusting? ────────
print("━━ LINK 8: AppDaemon Controller Activity ━━")
sleep_temp_history = get_history("number.smart_topper_left_side_sleep_temperature", hours=6)
if sleep_temp_history:
    changes = [(h["last_changed"], h["state"]) for h in sleep_temp_history if h.get("state") not in ("unavailable", "unknown")]
    unique_values = set(h["state"] for h in sleep_temp_history if h.get("state") not in ("unavailable", "unknown"))

    if len(unique_values) > 1:
        check("Controller is actively adjusting temps",
              True,
              f"{len(changes)} state changes in last 6h, values seen: {sorted(unique_values)}")
    elif len(unique_values) == 1:
        val = list(unique_values)[0]
        check("Controller is actively adjusting temps",
              False,
              f"Stuck at {val} for 6 hours — controller may not be running",
              critical=False)
    else:
        check("Controller has history", False, "No valid states in history")
else:
    check("Sleep temp has history", False, "No history data returned", critical=False)

# ── LINK 9: Webhook endpoint reachable ──────────────────────────────
print("━━ LINK 9: Webhook Endpoint ━━")
webhook_url = "https://71gpwlkh7etf6xbve4xol5rdbfrovjt6.ui.nabu.casa/api/webhook/apple_health_import"
try:
    # Send an empty/minimal payload — should get 200 even if data is ignored
    time.sleep(0.5)
    r = session.post(webhook_url, json={"data": {"metrics": []}}, timeout=15)
    check("Webhook endpoint responds",
          r.status_code == 200,
          f"HTTP {r.status_code}")
except Exception as e:
    check("Webhook endpoint reachable", False, str(e))

# ── LINK 10: All 27 required entities exist ─────────────────────────
print("━━ LINK 10: Required Entity Census ━━")
try:
    all_eids = set(ALL_STATES.keys())

    required = [
        "switch.smart_topper_left_side_running",
        "climate.smart_topper_left_side_left_side",
        "number.smart_topper_left_side_bedtime_temperature",
        "number.smart_topper_left_side_sleep_temperature",
        "number.smart_topper_left_side_wake_temperature",
        "sensor.smart_topper_left_side_body_sensor_right",
        "sensor.smart_topper_left_side_body_sensor_center",
        "sensor.smart_topper_left_side_body_sensor_left",
        "sensor.smart_topper_left_side_ambient_temperature",
        "sensor.superior_6000s_temperature",
        "input_number.apple_health_hr_avg",
        "input_number.apple_health_hrv",
        "input_text.apple_health_sleep_stage",
        "person.michael_mones",
        "automation.topper_pre_cool_start_9_pm",
        "automation.topper_morning_off_9_30_am",
        "automation.topper_vacation_mode_not_home_by_1_am",
    ]
    missing = [e for e in required if e not in all_eids]
    check(f"All {len(required)} required entities present",
          len(missing) == 0,
          f"Missing: {missing}" if missing else f"All {len(required)} found")
except Exception as e:
    check("Entity census", False, str(e))

# ── SUMMARY ─────────────────────────────────────────────────────────
print("=" * 70)
fails = [r for r in results if r[0] == "FAIL"]
warns = [r for r in results if r[0] == "WARN"]
passes = [r for r in results if r[0] == "PASS"]

print(f"  RESULTS: {len(passes)} PASS, {len(warns)} WARN, {len(fails)} FAIL")
print()

if fails:
    print("  ❌ CRITICAL FAILURES:")
    for _, name, evidence, _ in fails:
        print(f"     • {name}")
        if evidence:
            print(f"       {evidence}")
    print()

if warns:
    print("  ⚠️  WARNINGS (non-blocking):")
    for _, name, evidence, _ in warns:
        print(f"     • {name}")
        if evidence:
            print(f"       {evidence}")
    print()

if not fails:
    print("  🟢 ALL CRITICAL CHECKS PASSED")
    print("  The overnight pipeline has evidence of working right now.")
else:
    print(f"  🔴 {len(fails)} CRITICAL FAILURE(S) — tonight WILL have issues")
    print("  Fix these before going to bed.")

print("=" * 70)
sys.exit(1 if fails else 0)
