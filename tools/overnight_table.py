"""Pull full overnight sensor data and print a table."""
import json, os
from urllib.request import Request, urlopen

HA_URL = 'http://192.168.0.106:8123'
TOKEN = os.environ.get('HA_TOKEN', '')
HEADERS = {'Authorization': f'Bearer {TOKEN}', 'Content-Type': 'application/json'}

start = '2026-04-02T22:00:00'
end = '2026-04-03T09:00:00'

entities = {
    'L_bR':  'sensor.smart_topper_left_side_body_sensor_right',
    'L_bC':  'sensor.smart_topper_left_side_body_sensor_center',
    'L_bL':  'sensor.smart_topper_left_side_body_sensor_left',
    'L_amb': 'sensor.smart_topper_left_side_ambient_temperature',
    'R_bL':  'sensor.smart_topper_right_side_body_sensor_left',
    'R_bC':  'sensor.smart_topper_right_side_body_sensor_center',
    'R_bR':  'sensor.smart_topper_right_side_body_sensor_right',
    'R_amb': 'sensor.smart_topper_right_side_ambient_temperature',
    'L_set': 'number.smart_topper_left_side_sleep_temperature',
    'prog':  'sensor.smart_topper_left_side_run_progress',
}

raw = {}
for label, eid in entities.items():
    url = f'{HA_URL}/api/history/period/{start}?end_time={end}&filter_entity_id={eid}&minimal_response'
    data = json.loads(urlopen(Request(url, headers=HEADERS), timeout=15).read())
    entries = []
    if data and data[0]:
        for entry in data[0]:
            ts = entry.get('last_changed', entry.get('last_updated', ''))
            state = entry.get('state', '')
            if ts and state not in ('unknown', 'unavailable', ''):
                h, m = int(ts[11:13]), int(ts[14:16])
                h_edt = (h - 4) % 24
                day_offset = 24 * 60 if h_edt < 12 else 0
                mins = h_edt * 60 + m + day_offset
                try:
                    entries.append((mins, float(state)))
                except ValueError:
                    entries.append((mins, state))
    raw[label] = sorted(entries, key=lambda x: x[0])

# 15-min slots from 10pm to 9am
slots = []
for h in range(22, 24):
    for q in range(4):
        slots.append(h * 60 + q * 15)
for h in range(0, 9):
    for q in range(4):
        slots.append((h + 24) * 60 + q * 15)
slots.append(33 * 60)

def nearest(entries, target, window=10):
    best_val, best_diff = None, 999
    for mins, val in entries:
        diff = abs(mins - target)
        if diff < best_diff:
            best_diff = diff
            best_val = val
    return best_val if best_diff <= window else None

cols = ['L_bR', 'L_bC', 'L_bL', 'L_amb', 'R_bL', 'R_bC', 'R_bR', 'R_amb', 'L_set', 'prog']

# Header
print(f"  Time   ─── YOUR SIDE (Left) ───  ─── PARTNER (Right) ──  Set  Prog")
print(f"         bdy_R  bdy_C  bdy_L  ambi  bdy_L  bdy_C  bdy_R  ambi")
print("─" * 75)

for slot in slots:
    h = (slot // 60) % 24
    m = slot % 60
    row = f" {h:02d}:{m:02d}"
    for c in cols:
        v = nearest(raw[c], slot)
        if v is None:
            row += f"  {'--':>5s}"
        elif isinstance(v, float):
            if c == 'L_set':
                row += f"  {v:>+5.0f}"
            elif c == 'prog':
                row += f"  {v:>5.0f}"
            else:
                row += f"  {v:>5.1f}"
        else:
            row += f"  {str(v):>5s}"
    print(row)
