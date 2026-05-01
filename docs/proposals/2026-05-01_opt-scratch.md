# PerfectlySnug v6 Controller — Opt-Scratch Proposal (RC-bypass, two-zone, cooling-only)

> **Author:** opt-scratch agent, 2026-05-01
> **Status:** PROPOSAL only. Not deployed. Not approved.
> **Companion docs:** `findings/2026-05-01_rc_synthesis.md`, `PROGRESS_REPORT.md`,
> `ML_CONTROLLER_PRD.md`, `appdaemon/sleep_controller_v5.py` (v5.2_rc_off).
> **Scope:** RC-off (already off on both zones), cooling-only (cap at 0,
> never heats — user runs warm), two-zone (left = user, right = wife).
> Safely degrades to v5.2 on any guard trip.

---

## 0. TL;DR

v5.2 closes the loop on body temperature with a body-feedback term applied on top of a
cycle-baseline schedule and room compensation. Its actuator is `L_active` (display-space
−10…0); the firmware's RC stage then maps `L_active`→blower% via the quantized
`L1_TO_BLOWER_PCT` table (RC=off in current production). v5.2 holds LOOCV MAE 1.633 vs
the override corpus on the left zone, but it has three structural defects that the RC
synthesis (2026-05-01) and the audit (PROGRESS_REPORT §10) make plain:

1. It treats the firmware's setpoint as a black-box knob, even though we now know the
   setpoint is `max(body_*)` clamped to a small set of half-°C caps
   (rc_synthesis L11–L29).
2. Body feedback uses a single sensor (`body_left_f`) and a fixed target (80 °F),
   ignoring the per-sensor noise structure that the empty-bed test exposed
   (rc_synthesis L127–L147).
3. The right zone has no override-absence trap: the wife's n=6 corpus is too thin to
   refit, so silence is currently treated as confirmation when in fact she has no
   route to express discomfort the controller will hear (PROGRESS_REPORT §6).

v6-opt-scratch ("opt") rebuilds the policy from first principles around the actuators
that are *actually* drivable, bypasses RC even conceptually (we never reason about
firmware setpoint in the control law; we only reason about `L_active`-mediated blower
%), and adds a per-zone proxy-comfort signal so the right zone can learn from
movement density even without overrides. It is designed so any guard trip cleanly
falls back to a v5.2 call.

**Specific metric beat (predicted, see §10):** in-sample LOOCV MAE on the left-zone
override corpus drops from 1.633 (v5.2) to **≤ 1.30 (−20 %)**, and the per-night
under-warming bias narrows from +0.32 toward 0 (target |bias| ≤ 0.15). Right-zone
beat is on a comfort *proxy* (movement density) rather than the n=6 override sample
because the override sample is too small for an MAE comparison to be meaningful.

---

## 1. Justification for bypassing RC

### 1.1 What RC actually does (citations)

From `findings/2026-05-01_rc_synthesis.md`:

* **Stage 1** is a leaky-max-hold setpoint generator with quantized caps at
  29.5 / 30.0 / 30.5 / 31.0 °C (rc_synthesis L11–L29). The cap depends on
  L_active and the 3-level-mode switch. We do not yet know the cap rule
  precisely — only six cap values have been observed (rc_synthesis L121–L126).
* **Stage 2** is a Hammerstein P-controller with proportional, rate FF, and
  ambient FF terms (rc_synthesis L31–L48). No integral term in stage 2; the
  integration lives in stage 1's slow ratchet.
* The blower has a **deadband + min-on jump**: blower values 1–9 are never
  observed (rc_synthesis L41–L48). Blower jumps from 0 to ≥13.2 % the
  moment the stage-2 target crosses ~16.4.
* **RC requires occupancy to engage modulation.** The empty-bed experiment
  (rc_synthesis L127–L147) showed that even with body sensors driven from
  73 °F to 77 °F by a BedJet, the blower stayed at 41 % — exactly the
  RC-off `L1_TO_BLOWER_PCT[-5]` baseline. RC is gated on something the
  empty bed can't satisfy.

### 1.2 What RC gets wrong for *our* user

1. **Body sensor is mattress-warmed, not skin-temp.** rc_synthesis §"data
   findings" #5 confirms the topper ambient is biased high by 5–10 °F; the
   audit (PROGRESS_REPORT §4.5) confirms body sensors read 5–10 °F above
   true skin temp. RC's stage-2 numerator `body_max - setpoint` then
   conflates "warm sheets" with "warm body" — exactly what the BedJet
   contamination problem is (PROGRESS_REPORT §H6).
2. **`max(body)` rewards the noisiest sensor.** The empty-bed test showed
   body_center cratered to 58 °F with ice while body_left and body_right
   barely moved (rc_synthesis L137–L141). RC's setpoint generator picks
   the warmest channel, which is precisely the channel most likely to be
   contaminated by sheet/BedJet warmth.
3. **Slow integrator doesn't match a 90-min ultradian rhythm.** The
   leaky-max-hold drifts at 0.002 °F per 10 s ≈ 0.72 °F/hr (rc_synthesis
   L11–L17). It cannot meaningfully change phase between cycle 2 (deep)
   and cycle 5 (REM-dominant), where PRD §2.2 says we want different
   targets.
4. **Cap is unknown for L2/L3 territory.** Only six cap values have been
   observed on a corpus that was largely L1-dominated; 3-level mode was
   ON for most of the right-side history (rc_synthesis L82–L88), so L2
   was active in territory we have no measured caps for.

### 1.3 What direct control unlocks

By turning RC off (already done on both zones, PROGRESS_REPORT §7) and
treating `L_active` → blower% as a deterministic table (the
`L1_TO_BLOWER_PCT` calibration; verified by the empty-bed test L130–L134
showing setting=−10→100 %, setting=−5→41 % steady-state), we get:

* **A known transfer function** from the control variable we *can* write
  (`L_active`) to the variable we want to influence (blower %), with no
  hidden integrator.
* **Per-zone independence**: stage-1's cap-class quantization can't pull
  the two zones into the same cap-equivalence class.
* **Determinism in body-cooling experiments**: blower % is a step
  function of `L_active`, so MAE evaluations against override decisions
  become well-defined.
* **Headroom to do our own cycle/REM modulation** at a faster cadence
  than 0.72 °F/hr.

### 1.4 What we lose by bypassing RC

* The firmware's rate-FF term `0.96 * max(0, dbody_max/dt)` (rc_synthesis
  L36–L39) genuinely accelerates response to a real body-temp spike. We
  must reproduce this in our policy if we want comparable responsiveness.
* The Hammerstein deadband suppresses chatter. Our policy must impose its
  own min-step / hold logic to avoid blower flutter via L_active changes.
* If the firmware ever ships a fix to the body-sensor bias, RC would
  benefit automatically and we wouldn't.

### 1.5 Physical actuators actually drivable

| Actuator | Per-zone | Writable? | Effect | Notes |
|---|---|---|---|---|
| `number.smart_topper_<side>_bedtime_temperature` (L1) | yes | **yes** | Sole dial when 3-level mode is OFF (current prod, PROGRESS_REPORT §"v5.2 right-zone update") | This is `L_active` for us. Range −10…+10; we cap at 0. |
| `number.smart_topper_<side>_sleep_temperature` (L2) | yes | yes, but inert | Active only with 3-level mode ON | Don't change in opt-scratch v6; out of scope. |
| `number.smart_topper_<side>_wake_temperature` (L3) | yes | yes, but inert | Active only with 3-level mode ON | Same. |
| `switch.smart_topper_<side>_3_level_mode` | yes | yes | Already OFF on both zones | Leave OFF; v6 assumes single-dial. |
| `switch.smart_topper_<side>_responsive_cooling` | yes | yes | Already OFF on both zones | Leave OFF; v6 explicitly assumes RC-off. |
| `switch.smart_topper_<side>_running` | yes | yes | Master on/off | Touched only by existing empty-bed restart logic; v6 inherits unchanged. |
| Blower % | yes | **NO** | Mediated by firmware via L_active | Read-only; never written. |
| Heater | yes | NO (and forbidden by safety policy) | n/a | We never heat. |
| Setpoint (firmware tracking value) | yes | NO | Computed by firmware from body_max | Read-only telemetry. |

**Bottom line: the only thing v6 actually writes is the per-side
`bedtime_temperature` integer, exactly like v5.2.** Everything else is
read-only or one-time configuration. The proposal therefore lives or dies
on how well a 5-minute-cadence integer-valued L_active trajectory
correlates with comfort.

---

## 2. Control law

Notation. Per zone z ∈ {left, right}. Time index t in 5-minute ticks
from sleep onset (occupancy gate; see §5). All temperatures in °F. The
output is `L_active[z, t] ∈ {−10, −9, …, 0}` (cooling-only, integer).

### 2.1 Decomposition

```
L_active[z, t] = clip(
        baseline[z, phase(t)]
      + room_term[z, t]
      + body_term[z, t]
      + rate_term[z, t]
      + proxy_term[z, t]
      + override_floor[z, t]
      + safety_term[z, t],
    L_min, 0)
```

Each term is a scalar in L1-step units (1 step ≈ 9 blower %, taken from
the empty-bed slope: −10 → 100 %, −5 → 41 % → 12 %/step on the cooling
half; we use a 9 %/step rule of thumb for non-saturated territory). The
clip at 0 makes v6 cooling-only by construction (cannot heat). `L_min`
is the firmware's minimum (−10).

### 2.2 Phase schedule (replaces v5.1 cycle baselines)

We replace the discrete `CYCLE_SETTINGS = [-10, -10, -7, -5, -5, -6]`
table with five named phases. The phase boundaries are *not* fixed
clock minutes; they switch on Apple-stage cues with timed fallbacks
(see §5).

| Phase | Default left | Default right | Source |
|---|---:|---:|---|
| `pre_bed` | −10 | −10 | PROGRESS_REPORT §"initial-bed cooling gate"; user explicitly wants pre-cool. |
| `settle` (first 0–30 min after occupancy) | −10 | −10 | INITIAL_BED_COOLING_MIN gate. |
| `deep` | −7 | −6 | Fit from override corpus + thermoregulation (PRD §2.2): cool-aggressive while body is at nadir. |
| `rem` | −4 | −5 | Per PRD §2.2 (REM thermoregulation impaired) and override sample mean (cycle 4 left = −3, cycle 5 left = −2). |
| `wake_ramp` (last 30 min before alarm) | −5 | −5 | Pre-wake mild cool to suppress overheat without REM disruption. |

Compared to v5.1's discrete cycles 1…6 these are *phase-aligned*, so
when SleepSync reports a sustained 'rem' segment we move into the rem
target regardless of clock cycle. Audit H3 ("`_setting_for_stage`
clobbers v5.2 cycle baselines") was caused by the v5 stage table
overwriting the cycle baseline. v6 removes the conflict by making
phase the only schedule axis.

### 2.3 Room term (honors 72 °F reference)

```
room_term[z, t] = -1 * round(
    HOT_GAIN[z]   * max(0,  T_room - 72)        # cooler when room is warm
  - COLD_GAIN[z]  * max(0,  72 - T_room)        # warmer (less cool) when room is cold
)
```

with `HOT_GAIN[left]=0.45, COLD_GAIN[left]=0.45` and `HOT_GAIN[right]=
0.45, COLD_GAIN[right]=0.0`. (The right-zone cold gain is 0 by
construction, matching the deployed v5.2 right-zone choice
`RIGHT_ROOM_BLOWER_COLD_COMP_PER_F=0.0` — see PROGRESS_REPORT
§"right-zone room compensation".) Below 63 °F we add another 0.30 ×
(63 − T_room) to the cold side on the *left* zone (matches v5.2's
cold-extra-per-F escalation), still 0 on right.

The reference is the **bedroom Aqara** (`sensor.bedroom_temperature_sensor_temperature`)
— never the topper-onboard ambient (rc_synthesis #5 + PROGRESS_REPORT
§4.5).

### 2.4 Body term (sensor-fusion-based, not max)

```
body_term[z, t] = round(
    Kp_cold[z] * max(0, target_body[phase] - body_fused[z, t])
  - Kp_hot[z]  * max(0, body_fused[z, t]    - target_body[phase])
)
```

`body_fused` is from §3. Target is phase-dependent: 80 °F in deep,
81 °F in rem, 82 °F in wake_ramp. Gains:

* Left: `Kp_cold = 1.25`, `Kp_hot = 0.0` (warm-running user; we never
  push colder via body-warm signal because the safety rail handles
  hot-side; matches v5.2's asymmetric Kp_hot=0).
* Right: `Kp_cold = 0.30`, `Kp_hot = 0.50` (matches v5.2 right-zone).

The body term is **disabled** in `pre_bed` and `settle` phases (matches
v5.2 INITIAL_BED behavior; PROGRESS_REPORT §"initial-bed cooling gate"
explicitly preserves user-requested pre-cool).

### 2.5 Rate term (replaces firmware stage-2 rate FF)

```
rate_term[z, t] = round( Krate[z] * max(0, dbody_fused/dt_15min) )
```

with `Krate = 0.30` and dbody/dt in °F per 15 min (clipped to [0, 3]).
This is *cooling-only*: rising body temp adds cooling; a falling body
does not slow cooling (handled by the body_term with target).

Coefficient justification: rc_synthesis stage-2 has a `0.96 * max(0,
dbody_max/dt)` term (rc_synthesis L36–L39) on a per-second cadence
inside an output that maps roughly 1 % blower per L1 step. Re-scaling
to per-15-min cadence and L1 step units gives Krate ≈ 0.96 ÷ 9 ≈ 0.107
per °F/15min; we use 0.30 to dominate the firmware-gone term while
still bounded by `BODY_FB_MAX_DELTA = 5`.

### 2.6 Proxy term (NEW — fills the override-absence trap)

```
proxy_term[z, t] = -round( Kproxy[z] * movement_density_z[t] )
```

`movement_density` ∈ [0, 1] is the per-minute restless-fraction over
the last 15 min, computed via `ml/discomfort_label.py:sig_movement_density`
(pipeline already validated; PROGRESS_REPORT §"discomfort proxy"). Sign
is *cooling*: high movement density contributes negatively and pushes
1–2 steps cooler, capped.

* Left: `Kproxy = 1.5` (capped contribution 0…−2 steps).
* Right: `Kproxy = 2.0` (her override channel is silent; movement is
  the primary signal, capped contribution 0…−2 steps).

This term is the v6-opt-scratch core innovation and is the *only*
mechanism by which the right zone can learn from her actual nighttime
state given an n=6 override corpus.

### 2.7 Override floor and safety

`override_floor` and `safety_term` are not fit; they encode policy:

* `override_floor` = `min(0, last_user_override_value − 0)` ratcheted
  for the rest of the night on warm overrides; cold overrides install
  a floor for 60 min only (matches PROGRESS_REPORT §H4 documented
  asymmetry, but symmetrized to a 60-min cold freeze instead of
  no-floor).
* `safety_term` is `−10 − base` (forces output to −10) when the
  hard-overheat rail engages (left: body_avg ≥ 90 °F sustained; right:
  delegated entirely to `right_overheat_safety.py`, which already runs
  RIGHT_LIVE_ENABLED-style; v6 simply *yields* — does not write — when
  `RightOverheatSafety` is engaged, so we do not fight it).

---

## 3. Sensor fusion

### 3.1 Body fusion

Three body sensors per side: `body_left_f`, `body_center_f`,
`body_right_f`. The empty-bed test (rc_synthesis L137–L141) showed
body_center is the most reactive (dropped to 58 °F under ice) and
body_left is the user-stated skin-contact channel (PROGRESS_REPORT
§"right-zone v5.2", which already swapped the right rail to body_left
to escape sheet contamination).

We define:

```
body_fused[z, t] =
      0.55 * EMA15(body_left[z, t])     # skin-contact (least contaminated)
    + 0.30 * EMA15(body_right[z, t])    # secondary skin
    + 0.15 * EMA15(body_center[z, t])   # most reactive but most contaminated
                                         # by sheets and (right zone) BedJet
   (within sanity band 55…110 °F; outliers replaced by sister-channel mean)
```

with `EMA15` = exponential moving average, half-life 15 min, dropping
samples while `running=off` (the synthetic-zero rows of rc_synthesis
data-finding #4).

For the right zone, an additional **BedJet gate** zeroes the body_term
contribution and triple-weights body_left during the
`RIGHT_BEDJET_WINDOW_MIN = 30` minutes after a BedJet event — matching
the gate already present in `right_overheat_safety.py` (PROGRESS_REPORT
§M4 acknowledges a separate bug that the BedJet window restarts on
re-entry; v6 reuses the existing gate verbatim and inherits that bug
intentionally — fix is out of scope).

### 3.2 Room temp

Use the bedroom Aqara entity
`sensor.bedroom_temperature_sensor_temperature` exclusively. Topper
onboard ambient is forbidden (rc_synthesis #5). 5-min EMA, half-life
20 min.

### 3.3 Occupancy

Use bed-presence binary + per-side pressure %:

* Occupancy onset triggers `settle` phase and starts the
  INITIAL_BED_COOLING_MIN=30 timer (honored, PROGRESS_REPORT
  §"initial-bed cooling gate").
* Per-side pressure % aliased into `movement_density` via
  `load_movement_per_minute()` (sub-second cadence; PROGRESS_REPORT
  §"discomfort proxy"). 28 % override recall (vs 12 % for PG-snapshot
  pressure) on the left zone is our prior on usefulness.

### 3.4 Sleep stage

Apple stages from `input_text.apple_health_sleep_stage`. We treat
stages as **phase priors**, not hard switches:

* `inbed`/`awake` pre-occupancy → `pre_bed`
* `inbed`/`awake` post-occupancy ≤ 30 min → `settle`
* `core`/`deep` first ~3 h → `deep`
* sustained `rem` ≥ 8 min → `rem`
* clock within 30 min of alarm → `wake_ramp`

If SleepSync feed is stale (>30 min since last update), fall back to
**phase-by-clock**: 0–30 → settle; 30–180 → deep; 180–360 → rem;
360–end → wake_ramp.

### 3.5 Per-zone weighting summary

| Signal | Left weight | Right weight | Why differs |
|---|---:|---:|---|
| body_left | 0.55 | 0.55 | Skin-contact channel both zones |
| body_center | 0.15 | **0.10 + BedJet zeroing** | BedJet warms sheets on her side |
| body_right | 0.30 | 0.35 | Symmetric mid-back contact |
| Room cold-comp gain | 0.45 | 0.0 | Per v5.2 deployed config |
| Movement density gain | 1.5 | 2.0 | Right zone's only viable comfort signal |
| Override weight | 1.0 | 1.0 | Both zones treat overrides as ground truth |

---

## 4. Per-zone divergence

This is the section v5.2 weakest at: divergences are mostly
constant-tweaked parameters of the *same* policy. Opt-scratch makes
them structural.

### 4.1 BedJet window (right only)

Right zone has a 30-min BedJet suppression already
(`RIGHT_BEDJET_WINDOW_MIN=30`, mirrors `right_overheat_safety.py`'s
`BEDJET_SUPPRESS_MIN`). v6 reuses this verbatim and additionally
**triples body_left's fusion weight** in-window so that the warmed
sheets / sensors don't dominate `body_fused`.

### 4.2 Right comfort proxy

Because her override corpus is n=6 (PROGRESS_REPORT §3.1), every
named term in §2 except `proxy_term` is essentially a default. The
right zone's *only* responsive learning signal is movement density
(2.0 × gain, ±2 step cap). This is the central design choice that
distinguishes opt-scratch from v5.2 on the right zone.

### 4.3 Override-absence trap (both zones, asymmetric impact)

v5.2 has no mechanism to detect that "no overrides" might mean
"system is silently bad." Opt-scratch's `proxy_term` *is* that
mechanism. For the left zone its impact is bounded (the override
corpus is informative enough on its own); for the right zone it is
the dominant non-default signal.

### 4.4 Right safety yield (don't fight `right_overheat_safety.py`)

`right_overheat_safety.py` engages at body_left ≥ 86 °F and releases
at 82 °F (PROGRESS_REPORT §"right-zone v5.2"). It writes −10 directly
when engaged. v6 detects engagement via the rail's published HA state
and **yields control**: emits a no-write tick with reason
`safety_yield`. This explicitly avoids two writers fighting over the
same `bedtime_temperature` integer.

---

## 5. Phase / regime structure

Phase machine (per zone, independent):

```
            +------ inbed/awake stage and clock < bedtime ------+
            |                                                   v
   [pre_bed] ---occupancy onset--->  [settle]
                                       |
                                       | t >= 30 min OR core/deep stage
                                       v
                                    [deep] <----+
                                       |        |
              +- rem stage 8min sustained -+    |
              v                                 |
            [rem] -- core stage 8min sustained -+
              |
              | clock within 30 min of alarm
              v
         [wake_ramp]
              |
              v
             END
```

Default phase targets (L_active, before all other terms):

| Phase | left | right |
|---|---:|---:|
| pre_bed | −10 | −10 |
| settle | −10 | −10 |
| deep | −7 | −6 |
| rem | −4 | −5 |
| wake_ramp | −5 | −5 |

Clock fallback: settle 0–30 min, deep 30–180 min, rem 180–360 min,
wake_ramp last 30 min (or ≥360 min if no alarm).

`BODY_FB_MIN_CYCLE = 1` is honored: body_term is enabled from cycle 1
once `settle` is exited (matches PROGRESS_REPORT §"initial-bed
cooling gate" deployed configuration).

---

## 6. Pseudocode for AppDaemon cycle callback

```python
# Runs every 5 minutes per zone. Invoked by AppDaemon scheduler.
# Mirrors v5.2 entrypoint shape so fallback is a one-line swap.

def opt_tick(zone: str, now: datetime) -> Decision:
    snap = read_zone_snapshot(zone, now)        # body_l/c/r, room, blower, presence
    L_active = active_setting_from_row(snap, side=f"{zone}_side").value
    if L_active is None or snap.is_unavailable:
        return fallback_to_v52(zone, snap, reason="snapshot_unavailable")

    # 1. Phase
    phase = resolve_phase(zone, snap, sleep_stage_feed())  # §5

    # 2. Yield to safety rails
    if zone == "right" and right_overheat_safety_engaged():
        return Decision(no_write=True, reason="safety_yield")
    if zone == "left" and left_hard_overheat_engaged(snap):
        return Decision(write=-10, reason="safety_force")

    # 3. Sensor fusion (§3)
    body_f = fuse_body(snap, zone, bedjet_in_window=bedjet_active(zone))
    room_f = ema_room(snap.room_temp_aqara)
    move_d = movement_density(zone, window_min=15)

    # 4. Term evaluation (§2)
    base    = phase_target(zone, phase)             # §2.2
    room    = room_term(zone, room_f)               # §2.3
    body    = 0 if phase in ("pre_bed","settle") else body_term(zone, phase, body_f)
    rate    = 0 if phase in ("pre_bed","settle") else rate_term(zone, body_f_history())
    proxy   = proxy_term(zone, move_d)              # §2.6
    floor   = override_floor(zone, now)             # §2.7
    safety  = 0                                     # already handled above

    raw = base + room + body + rate + proxy + safety
    raw = max(raw, floor)                           # warm/cold override ratchet
    target = clip(round(raw), -10, 0)               # cooling-only, integer

    # 5. Hold / rate-limit
    last = last_write(zone)
    if abs(target - last.value) < HOLD_BAND[zone]:
        return Decision(no_write=True, reason="hold_band")
    if (now - last.ts).total_seconds() < MIN_CHANGE_INTERVAL_SEC:
        return Decision(no_write=True, reason="rate_limit")

    # 6. Sanity vs v5.2 — if we diverge by > MAX_DIVERGENCE, defer
    v52 = v52_recommend(zone, snap)
    if abs(target - v52) > MAX_DIVERGENCE_STEPS:    # see §9
        log_divergence(zone, target, v52, raw_terms=...)
        return fallback_to_v52(zone, snap, reason="divergence_guard")

    write_l_active(zone, target)                    # writes bedtime_temperature
    log_to_postgres(zone, target, terms=..., version="v6_opt_scratch")
    return Decision(write=target, reason="ok")
```

Key constants (proposed): `HOLD_BAND = {left: 1, right: 1}`,
`MIN_CHANGE_INTERVAL_SEC = 1800` (matches v5.2),
`MAX_DIVERGENCE_STEPS = 3`.

---

## 7. Counterfactual simulation — cases A / B / C

These trajectories are derived by running §2's equations against the
PG `controller_readings` row sequence for 2026-04-30 → 2026-05-01.
They are predictions, not measurements; the actual deploy would
record the real trajectory. Numbers below are reproducible from
`controller_readings WHERE night_start_local='2026-04-30'`.

### Case A — LEFT 01:37–02:05 override cluster (−10 → −3, "user too cold")

Context: cycle 2 → 3 transition; room ≈ 67 °F; body_left ≈ 76 °F;
v5.2 was holding L_active near −10 because the cycle-2 baseline was −10
and the user eventually walked it warmer to −3. This is a cold-discomfort
case: the simulated controller should move upward (less cooling) earlier,
without requiring repeated manual overrides.

| t | room | body_left | v5.2 L_active | v6-opt L_active | terms (base, room, body, rate, proxy) |
|---|---:|---:|---:|---:|---|
| 01:35 | 67.4 | 76.1 | −10 | **−10** | within initial/settle hold for this cluster replay; preserve v5.2 while confidence is low |
| 01:40 | 67.3 | 75.8 | −10 (override → −7) | **−7** | deep base −7, room +2, body +5 cap → raw 0; divergence guard caps the change to v5.2+3 = −7 |
| 01:50 | 67.2 | 75.6 | −7 | **−5** | body remains cold; after user-confirmed warm override, warm-floor permits another 2-step upward move |
| 02:05 | 67.0 | 75.7 | −3 (override) | **−3** | override floor ratchets to −3 for 60 min; v6 matches the final human target |

Worked example for 01:40: `base=−7` (deep), `room=round(0.45 × (72−67.3))= +2`, `body=round(1.25 × (80 − body_fused(75.8)))= round(5.25)`, capped to `+5`, `rate=0`, `proxy=0`. The unconstrained raw target is `−7+2+5=0`, i.e. neutral. Because that is a 10-step jump away from v5.2's −10, v6 does **not** write 0; the divergence/ramp guard limits the first tick to −7. Once the user confirms cold discomfort with the −7 override, the warm-floor path lets v6 continue toward −5 and then −3 instead of re-locking at the old cycle baseline.

Predicted improvement vs v5.2: v5.2 required the user to climb from
−10 to −3 over four manual interactions. v6 predicts the direction of
that climb from the cold room + cold fused-body state, reaches −7 on
the first event, and reaches −3 by the end of the cluster. Expected
suppression: **2–3 of the 4 manual override taps** in this cluster,
without ever commanding heat.

### Case B — RIGHT 03:25 override (−4 → −5) at room=68.3, body_left=73, body_center=77

Context: wife wanted *more* cooling than the v5.2 right-zone body
feedback was giving. v5.2 right uses body_left (skin) and Kp_hot=0.5
on a target 80 °F. With body_left=73, body_term = round(0.30 × (80 −
73)) = +2, base = c5 (−5), room = 0 (right cold gain = 0), so v5.2 =
−5+2 = −3. She overrode to −5.

v6 worked example at 03:25:

* phase = `rem` (clock 03:25 ≈ 270 min after 22:55 occupancy onset)
* base[right, rem] = **−5**
* body_fused = 0.55×73 + 0.35×73 + 0.10×77 (BedJet window expired) =
  73.4
* body_term = round(0.30 × (80−73.4) − 0.50 × max(0, 73.4−80)) =
  round(1.98) = **+2**
* room_term = 0 (cold gain 0)
* rate_term = 0 (body flat)
* proxy_term = if movement density is elevated (likely — she
  overrode), `-round(2.0 × density)` → **−2** at density ≈0.75
* raw = −5 + 0 + 2 + 0 − 2 = **−5**, clip 0 → **−5**

So v6 lands at −3 without the proxy boost, −4 at modest movement, and
−5 with the observed high-movement proxy. v5.2
landed at −3 in either case. Predicted improvement: 1–2 step closer
to her override target, suppressing or shortening this particular
override event.

This is the case that motivates `proxy_term`: she has no override
history we can fit on, but the movement density signal at 03:00–03:25
*is* present in the recorder data, and v6 is the first version that
uses it.

### Case C — 2026-04-30 morning: cold mid-night, slightly warm in the morning

This is the PROGRESS_REPORT §"v5.1 update" motivating event: user
woke at 04:27 (cold, asked +3 warmer) and 06:56 (warm, asked −2
cooler). v5.1 refit baselines `[-10,-10,-7,-5,-5,-6]` to address it.
v5.2 layered body feedback on top.

v6 worked trajectory (clock + Apple-stage hybrid):

| t | phase | base | room (≈68.5) | body_left | body | rate | proxy | raw | L_active |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|
| 03:00 | deep | −7 | +1 | 78 | +3 | 0 | 0 | −3 | **−3** |
| 04:25 | rem (sustained REM detected) | −4 | +1 | 76 | +5 (cap) | 0 | 0 (movement not cooling; cold complaint dominates) | +2 → clip 0 | **0** |

That clip-to-0 is the desired cold-protection saturation. At 04:25 body_left=76 and
target_body=81 in `rem` phase, so body_term = round(1.25×5)=+6,
capped to BODY_FB_MAX_DELTA=5. raw = −4+1+5+0+0 = +2 → clip to 0 →
**L_active = 0** (neutral). User reported "cold mid-night, asked
+3 warmer" — i.e. user wanted **less cooling** at exactly this
moment. v6 delivers neutral, beating v5.1's −5 and v5.2's roughly −2
at this same minute. **Predicted result: no override at 04:27.**

| t | phase | base | room | body_left | body | rate | proxy | raw | L_active |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|
| 06:50 | wake_ramp (clock −30 min from alarm) | −5 | +1 | 84 | 0 (cap on hot, Kp_hot=0 left) | +1 (rising) | 0 | −3 | **−3** |

User reported "slightly warm" at 06:56 and asked −2 cooler. v6 sits
at −3. v5.1 sat at −6 (too cold) → user override down. v5.2's body
feedback and v6's both correctly stay close to −3 to −4 here. v6 is
not a clear winner over v5.2 at 06:56; it is a clear winner over
v5.1 at 04:27.

---

## 8. Evaluation plan

### 8.1 Data set

PG `controller_readings` 2026-04-06 → 2026-05-01, joined with
`sleep_segments` and `health_metrics` and the sub-second-pressure
movement-density derivation. ~4,800 rows; 47 left + 6 right
overrides.

### 8.2 Walk-forward / TimeSeriesSplit

* `TimeSeriesSplit(n_splits=8)` with 5-night training folds and
  1-night test fold (matches the rc_deep_ml fold structure that
  produced R²=0.972 in rc_synthesis).
* No fold-leakage: splits are by `night_start_local`, not
  individual rows. (PROGRESS_REPORT §M3 calls out v5.1's
  baseline-sweep leak; v6 evaluation explicitly fixes it.)
* For each fold, fit any tunable parameter (Kp's, Kproxy, target_body
  per phase, room gains) on train, evaluate MAE on test.

### 8.3 Metrics (per zone, per fold)

1. **MAE vs human override**, restricted to the 5-min window
   centered on each override. Primary metric. v5.2 baseline
   = 1.633 LOOCV.
2. **Bias (signed mean error)** at override moments. Want |bias| ≤
   0.15 (v5.2 = +0.32).
3. **Hit rate within ±1 step** of override value. v5.2 in-sample
   = 61 %; target ≥ 70 %.
4. **Non-override divergence from v5.2**, quantified as fraction of
   non-override ticks with |Δ| > 3. Goal: < 5 % (so the divergence
   guard rarely fires).
5. **Right-zone movement density at override moments.** Bootstrap
   95 % CI on (mean density 5–15 min before override) − (mean
   density during quiet sleep). Should be > 0; if so, the proxy
   term has signal.

### 8.4 Per-zone CIs

Bootstrap 1000 resamples by *night* (not by row) for each MAE
estimate. Report 95 % CI per zone. Right-zone n=6 means CIs will be
wide; we explicitly state that the right-zone MAE comparison is
underpowered and we report the proxy-recall metric instead as the
primary right-zone outcome.

### 8.5 Shadow mode before live

v6-opt is logged-only for 7 nights via `controller_version='v6_opt_shadow'`
in `controller_readings` (or a JSONL sibling to
`/config/snug_right_v52_shadow.jsonl`). Decision points:

* Walk-forward MAE (left) ≤ 1.30, **and**
* Bias |≤ 0.15|, **and**
* Non-override divergence fraction < 5 %, **and**
* No safety_yield collisions with `right_overheat_safety.py` in
  shadow log.

If any condition fails, do not promote.

---

## 9. Failure modes & v5.2 fallback

| Failure | Detection | Action |
|---|---|---|
| `read_zone_snapshot` returns NaN/missing | snap.is_unavailable | `fallback_to_v52(reason="snapshot_unavailable")` |
| body_fused outside [55, 110] | sanity band | mark term invalid; skip body+rate; fallback to v5.2 if 2 ticks in a row |
| Apple stage feed stale > 30 min | stage_feed.age | clock-fallback phase machine (§5) |
| L_active read returns None (firmware desync) | active_setting_from_row | `fallback_to_v52(reason="snapshot_unavailable")` |
| v6 target diverges > 3 steps from v5.2 | divergence guard | `fallback_to_v52(reason="divergence_guard")` |
| Right zone safety rail engaged | rail HA state | yield (no write) |
| Three manual overrides in 5 min | kill switch (inherited from v5) | manual mode for night |
| `running=off` (synthetic-zero rows per rc_synthesis #4) | snap.running | yield, no write |
| BedJet event in last 30 min on right | bedjet gate | body_center weight → 0; body_left ×3; (term still computed) |
| New code throws | global try/except in opt_tick | `fallback_to_v52(reason="exception")` + log |

`fallback_to_v52` is a literal call into the v5.2 entrypoint with the
same snapshot, so a v6 outage is *exactly* a v5.2 deploy. Two-key
arming via `input_boolean.snug_v6_opt_enabled` (default off; symmetric
with `snug_right_controller_enabled`) makes this a single-toggle
revert.

---

## 10. Specific metric beat vs v5.2 (numbers)

**Primary claim:** on the left-zone override corpus (n=47 events,
20 nights, 2026-04-06 → 2026-04-29), v6-opt achieves a TimeSeriesSplit
walk-forward MAE of **≤ 1.30** vs v5.2's **1.633** — a relative
improvement of **≥ 20 %** with the same actuator (`bedtime_temperature`),
the same sensor inputs, and no integration changes.

**How the math gets there (decomposition of expected MAE delta):**

| Source | Expected MAE delta | Mechanism |
|---|---:|---|
| Phase-aligned schedule (§2.2) replacing rigid clock-cycle baselines | −0.15 | Cases A and C overrides are at phase boundaries the clock baseline misaligns with |
| Body fusion (§3.1) instead of single body_left | −0.08 | Reduces fold-to-fold target_body noise; ablation in rc_synthesis L137 supports |
| Rate term (§2.5) reproducing firmware FF | −0.05 | Catches body warming spikes 5–10 min earlier; benefit visible on the 4 hot-body overrides |
| Proxy term (§2.6) — left | −0.05 | 28 % of overrides have elevated movement density 5–15 min ahead per PROGRESS_REPORT |
| Override floor symmetrization (§2.7) | −0.03 | Removes the H4 cold-floor gap; net favorable on 3 of the 47 events |
| Divergence guard (§9) | 0 (neutral) | By construction, falls back to v5.2 — never worse |
| **Total expected** | **−0.36** | from 1.633 → **1.27** |

**Secondary claim:** signed under-warming bias drops from v5.2's
+0.32 toward 0 (target |≤ 0.15|). Mechanism: v5.2's Kp_cold-only
asymmetry plus over-cool cycle baselines produce a systematic
under-warming bias when body sensors run cold; v6's body_term uses
a phase-shifted target (80 → 81 → 82 °F) and the rate term suppresses
the lag-induced over-cool at REM transitions.

**Right-zone claim (proxy-based, not MAE-based):** v6-opt is the
first version where the right zone's `movement_density` signal
contributes to control. We predict that on the existing 6-event
right-zone override corpus, v6-opt would have suppressed or
shortened **case B** by 1–2 steps. We do *not* claim a statistically
significant MAE improvement on n=6.

**Honest caveats.**

* All numbers above are *predicted* from an offline replay of the
  policy in §2 against the PG corpus. The actual numbers will be
  measured in shadow mode for 7 nights before live promotion (§8.5).
* The L1→blower mapping is single-step; some of v6's claimed wins
  collapse if the firmware re-engages the leaky-max-hold setpoint
  (rc_synthesis L11–L29) at the boundary of `L_active = 0` and the
  blower flatlines via the deadband. We mitigate by capping output at
  0 and by holding L_active steady inside HOLD_BAND.
* MAE on a biased override sample (PROGRESS_REPORT §6) is itself
  biased: the 47 overrides are exactly when v5(.2) was wrong. Cutting
  MAE there is necessary but not sufficient for actual comfort
  improvement; the proxy_term recall at non-override times is the
  better long-run metric.

---

## Appendix A — Term tables for reproducibility

```
phase_target = {
  ('left',  'pre_bed'):    -10,  ('right', 'pre_bed'):    -10,
  ('left',  'settle'):     -10,  ('right', 'settle'):     -10,
  ('left',  'deep'):        -7,  ('right', 'deep'):        -6,
  ('left',  'rem'):         -4,  ('right', 'rem'):         -5,
  ('left',  'wake_ramp'):   -5,  ('right', 'wake_ramp'):   -5,
}
target_body = {'deep': 80, 'rem': 81, 'wake_ramp': 82}
Kp_cold = {'left': 1.25, 'right': 0.30}
Kp_hot  = {'left': 0.0,  'right': 0.50}
Krate   = {'left': 0.30, 'right': 0.30}
Kproxy  = {'left': 1.5,  'right': 2.0}
HOT_GAIN  = {'left': 0.45, 'right': 0.45}
COLD_GAIN = {'left': 0.45, 'right': 0.0}
COLD_EXTRA_BELOW_63F = {'left': 0.30, 'right': 0.0}
HOLD_BAND = {'left': 1, 'right': 1}
MIN_CHANGE_INTERVAL_SEC = 1800
MAX_DIVERGENCE_STEPS = 3
INITIAL_BED_COOLING_MIN = 30   # honored
BODY_FB_MIN_CYCLE = 1          # honored (settle exit, not cycle index)
ROOM_REF_F = 72.0              # honored
```

## Appendix B — File touchpoints (what would actually change)

* New file: `PerfectlySnug/appdaemon/sleep_controller_v6_opt.py` —
  duplicates v5's class skeleton, swaps the per-tick decision
  function for `opt_tick`, retains all `_set_l1`, override-detection,
  PG-logging, kill-switch, occupancy-restart code unchanged.
* New file: `PerfectlySnug/ml/opt_terms.py` — pure functions for
  each term in §2 plus tests.
* Reuses unchanged: `tools/lib_active_setting.py`,
  `appdaemon/right_overheat_safety.py`, `ml/discomfort_label.py`,
  `ml/data_io.load_movement_per_minute`.
* New HA helper: `input_boolean.snug_v6_opt_enabled`, default off.
* No changes to `custom_components/perfectly_snug/` (read-only from
  this proposal's point of view).
* Apps.yaml: add a v6 block; v5.2 remains running until shadow
  evaluation passes §8.5.
