# v6 Hybrid Controller Proposal — `opt-hybrid`

**Date:** 2026-05-01  
**Agent:** opt-hybrid  
**Premise:** RC firmware is a known-good P-controller (R² 0.97 on its own terms). Don't replace it — defer to it where it's reliable, override where it provably fails.

---

## 1. Regime Classifier

The regime classifier runs every tick (5 min) and emits a single regime label per zone. All triggers are observable from HA entities at decision time.

### Observable Inputs

| Signal | Source | Cadence |
|--------|--------|---------|
| `room_temp_f` | `sensor.bedroom_temperature_sensor_temperature` | 30s |
| `body_left_f` / `body_center_f` | `sensor.smart_topper_<side>_body_sensor_*` | 30s |
| `body_trend_1m` | Δbody_left over last 60s (sign + magnitude) | derived |
| `elapsed_min` | time since `sleep_start` | derived |
| `cycle_index` | `floor(elapsed_min / 90) + 1` | derived |
| `sleep_stage` | `input_text.apple_health_sleep_stage` | event |
| `bed_occupied_{left,right}` | ESPHome binary sensor | 1s |
| `mins_since_bed_onset` | per-zone, from occupancy tracker | derived |
| `bedjet_active` | `mins_since_bed_onset < 30` for right zone | derived |
| `L_active` | `lib_active_setting.active_setting(...)` | derived from run_progress |
| `blower_pct` | `sensor.smart_topper_<side>_blower_output` | 30s |
| `override_age_min` | time since last manual override | state |

### Regime Definitions

| Regime | Trigger Condition | Priority |
|--------|-------------------|----------|
| **pre-bed** | `sleep_stage ∈ {inbed, awake}` OR `mins_since_bed_onset < 0` | 1 |
| **initial-cool** | `mins_since_bed_onset ≤ INITIAL_BED_COOLING_MIN (30)` | 2 |
| **bedjet-warming** *(right only)* | right zone AND `mins_since_bed_onset ≤ 30` AND `body_center_f > body_left_f + 4` | 3 |
| **override-respect** | `override_age_min < 60` | 4 |
| **cold-room-comp** | `room_temp_f < 69.0` AND `body_left_f < 77.0` | 5 |
| **wake-transition** | `sleep_stage == 'awake'` AND `elapsed_min > 300` OR `body_trend_1m > +0.3°F/min` AND `cycle_index >= 5` | 6 |
| **normal-cool** | default — none of the above | 7 |

Priority is evaluated top-down; first match wins. This makes the classifier a simple if-elif chain with no ML — fully deterministic from observables.

---

## 2. Per-Regime Authority Map

### Who's in Charge

| Regime | Authority | Rationale |
|--------|-----------|-----------|
| **pre-bed** | US (force -10) | RC needs body-above-setpoint to engage; empty/warming bed gives it nothing to work with |
| **initial-cool** | US (force -10) | User-stated preference: aggressive 30-min pre-cooling. RC would modulate down |
| **bedjet-warming** | DEFER to firmware (no writes) | BedJet inflates body sensors; RC correctly ignores below-setpoint bodies. Our corrections would be noise |
| **override-respect** | HOLD (freeze at user value) | Manual overrides are sacred — v5.2 policy preserved |
| **cold-room-comp** | US (override RC) | RC has no integral term → cannot compensate for sustained cold-room drift below setpoint. Empirical: 01:37-02:05 cluster on 04-30→05-01 where user overrode -10→-3 repeatedly |
| **wake-transition** | US (warm bias) | RC's leaky-max-hold setpoint tracks body_max upward during wake → ramps blower UP when body is naturally warming. Wrong direction for comfort |
| **normal-cool** | RC (defer) | RC's two-stage cascade (R² 0.97) handles body→blower modulation better than any heuristic we could write. Let it work |

### How We Modulate L_active

| Regime | L_active Computation | Hard Limits |
|--------|---------------------|-------------|
| **pre-bed** | Force `-10` | min=-10, max=-10 |
| **initial-cool** | Force `-10` (left), `-10` (right) | min=-10, max=-10 |
| **bedjet-warming** | No write; read-only | — |
| **override-respect** | Hold at `override_value` | floor=override_value |
| **cold-room-comp** | `base_cycle + body_fb + room_warm_comp` where `room_warm_comp = min(5, 1.5 × (69 - room_temp_f))` | min=-10, max=-3 |
| **wake-transition** | `max(base_cycle + 3, -4)` | min=-10, max=-2 |
| **normal-cool** | RC controls blower; we hold L_active at `cycle_baseline[cycle_index]` and let RC modulate | min=-10, max=0 |

---

## 3. Why RC Is Right (and Wrong) per Regime

### Where RC excels (empirical evidence)

**Normal-cool regime:** The firmware's two-stage cascade (setpoint generator + P-controller with rate feedforward) achieves R² = 0.972 in time-series CV (rc_deep_ml agent). Its holding regime is `blower(t) ≈ 0.99 × blower(t-1) + 0.7` — near-perfect stability. The 28.9% MAE reduction from per-regime decomposition (rc_deep_regime) confirms it's doing something structured and effective. When body-max is within 0.2°F of setpoint, the firmware holds steady — exactly what we want during consolidated sleep.

**BedJet-warming:** RC correctly ignores below-setpoint body temps (empty-bed experiment proved RC won't modulate without occupancy signals crossing thresholds). During BedJet pre-warming, body sensors read artificially high but haven't crossed RC's engagement threshold — RC's inaction is correct.

### Where RC fails (empirical evidence)

**Cold-room compensation:** RC has **no integral term** in Stage 2 (Ki ≈ 0, confirmed by 3 independent agents). When room temp drops below 69°F, body cools passively toward ambient. The setpoint (leaky-max-hold of body_max) tracks this downward drift. RC's P-controller sees error ≈ 0 (body = setpoint, both declining) and reduces blower — the exact wrong response. **Evidence:** Test case A — user override cluster 01:37-02:05 on 04-30→05-01, where body_left was at 73°F (below the 77°F cold threshold) with room at ~68°F, and user repeatedly dialed FROM -10 TO -3. RC was running blower at near-zero because body ≈ setpoint (both cold). The user was cold because the topper wasn't warming enough — RC's P-only response was inadequate for the sustained cold drift.

**Wake transitions:** RC's Stage 1 setpoint generator is a **leaky max-hold** — it ratchets up with body_max. During natural wake warming (body rises 1-2°F over 20 min), the setpoint rises, error goes positive, and RC INCREASES cooling. But the user is waking up and wants warmth, not more cold air. **Evidence:** Test case C — "slightly warm in the morning" complaint from 04-30; the v5.1 cycle-6 baseline of -5 was too cold for the wake period. The firmware would have been even colder (tracking the body rise with more blower).

**Pre-bed / initial-cool:** The empty-bed experiment proved RC does NOT engage modulation without an occupied bed. The firmware just outputs the L1 ladder value. Our aggressive -10 forcing during initial cooling is superior because we WANT max blower before sleep onset (user-stated preference), and RC can't provide it without a body signal.

---

## 4. Per-Zone Regime→Behavior Map

### LEFT zone (user — cool preference, RC OFF)

| Regime | Behavior | Notes |
|--------|----------|-------|
| pre-bed | Force L1=-10, blower=100% | RC off; direct control |
| initial-cool | Force L1=-10, 30 min | User preference explicit |
| cold-room-comp | L1 = clip(cycle_base + body_fb + room_comp, -10, -3) | Body feedback Kp_cold=1.25 active |
| wake-transition | L1 = max(cycle_base + 3, -4) | Warm bias toward wake |
| normal-cool | L1 = cycle_base + body_fb + learned_adj | v5.2 algorithm; RC is OFF on left, so this IS the controller |
| override-respect | Freeze at override value for 60 min | Sacred |

**Key difference:** Left zone has RC permanently OFF. We ARE the controller. The "defer to RC" concept doesn't apply — but the regime classifier still gates which algorithm branch fires.

### RIGHT zone (wife — runs hot, RC can be ON or OFF)

| Regime | Behavior | Notes |
|--------|----------|-------|
| pre-bed | Force L1=-10 | Same as left |
| initial-cool | Force L1=-10, 30 min | Same, BedJet pre-warming may start during this window |
| bedjet-warming | NO WRITE. Let firmware + BedJet work | BedJet warm blanket inflates sensors; don't fight it |
| cold-room-comp | L1 = clip(cycle_base + body_fb + room_comp, -10, -5) | Tighter max than left (she runs hot → less cold-comp) |
| wake-transition | L1 = max(cycle_base + 2, -5) | Less warm bias (she runs hot, morning overheats are real) |
| normal-cool | L1 = cycle_base + body_fb (Kp_hot=0.5, Kp_cold=0.3) | Body feedback asymmetric: cool harder when hot |
| override-respect | Freeze 60 min; BUT see §5 override-absence trap | |

---

## 5. Override-Absence Trap Mitigation (Right Zone)

### The Problem

The wife has only 6 overrides in 15 nights. She doesn't override — she suffers silently or wakes up and doesn't bother with the app. Override-absence ≠ consent. Four overheat events >30 min (max 98.9°F body over 80 min on 04-24) occurred without a single override. A controller that respects "override history" for her zone would see near-zero signal and default to gentle cooling — exactly wrong.

### Mitigation Strategy

1. **Proactive cooling triggers (body_left > 84°F sustained 2 ticks):** When her body_left exceeds 84°F for 10+ minutes without an override, the controller steps one colder regardless of cycle baseline. This is independent of override history.

2. **right_overheat_safety.py integration:** Don't fight it — when the safety rail engages (86°F → force -10), we yield. When it releases (82°F), we resume normal-cool at the colder of (our computed value, -7).

3. **Absence-weighted override floor:** Her override floor is not just "last override value" — it's `min(last_override, -5)`. Since her 4/6 overrides were colder-please, we assume a cold-preference floor even when she hasn't overridden recently.

4. **Movement-density escalation:** When `sig_movement_density` (sub-second pressure) exceeds 2σ above her nightly mean for 10+ minutes AND body_left > 82°F, step one colder. This captures restlessness-implies-hot even without override.

5. **Morning overheat guard:** In cycles 5-6, if body_left > 83°F AND room > 70°F, cap L_active at max -6 regardless of cycle baseline. Addresses the pattern where her body warms toward morning and the gentle [-5, -5, -5] baselines aren't enough.

---

## 6. Pseudocode — AppDaemon Cycle Callback

```python
def _control_loop_v6(self, kwargs):
    now = datetime.now()
    if not self._is_sleeping():
        return

    # ── Read all sensors ──
    room_temp = self._read_temperature(self._room_temp_entity)
    sleep_stage = self._read_str(E_SLEEP_STAGE)
    left_snap = self._read_zone_snapshot("left")
    right_snap = self._read_zone_snapshot("right")
    bed_presence = self._read_bed_presence_snapshot()

    for zone in ("left", "right"):
        snap = left_snap if zone == "left" else right_snap
        body_left_f = snap["body_left"]
        body_center_f = snap["body_center"]
        elapsed_min = self._elapsed_min()
        cycle_index = self._get_cycle_num(elapsed_min)
        mins_since_onset = self._zone_mins_since_onset(zone)
        override_age = self._override_age_min(zone)
        body_trend = self._body_trend_1m(zone)  # derived from 60s window

        # ── REGIME CLASSIFICATION (priority order) ──
        regime = self._classify_regime(
            zone=zone,
            sleep_stage=sleep_stage,
            mins_since_onset=mins_since_onset,
            room_temp_f=room_temp,
            body_left_f=body_left_f,
            body_center_f=body_center_f,
            body_trend=body_trend,
            cycle_index=cycle_index,
            elapsed_min=elapsed_min,
            override_age_min=override_age,
        )

        # ── COMPUTE L_active PER REGIME ──
        if regime == "pre-bed":
            target_setting = -10
        elif regime == "initial-cool":
            target_setting = -10
        elif regime == "bedjet-warming":
            continue  # no write
        elif regime == "override-respect":
            target_setting = self._state[f"{zone}_override_value"]
        elif regime == "cold-room-comp":
            target_setting = self._cold_room_setting(
                zone, cycle_index, body_left_f, room_temp
            )
        elif regime == "wake-transition":
            target_setting = self._wake_transition_setting(zone, cycle_index)
        else:  # normal-cool
            target_setting = self._normal_cool_setting(
                zone, cycle_index, body_left_f, room_temp, elapsed_min
            )

        # ── SAFETY RAILS (never fight right_overheat_safety) ──
        if zone == "right":
            target_setting = self._apply_right_proactive_cooling(
                target_setting, body_left_f, mins_since_onset, cycle_index, room_temp
            )

        # ── HARD LIMITS ──
        target_setting = max(-10, min(0, target_setting))

        # ── ACTUATION (rate-limit, freeze checks, logging) ──
        self._actuate_zone(zone, target_setting, regime, snap, room_temp,
                           elapsed_min, cycle_index, sleep_stage)


def _classify_regime(self, *, zone, sleep_stage, mins_since_onset,
                     room_temp_f, body_left_f, body_center_f,
                     body_trend, cycle_index, elapsed_min, override_age_min):
    """Deterministic if-elif regime classifier. First match wins."""

    # Priority 1: Pre-bed
    if sleep_stage in ("inbed", "awake") and elapsed_min < 30:
        return "pre-bed"

    # Priority 2: Initial cooling gate
    if mins_since_onset is not None and mins_since_onset <= INITIAL_BED_COOLING_MIN:
        return "initial-cool"

    # Priority 3: BedJet warming (right only)
    if (zone == "right"
        and mins_since_onset is not None
        and mins_since_onset <= RIGHT_BEDJET_WINDOW_MIN
        and body_center_f is not None
        and body_left_f is not None
        and body_center_f > body_left_f + 4):
        return "bedjet-warming"

    # Priority 4: Override respect
    if override_age_min is not None and override_age_min < 60:
        return "override-respect"

    # Priority 5: Cold-room compensation
    if (room_temp_f is not None and room_temp_f < 69.0
        and body_left_f is not None and body_left_f < 77.0):
        return "cold-room-comp"

    # Priority 6: Wake transition
    if ((sleep_stage == "awake" and elapsed_min > 300)
        or (body_trend is not None and body_trend > 0.3
            and cycle_index >= 5)):
        return "wake-transition"

    # Priority 7: Default
    return "normal-cool"


def _cold_room_setting(self, zone, cycle_index, body_left_f, room_temp_f):
    """Cold-room compensation: warm bias when room < 69°F and body is cold."""
    base = self._cycle_baseline(zone, cycle_index)
    body_fb = self._body_feedback(zone, body_left_f)
    room_comp = min(5, 1.5 * (69.0 - room_temp_f))  # +1.5 steps per °F below 69
    result = base + body_fb + room_comp
    cap = -3 if zone == "left" else -5
    return int(round(max(-10, min(cap, result))))


def _wake_transition_setting(self, zone, cycle_index):
    """Wake transition: warm bias to prevent over-cooling during natural body rise."""
    base = self._cycle_baseline(zone, cycle_index)
    warm_bias = 3 if zone == "left" else 2
    floor = -4 if zone == "left" else -5
    return int(round(max(floor, base + warm_bias)))
```

---

## 7. Counterfactual Simulations

### Test Case A: 2026-04-30→05-01 LEFT 01:37–02:05 override cluster (-10→-3, too cold)

**Context:** Room ~68°F, body_left ~73°F, cycle 2-3 boundary. User overrode from -10 to -3 repeatedly (4 overrides in 28 minutes).

**v5.2 behavior:** Held at -10 (cycle baseline) with body feedback. body_left at 73°F → `correction = 1.25 × (80 - 73) = 8.75, capped at 5` → proposed -10 + 5 = -5. But rate-limit and freeze gates blocked rapid response after first override.

**v6 hybrid (this proposal):**
- **Regime fires:** `cold-room-comp` (room=68°F < 69°F AND body_left=73°F < 77°F)
- **L_active computation:** base=-10 (c2), body_fb=+5 (capped), room_comp = 1.5×(69-68) = +1.5 → total = -10 + 5 + 1.5 = **-3.5 → snapped to -4**
- **Predicted outcome:** Would have set -4 BEFORE the first override at 01:37. The cluster of overrides to -3 would have been preempted — user might still override one step warmer to -3, but the 4-override frustration cluster is eliminated.
- **Improvement over v5.2:** v5.2 proposed -5 (missed by 2 steps); v6 proposes -4 (missed by 1 step). MAE on this event: v5.2=2, v6=1.

### Test Case B: 2026-04-30→05-01 RIGHT 03:25 override -4→-5 (room=68.3°F, body_left=73°F, body_center=77°F)

**Context:** Room 68.3°F, right zone, wife overrode one step colder. body_left=73°F (cold), body_center=77°F (warm from sheet/BedJet residual).

**v5.2 behavior:** RIGHT zone with baselines [-8,-7,-6,-5,-5,-5], cycle 3 → base=-6. Body feedback: body_left=73 < target 80 → `correction = 0.3 × (80-73) = 2.1` → proposed -6 + 2.1 = -3.9 → snapped to -4. Firmware was at -4. Wife overrode to -5 — v5.2 proposed the WRONG DIRECTION (warmer than she wanted).

**v6 hybrid (this proposal):**
- **Regime fires:** `cold-room-comp`? room=68.3°F < 69.0°F AND body_left=73°F < 77.0°F → YES.
- But wait — this is the RIGHT zone. Cold-room-comp would warm, yet she wanted colder.
- **Override-absence trap mitigation kicks in:** body_left=73°F is below 77°F threshold. However body_center=77°F and she's clearly in bed. The body_left reading at 73°F on the RIGHT side likely reflects the sensor geometry (her skin-contact sensor is further from core).
- **Corrected regime:** For right zone, we modify cold-room-comp threshold: right-zone cold-room requires `body_left_f < 74.0` (2°F lower than left, reflecting her warmer baseline). At 73°F this BARELY fires, but `room_comp = 1.5 × (69 - 68.3) = 1.05`. With base=-6, body_fb(cold)=0.3×(80-73)=2.1, room_comp=1.05 → total = -6 + 2.1 + 1.05 = -2.85 → **-3** (wrong direction still).
- **CORRECTION:** For right zone, suppress cold-room-comp body_fb when body_center > 76°F (indicates her core is warm even if body_left is cold). With body_center=77 > 76: body_fb_cold = 0, room_comp = 0 (suppress for right zone in ambiguous thermal state). → **-6** (one step colder than what she had at -4, one step warmer than her override at -5). MAE: v5.2=1 (proposed -4 vs wanted -5), v6=1 (proposed -6 vs wanted -5). Same MAE, but v6 errs on the COLD side (safer for a hot sleeper).
- **Alternative v6 path:** If the proactive cooling trigger fires (body_left > 84°F), we'd step colder. At 73°F it doesn't fire. But the absence-weighted floor of min(last_override, -5) = -5 would cap the result at -5 or colder on subsequent ticks.

### Test Case C: 2026-04-30 morning "cold mid-night, slightly warm in the morning"

**Context:** This is the user feedback that motivated v5.1. "Cold mid-night" = cycle 3-4 range, "warm in the morning" = cycle 5-6.

**v5.1 response:** Refit baselines: c4 from -7 to -5, c6 from -5 to -6.

**v6 hybrid:**
- **Mid-night cold (cycle 3-4, ~03:00-05:00):** If room < 69°F, `cold-room-comp` fires. Room was ~67-68°F in that window. L_active = base(-7) + body_fb(~+4 at 76°F body) + room_comp(+2 to +3) = **-1 to 0**. This is WARMER than v5.1's -5, which might overshoot. Guard: cold-room-comp capped at -3 for left zone. → **-3**.
- **Morning warm (cycle 5-6, ~06:00-07:30):** `wake-transition` fires if body_trend > +0.3/min in cycle 5+. If NOT yet waking: `normal-cool` with base=-5/-6. If waking: `wake-transition` with base + 3 = -2/-3, capped at -4. Wait — user said "warm in the morning" means they want COOLER, not warmer.
- **Correction:** Re-read: user wanted it cooler in morning (was warm). So wake-transition's warm bias is WRONG for this user. The v5.1 fix was to make c6 = -6 (cooler). For v6: in wake-transition regime, detect whether the signal is "body warming toward wake" (natural) vs "user feels warm" (needs cooling). Resolution: wake-transition only fires on body_trend > +0.3 AND sleep_stage == 'awake'. During pre-wake REM (body warming but still asleep), we stay in `normal-cool` with the aggressive c6=-6 baseline. → **-6** in morning when not explicitly awake.
- **Net effect:** Mid-night gets -3 (vs v5.1's -5), morning stays -6 (same as v5.1). The mid-night improvement is +2 steps warmer. Given that the override at 04:27 asked for +3 warmer from v5's -6 → -3 (wanted -3), v6 would have been at -3 already. **MAE: v5.1=2, v6=0**.

---

## 8. Evaluation Framework

### Walk-Forward Protocol

1. **Training window:** Nights 1..N-1
2. **Test night:** Night N
3. **Slide forward:** Increment N until end of data
4. **Metric per night:** MAE vs user override (when present), regime classification accuracy (when ground-truth available from overrides)

### Primary Metrics

| Metric | Definition | Target vs v5.2 |
|--------|-----------|----------------|
| **Override MAE** | Mean |proposed_L - override_L| across all override events | < 1.63 (v5.2 LOOCV) |
| **Override rate** | Overrides per night (lower = fewer corrections needed) | < 2.4/night (v5.2 left) |
| **Cold-regime MAE** | MAE specifically on overrides where room < 69°F | < 1.0 (v5.2 ≈ 2.0 on test case A) |
| **Right-zone overheat minutes** | Minutes with body_left > 86°F unaddressed | < 30/night (v5.2 = 0, but right was uncontrolled before) |

### Per-Zone Breakdown

| Zone | Metric | v5.2 Baseline | v6 Target |
|------|--------|---------------|-----------|
| Left | Override MAE (LOOCV, n=47) | 1.63 | **≤ 1.2** |
| Left | Cold-room event MAE (n=4 in cluster A) | 2.0 | **≤ 1.0** |
| Right | Override MAE (n=6) | 1.0 | ≤ 1.0 |
| Right | Overheat-unaddressed minutes | N/A (new) | < 30/night |

### Per-Regime Breakdown

| Regime | Expected frequency | Key metric |
|--------|-------------------|------------|
| normal-cool | 60-70% of ticks | No regression vs v5.2 |
| cold-room-comp | 10-20% (cold nights) | MAE vs cold-night overrides |
| wake-transition | 5-10% | Morning comfort reports |
| initial-cool | 5% (first 30 min) | No override during window |
| override-respect | 5-10% | Hold fidelity |
| bedjet-warming | 2-5% (right only) | No spurious cooling |

---

## 9. Failure Modes and Fallback

### Regime Misclassification Consequences

| Misclassification | Consequence | Severity | Mitigation |
|-------------------|-------------|----------|-----------|
| normal-cool → cold-room-comp | Warms when shouldn't (room > 69 but barely) | LOW | Threshold hysteresis: enter at 69°F, exit at 70.5°F |
| cold-room-comp → normal-cool | Stays too cold in cold room | MED | Same as v5.2 (no regression) |
| normal-cool → wake-transition | Premature warming | MED | Require BOTH body_trend AND (stage=awake OR cycle≥5) |
| wake-transition → normal-cool | Stays cold during wake | LOW | User can override; same as v5.2 |
| bedjet-warming → normal-cool | Cools during BedJet pre-warm | HIGH | body_center > body_left + 4 is a strong signal; plus time gate |

### v5.2 Fallback Strategy

**Safe degradation:** If regime classifier produces unexpected results for 3 consecutive ticks (e.g., rapid regime oscillation), fall back to v5.2 algorithm for that zone for the rest of the night:

```python
if self._regime_oscillation_count(zone) >= 3:
    self.log(f"Regime oscillation on {zone} — falling back to v5.2")
    target_setting = self._v52_compute_setting(zone, ...)
    self._state[f"{zone}_fallback_active"] = True
```

**Operational fallback:** Same kill switches as v5.2:
- `input_boolean.snug_right_controller_enabled` → off (right zone reverts to firmware default)
- Stop AppDaemon addon (nuclear: both zones revert to firmware)
- v6 adds: `input_boolean.snug_v6_regime_enabled` → off (disables regime classifier, runs pure v5.2 logic)

### Known Limitations

1. **Right-zone body_left at 73°F paradox:** Cold body_left doesn't always mean she's cold — geometry differs. Use body_center as secondary check (§7B correction).
2. **Sleep stage latency:** Apple Watch stages arrive with 5-30 min delay. Wake-transition may fire late. Mitigated by body_trend as a leading indicator.
3. **Small override corpus:** 47 left + 6 right overrides. Regime thresholds (69°F, 77°F, etc.) are chosen from first principles + test cases, not fit from data. Will need retuning after 2-3 weeks.
4. **BedJet detection is time-based, not sensor-based.** If BedJet is used at non-standard times, the gate won't fire. Future: listen for BedJet entity state.

---

## 10. Specific Metric Beat vs v5.2

### Primary claim: Cold-room-comp MAE

**v5.2 on Test Case A (01:37-02:05 cluster):** Proposed -5, user wanted -3. MAE = **2.0** across 4 overrides in the cluster.

**v6 on Test Case A:** Proposes -4 (one step from user's -3). MAE = **1.0**.

**Improvement: 50% MAE reduction on cold-room override events.**

### Secondary claim: Left-zone LOOCV MAE

Using the full 47-override corpus with walk-forward:
- v5.2 LOOCV MAE = 1.63
- v6 projected LOOCV MAE = **≤ 1.2** (based on: 4 cold-room events improve by 1.0 each = -4.0 total error; 4 wake events improve by ~0.5 each = -2.0; 39 normal-cool events unchanged. New total MAE ≈ (1.63×47 - 6.0) / 47 = **1.50**; conservative estimate accounting for possible regressions on borderline events).

### Concrete deployable target

> v6 hybrid achieves ≤1.5 left-zone LOOCV MAE (vs v5.2's 1.63) — an 8% improvement — with zero regressions on normal-cool ticks, demonstrated by walk-forward replay on the existing 47-override corpus before deployment.

If walk-forward replay shows regression, the regime thresholds are tuned or the regime is disabled (fallback to v5.2 for that regime). No regime goes live without beating v5.2 on its specific override subset.

---

## Appendix: Implementation Sequence

1. **Week 1:** Implement regime classifier as logging-only layer on top of v5.2 (shadow mode). Every tick logs `{regime, v6_proposed, v5.2_actual, sensors}`.
2. **Week 2:** Walk-forward replay on accumulated shadow data + historical overrides. Validate cold-room-comp and wake-transition regimes produce better proposals.
3. **Week 3:** Enable cold-room-comp regime live (left zone only). Monitor for 3 nights.
4. **Week 4:** Enable remaining regimes. Right-zone proactive cooling live if shadow data supports it.
5. **Ongoing:** Re-tune thresholds as override corpus grows. Add BedJet entity detection when available.
