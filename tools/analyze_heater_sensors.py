"""
Investigate the THH/THF (heater head/foot) sensor encoding.

Known:
- TA, TSR, TSC, TSL use formula: (raw - 32768) / 100 = °C
- THH/THF raw values are typically 36000-37000 (currently 36704, 36810 for left)
- Using the same formula gives ~39°C / 103°F which seems wrong for an empty bed at room temp
- The heater output % reads 0 for both, so heaters are off
- Room temp from TA sensors is ~72°F / 22°C

Hypothesis: THH/THF may use a different encoding, or represent something
other than temperature (e.g., heater element resistance, ADC counts, etc.)
"""
import json
import os
import time
from urllib.request import Request, urlopen

HA_URL = "http://192.168.0.106:8123"
HA_TOKEN = os.environ.get("HA_TOKEN", "")

SENSORS = {
    # Body and ambient sensors (known good encoding)
    "sensor.smart_topper_left_side_ambient_temperature": "L-Ambient (°F)",
    "sensor.smart_topper_left_side_body_sensor_center": "L-Body-C (°F)",
    "sensor.smart_topper_right_side_ambient_temperature": "R-Ambient (°F)",
    "sensor.smart_topper_right_side_body_sensor_center": "R-Body-C (°F)",
    # Heater raw values
    "sensor.smart_topper_left_side_heater_head_raw": "L-THH-Raw",
    "sensor.smart_topper_left_side_heater_foot_raw": "L-THF-Raw",
    "sensor.smart_topper_right_side_heater_head_raw": "R-THH-Raw",
    "sensor.smart_topper_right_side_heater_foot_raw": "R-THF-Raw",
    # Heater output %
    "sensor.smart_topper_left_side_heater_head_output": "L-HH-Out%",
    "sensor.smart_topper_left_side_heater_foot_output": "L-FH-Out%",
    "sensor.smart_topper_right_side_heater_head_output": "R-HH-Out%",
    "sensor.smart_topper_right_side_heater_foot_output": "R-FH-Out%",
    # Blower
    "sensor.smart_topper_left_side_blower_output": "L-Blower%",
    "sensor.smart_topper_right_side_blower_output": "R-Blower%",
}


def get_states():
    req = Request(
        f"{HA_URL}/api/states",
        headers={"Authorization": f"Bearer {HA_TOKEN}"}
    )
    with urlopen(req, timeout=10) as resp:
        states = json.loads(resp.read())
    return {s["entity_id"]: s["state"] for s in states}


def analyze_raw(raw_val):
    """Try multiple decoding hypotheses on a raw THH/THF value."""
    try:
        raw = int(float(raw_val))
    except (ValueError, TypeError):
        return "unavailable"

    results = []
    # Hypothesis 1: Same as body sensors (raw - 32768) / 100
    c1 = (raw - 32768) / 100
    f1 = c1 * 9 / 5 + 32
    results.append(f"BodyFormula: {c1:.1f}°C / {f1:.1f}°F")

    # Hypothesis 2: Raw / 100 directly
    c2 = raw / 100
    f2 = c2 * 9 / 5 + 32
    results.append(f"Raw/100: {c2:.1f}°C / {f2:.1f}°F")

    # Hypothesis 3: 10-bit ADC (0-1023), raw might be scaled
    # If raw = ADC * 36 (rough), then ADC = raw/36 ≈ 1020 which is near max
    results.append(f"Raw/36≈ADC: {raw/36:.0f}")

    # Hypothesis 4: Offset from 32768 but /10 instead of /100
    c4 = (raw - 32768) / 10
    results.append(f"(raw-32768)/10: {c4:.1f}")

    # Hypothesis 5: TWO separate bytes (high byte = integer, low byte = fraction)
    high = raw >> 8
    low = raw & 0xFF
    results.append(f"HiByte={high}, LoByte={low}, Hi.Lo={high}.{low}")

    # Hypothesis 6: raw - 32768 is already in some unit (0.01°C was body, maybe different scale)
    delta = raw - 32768
    results.append(f"Offset={delta} (if /1000={delta/1000:.3f}°C, if /50={delta/50:.1f})")

    return " | ".join(results)


states = get_states()
print("=== CURRENT SENSOR READINGS ===\n")

for eid, label in SENSORS.items():
    val = states.get(eid, "missing")
    extra = ""
    if "raw" in label.lower():
        extra = f"  → {analyze_raw(val)}"
    print(f"  {label:20s}: {val}{extra}")

# Also look at the relationship between raw values and ambient
print("\n=== ANALYSIS ===\n")
for side in ["left", "right"]:
    prefix = side[0].upper()
    thh_raw = states.get(f"sensor.smart_topper_{side}_side_heater_head_raw", "0")
    thf_raw = states.get(f"sensor.smart_topper_{side}_side_heater_foot_raw", "0")
    amb = states.get(f"sensor.smart_topper_{side}_side_ambient_temperature", "0")

    try:
        thh = int(float(thh_raw))
        thf = int(float(thf_raw))
        amb_f = float(amb)
        amb_c = (amb_f - 32) * 5 / 9

        print(f"{prefix} side:")
        print(f"  Ambient: {amb_f}°F ({amb_c:.1f}°C)")
        print(f"  THH raw={thh}, THF raw={thf}")
        print(f"  THH-THF delta: {thh - thf}")
        print(f"  If body formula: THH={((thh-32768)/100)*9/5+32:.1f}°F, THF={((thf-32768)/100)*9/5+32:.1f}°F")
        print(f"  Excess over ambient (body formula): THH={((thh-32768)/100)*9/5+32 - amb_f:.1f}°F, THF={((thf-32768)/100)*9/5+32 - amb_f:.1f}°F")
        print()
    except (ValueError, TypeError):
        print(f"{prefix} side: could not parse values")
