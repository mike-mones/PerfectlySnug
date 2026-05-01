# RC Experiment — Reverse-engineering Responsive Cooling

A ~95-minute controlled experiment to characterize the firmware Responsive
Cooling (RC) behavior on the **right** side of the PerfectlySnug topper.

## Overview

| Test | Duration | What it measures |
|------|----------|------------------|
| 1. Setpoint step | 30 min | First-order plant + dead time of blower vs `bedtime` setting; setpoint dynamics |
| 2. Ice pack on each body sensor | 30 min | Per-sensor weighting (left/center/right) of body input into RC |
| 3. BedJet heat injection | 20 min | Body-sensor sensitivity, blower rise time under external heat |
| 4. Setpoint sweep | 10 min | Static map: `bedtime setting -> steady-state blower / setpoint` |

## Prerequisites

1. **Bed must be empty** for the entire run. Body sensor responses are the
   primary signal; an occupant will dominate everything.
2. **AppDaemon right-side controller paused.** The script asserts this with a
   condition; set `input_boolean.snug_right_controller_enabled = off` (HA UI
   or `homeassistant.toggle`).
3. RC switch will be turned **on** by the script (`switch.smart_topper_right_side_responsive_cooling`).
4. Topper running switch will be turned on if not already.
5. **Ice packs**: 3 small reusable packs ready by the bed before starting.
   You'll get phone notifications telling you exactly which sensor to cover.
6. **BedJet**: place hose so it can blow heated air across the right-side body
   sensors during Test 3.
7. Phone notify target defaults to `mobile_app_mike_mones_iphone_14`. Override
   via the `notify_target` field if needed.

## Files

- `ha-config/packages/rc_experiment.yaml` — the HA package containing the
  script (`script.rc_experiment_run`) and the `shell_command` helpers used
  for CSV logging. Loaded automatically because `configuration.yaml` does
  `packages: !include_dir_named packages`.
- `PerfectlySnug/tools/analyze_rc_experiment.py` — analyzer.
- `PerfectlySnug/tools/RC_EXPERIMENT.md` — this file.

## Run

After the package is deployed and HA reloaded:

```bash
# 1. deploy package (script + shell_command)
scp ha-config/packages/rc_experiment.yaml \
    root@192.168.0.106:/config/packages/rc_experiment.yaml

# 2. validate config & reload (shell_command/script changes need a reload of those domains;
#    package additions of new shell_command keys require a config check at minimum)
ssh root@192.168.0.106 "/config/scripts/reload_automations.sh --check-only"

# A new shell_command requires a full HA restart to pick up. After first deploy:
ha core restart
# (or use the HA UI: Developer Tools -> YAML -> "Check configuration" then restart)

# 3. set right-side controller off
#    Developer Tools -> States -> input_boolean.snug_right_controller_enabled -> off
#    or:
curl -X POST -H "Authorization: Bearer $HA_TOKEN" \
     -H "Content-Type: application/json" \
     -d '{"entity_id":"input_boolean.snug_right_controller_enabled"}' \
     http://192.168.0.106:8123/api/services/input_boolean/turn_off

# 4. Start the script (Developer Tools -> Services -> script.rc_experiment_run)
#    or:
curl -X POST -H "Authorization: Bearer $HA_TOKEN" \
     -H "Content-Type: application/json" \
     http://192.168.0.106:8123/api/services/script/rc_experiment_run
```

The script will:
- send a starting notification,
- step the bedtime number through the protocol while writing every state
  change to `/config/rc_experiment_log.csv`,
- send phone prompts when you need to place/remove ice packs,
- drive the BedJet for Test 3,
- send a completion notification.

Total runtime: **~95 minutes**. You only need to be present during Test 2
(~30 min in) and to position the BedJet for Test 3 (~60 min in).

## Analyze

```bash
# Pull the CSV from HA
scp root@192.168.0.106:/config/rc_experiment_log.csv .
scp root@192.168.0.106:/config/rc_experiment_summary.txt .

# Run analyzer (needs numpy, scipy)
pip install numpy scipy
export HA_TOKEN="$(ssh root@192.168.0.106 'cat /config/.ha_token')"
python3 PerfectlySnug/tools/analyze_rc_experiment.py \
    --ha-url http://192.168.0.106:8123 \
    --csv ./rc_experiment_log.csv \
    --out rc_experiment_report.md
```

The analyzer reads the CSV to delimit each test, fetches recorder history
from HA for all relevant entities over each window, then fits:

- **Test 1**: first-order-plus-dead-time (`K`, `tau`, `td`) for the blower
  step from `0 → -10`, the matching setpoint dynamic, and the decay back to
  `0`.
- **Test 2**: per-sensor sensitivity `Δblower / Δbody_F`, plus a normalized
  weight vector `{left, center, right}` indicating how RC averages them.
- **Test 3**: blower delta and rise time under BedJet heat, body-sensor
  responsiveness of `Δblower / Δbody_F` in the heating direction.
- **Test 4**: linear regression of steady-state blower vs setting, and of
  setpoint vs setting (slope `°F/unit` should match the ~2.8 °F/unit rule).

Output: `rc_experiment_report.md` with parameters and 95% CIs (when scipy is
available).

## Safety & abort

- Right side never heats during this experiment (settings stay ≤ 0).
- The `right_overheat_safety` AppDaemon app is **left running**. If it ever
  trips, it will drive the right side to neutral; the experiment will then
  show no useful blower response from that point on. Inspect the
  AppDaemon log if test results look flat.
- To abort mid-run: in HA UI, Developer Tools → Services →
  `script.turn_off` with `entity_id: script.rc_experiment_run`. Then manually
  set `number.smart_topper_right_side_bedtime_temperature` back to your usual
  value and turn off the BedJet if it was running.
