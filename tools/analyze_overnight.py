#!/usr/bin/env python3
"""Analyze overnight data timeline — when did peaks happen?"""
import json

data = json.load(open("docs/overnight_data.json"))

# Key sensors to timeline
sensors = [
    "sensor.smart_topper_left_side_body_sensor_center",
    "sensor.smart_topper_left_side_body_sensor_right",
    "sensor.smart_topper_left_side_heater_head_temperature",
    "sensor.smart_topper_left_side_heater_foot_temperature",
    "sensor.smart_topper_left_side_blower_output",
    "sensor.smart_topper_left_side_pid_control_output",
    "sensor.smart_topper_left_side_temperature_setpoint",
    "sensor.smart_topper_right_side_body_sensor_center",
    "sensor.smart_topper_right_side_heater_foot_temperature",
]

for eid in sensors:
    points = data.get(eid, [])
    if not points:
        continue
    
    # Parse and find peaks
    vals = []
    for ts, v in points:
        try:
            vals.append((ts, float(v)))
        except ValueError:
            pass
    
    if not vals:
        continue
    
    label = eid.split(".")[-1].replace("smart_topper_", "").replace("_side_", " ")
    max_val = max(vals, key=lambda x: x[1])
    min_val = min(vals, key=lambda x: x[1])
    
    print(f"\n{label}:")
    print(f"  Peak: {max_val[1]:.1f}°F at {max_val[0][:19]}")
    print(f"  Low:  {min_val[1]:.1f}°F at {min_val[0][:19]}")
    
    # Show hourly progression
    print(f"  Timeline (sampled):")
    hourly = {}
    for ts, v in vals:
        hour = ts[11:13]
        if hour not in hourly:
            hourly[hour] = []
        hourly[hour].append(v)
    
    for hour in sorted(hourly.keys()):
        avg = sum(hourly[hour]) / len(hourly[hour])
        mx = max(hourly[hour])
        mn = min(hourly[hour])
        print(f"    {hour}:xx  avg={avg:5.1f}  min={mn:5.1f}  max={mx:5.1f}  ({len(hourly[hour])} pts)")
