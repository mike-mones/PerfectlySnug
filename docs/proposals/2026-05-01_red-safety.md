# v6 red-team safety review — red-safety (2026-05-01)

Scope: safety / hardware-damage review of the four optimizer proposals (`opt-mpc`, `opt-scratch`, `opt-hybrid`, `opt-learned`) against the deployed v5.2 controller and `right_overheat_safety.py` rail. I assume the non-negotiable policy is **cooling-side only**: v6 may write integer settings in `[-10, 0]` and must never write positive heat.

## Executive safety findings

1. **The biggest unsafe surface is right-zone coordination.** `right_overheat_safety.py` is a separate AppDaemon app that writes the same HA number entity as every proposed v6 right-zone controller. It has no mutex, no lease, and no shared state with v5/v6. Any proposal that says “yield to the rail” but does not define a machine-readable rail-engaged flag or writer lock is still unsafe.
2. **There is no documented firmware 86/82°F safety rail.** The 86°F engage / 82°F release right-zone rail is AppDaemon-only. Turning RC off or bypassing RC does not bypass that AppDaemon app if it remains running, but it does bypass RC’s inner occupancy-gated modulation, deadband, and rate feedforward. Hardware may still clip blower duty internally to 0–100%, but I found no evidence of firmware-level body-overheat protection equivalent to `right_overheat_safety.py`.
3. **All four proposals can create unsafe behavior under plausible missing/stale data.** Common bad input: right `body_left` stuck at 79°F, right occupancy stuck `on`, BedJet heat on outside the first 30 minutes, room sensor unavailable, and v6 keeps a mild setting while body_center/body_max climb into the 90s.
4. **Thermal cycling risk is under-specified.** Every optimizer can output a different L_active every 5 minutes. Topper firmware may smooth internally, but this is not guaranteed. A v6 safety wrapper must enforce a minimum write interval and max step rate independent of the optimizer.

---

## opt-mpc — outer-loop MPC over `L_active`

### 1. Top 3 safety / hardware failure modes

1. **Right rail hard constraint fights, rather than yields, near 86°F.**
   - Concrete input: right occupied, BedJet window expired, `body_left_R = 86.2°F` for two safety polls, current right setting `-5`, MPC’s cost sees `w_effort` and smoothness prefer `-6` over `-10` because predicted 30-min body trajectory is only mildly hot.
   - Unsafe action: MPC writes `-6` on its 5-min tick after the rail writes `-10`, prematurely weakening emergency cooling. The proposal says the optimizer is ignored if the rail is engaged, but does not specify a reliable engaged-state read from `right_overheat_safety.py`.

2. **Model error underestimates sustained overheat.**
   - Concrete input: right `body_left_R = 85.5°F`, `body_center_R = 94°F`, room `73°F`, occupancy true, history from BedJet contamination makes the model discount center, plant β fitted low, forecast says `body_left` falls with `u=-6`.
   - Unsafe action: MPC chooses `-6` or `-7` instead of max cooling; body_left crosses 86°F between 5-min ticks and remains above threshold while the MPC continues optimizing comfort/effort. If the rail is disabled or crashed, there is no second hard stop.

3. **Random-shooting / CEM jitter causes L_active flapping.**
   - Concrete input: `body_left` oscillates 80.8↔82.2°F, room 71.8↔72.2°F, stage `core/rem` alternates due Apple lag, two candidate trajectories differ by tiny cost.
   - Unsafe action: first committed `u0` alternates `-5, -8, -5, -7` every 5 minutes. This can thermally and mechanically cycle the blower/topper; `w_smooth` in the horizon is not a hard actuator rate limit.

### 2. Coordination failures with `right_overheat_safety.py`

- Both apps write `number.smart_topper_right_side_bedtime_temperature` unless v6 implements an external mutex. The rail has no public HA entity for `engaged`; it persists JSON state private to its app. Reading the numeric setting `-10` is not sufficient: a user or v6 may also legitimately set `-10`.
- Rail engage/release writes can be misclassified as manual overrides by a controller listener, creating accidental freezes; conversely self-write suppression may cause v6 to ignore a real user override.
- MPC’s hard constraint “right `b_L >= 86` → `u=-10`” duplicates the rail logic. Duplicating is dangerous: if v6 uses a 5-min cadence and the rail uses 60s cadence, v6 can race the rail and restore a non-rail value before 82°F release.

### 3. Sustained overheat scenarios

- **Sensor stuck low:** right `body_left_R` stuck at 79°F, center/right sensors climb >94°F, occupancy true. MPC cost is keyed to `b_L`; it may hold `-5` while real body overheats. Required mitigation: stuck-sensor detector using sensor variance/cross-channel disagreement and fail closed to `-10` or v5.2 hard rail.
- **Occupancy false positive + BedJet:** empty bed or partial occupancy, BedJet heats sheets, `body_center` high, `body_left` ambiguous. MPC may classify as BedJet / no discomfort and avoid cooling; if a person returns to a preheated bed, the rail’s occupancy-onset suppression may mask the heat for 30 minutes.
- **PG/model stale:** if nightly fit is stale or plant model not refit after recorder fix, MPC can under-predict right sensitivity/overheat and continue mild settings.

### 4. Hardware thermal cycling

- The solver replans every 5 minutes. Smoothness in the cost is not a guarantee. If the optimizer’s random tail or stage/room noise changes the argmin, it can issue repeated 1–3 step changes.
- Required hard wrapper: no more than 1 step per 15 minutes in normal operation, no more than 2 steps per 30 minutes except emergency cooling to `-10`, and no write if target differs by only 1 step inside a deadband unless sustained for two ticks.

### 5. BedJet conflicts

- Proposal’s BedJet hard constraint says right `u >= -4` during a warm-blanket window, while known initial-bed policy wants `-10` for the first 30 minutes. That is a direct conflict unless explicitly ordered. If BedJet heat is on and v6 pins `>= -4`, bed surface can warm enough to trigger the 86°F rail immediately after suppression ends.
- `climate.bedjet_shar` exists now but the proposal mostly models BedJet as a time window. Heat outside the first 30 minutes is not handled.

### 6. Fail-safe on missing data

- PG outage: OK for inference if model file exists, unsafe for retraining if bad model remains unnoticed. Must freeze model version and alert; do not silently retrain partial data.
- Recorder gap / missing `run_progress`: if 3-level mode is on, falling back to L1 can write the wrong dial. Safer response is force 3-level off or no-write + v5.2 fallback.
- Room sensor unavailable: proposal widens uncertainty and may use last known. For safety, stale room should remove comfort optimizations and keep only conservative v5.2 + rail.
- Presence flapping: can repeatedly restart BedJet/initial-cool windows; dangerous because it suppresses rail and/or forces max-cool. Need first-onset-of-night semantics and debounce.

### 7. Override path safety

- The proposal encodes warm-override floor as `u <= u_floor`, but with negative settings this inequality is easy to get wrong. A manual warm override to `-3` must prevent later controller writes colder than `-3` for the rest of the night unless the hard safety rail fires.
- Cold overrides should at minimum set a cooling floor or long freeze on the right zone; otherwise a wife `-4 → -5` can be erased after 60 minutes.

### 8. AppDaemon crash / hang behavior

- If v6 crashes after writing a mild setting (e.g., `-4`) and the safety rail is also crashed or disabled, the topper stays at that setting. If RC is on, firmware still modulates; if RC is off, this is a fixed blower-proxy setting. Need dead-man timer that reverts to v5.2 deterministic safe baseline or firmware RC after missed ticks.

---

## opt-scratch — RC-off direct controller

### 1. Top 3 safety / hardware failure modes

1. **Bypassing RC removes the only documented inner modulation.**
   - Concrete input: right occupied, RC off, 3-level off, setting `-5`, `body_left_R` climbs 83→87°F over 20 minutes, `movement_density=0`, user asleep/no override.
   - Unsafe action: scratch body term may stay mild because fused body is pulled down by `body_left=79–81` or because right cold gain/proxy dominate; firmware no longer has RC rate feedforward to increase blower. Only AppDaemon rail remains.

2. **Movement-density proxy drives unnecessary cooling and blower cycling.**
   - Concrete input: pressure movement caused by partner/pet/position change, right `body_left=73°F`, room 67°F, movement density high 0.8.
   - Unsafe action: proxy term pushes `-2` steps cooler even though skin signal is cold. User may become cold, override warm, then proxy cools again after freeze. Hardware cycles as pressure spikes repeat.

3. **Divergence guard can fail open in unsafe direction.**
   - Concrete input: v5.2 proposes `-3` due cold body, scratch raw proposes `0`, divergence >3 triggers fallback. But fallback v5.2 itself may be wrong for right overheat or may lack right override floor.
   - Unsafe action: “fallback to v5.2” is not inherently safe for right-zone overheating; v5.2 depends on separate rail and has known right-zone logging/override gaps.

### 2. Coordination failures with `right_overheat_safety.py`

- Scratch says it yields when rail is engaged, but there is no explicit rail engaged HA entity. It must not infer engagement from setting `-10` alone.
- If scratch listens to the same number entity and treats rail writes as manual overrides, rail engage/release can install incorrect floors/freezes.
- **Does bypassing RC bypass firmware safety?** It bypasses RC’s inner setpoint generator, Hammerstein deadband, and rate feedforward. Those are control features, not documented safety rails. The documented 86/82°F right-zone rail is AppDaemon-only and remains active only if `right_overheat_safety.py` is running and enabled. Hardware likely enforces setting/duty clipping, but no evidence shows firmware will force max cooling at 86°F skin.

### 3. Sustained overheat scenarios

- **Right body_left geometry false low:** right `body_left=73°F`, center 90°F, wife actually warm. Scratch may suppress cold-room warming only in the specific `body_center>76` case; outside that narrow case, body fusion can still misclassify.
- **BedJet outside expected window:** BedJet turns on at 03:00 via `climate.bedjet_shar`; scratch only gates first 30 minutes after occupancy. It may interpret hot sensors as overheat or movement as discomfort, fighting BedJet and then triggering rail.
- **Rail disabled:** scratch delegates right hard safety entirely to `right_overheat_safety.py`. If the rail helper is off or app crashed, scratch has no right hard stop except its own term choices.

### 4. Hardware thermal cycling

- HOLD_BAND=1 and 30-min write interval help, but term-level discontinuities remain: phase changes, movement density spikes, body_fused EMA crossing target, and divergence-guard fallback can jump target by 3 steps.
- The proxy term can oscillate on pressure bursts, creating alternating `-5 ↔ -7` proposals every time the 30-min rate gate opens.

### 5. BedJet conflicts

- Scratch intentionally forces `-10` during initial-bed even while BedJet pre-warms, while the safety rail suppresses overheat for that same 30 minutes. This can create a heater-vs-cooler fight: BedJet heats the blanket while topper runs 100% blower.
- If `climate.bedjet_shar` is active longer than 30 minutes, scratch may use contaminated body_center/body_left values and continue cooling against intentional heat.

### 6. Fail-safe on missing data

- Missing body snapshot: proposal falls back to v5.2. For right side, v5.2 may not have safe rail integration; fallback must also assert rail app alive/enabled.
- Room unavailable: no explicit fail-closed; room term disappears, possibly reducing cooling in a warm room.
- L_active missing: fallback assumes v5.2, but if 3-level mode is on and v5.2 writes L1, the write may be inert. Must force 3-level off or abort.
- Presence flapping: proposal inherits known bug of BedJet window restarting on every re-entry; this can repeatedly suppress safety logic or force initial `-10`.

### 7. Override path safety

- Proposal says warm overrides ratchet for rest of night, cold overrides only 60 minutes. That is unsafe for the right zone where 5/7 overrides are cooler and silence is not consent. A right cold override to `-5` should be a hard **ceiling on warmth** (do not later write warmer than `-5`) for the rest of the night unless the user changes it.
- Divergence guard can override a user floor if floor is applied before fallback instead of after. Floors must be the final non-emergency gate.

### 8. AppDaemon crash / hang behavior

- With RC off, a crash leaves the last fixed setting in place. If crash leaves `0` or `-3` during a warm right-zone period, no RC inner loop will rescue. Dead-man must either re-enable RC or force a conservative cooling baseline when v6 stops heartbeating.

---

## opt-hybrid — deterministic regime classifier

### 1. Top 3 safety / hardware failure modes

1. **Priority bug suppresses BedJet no-write.**
   - Concrete input: right occupied 10 minutes, BedJet heat on, `body_center=94°F`, `body_left=88°F`, `mins_since_onset=10`.
   - Unsafe action: classifier priority selects `initial-cool` before `bedjet-warming`, so v6 forces `-10` instead of no-write. This fights BedJet and can rapidly trip the rail after the suppression window.

2. **Wake-transition warm bias during actual heat.**
   - Concrete input: cycle 6, `body_left_R=85°F`, room 72.5°F, sleep_stage `awake` or body trend >0.3°F/min.
   - Unsafe action: wake-transition computes `max(base+2, -5)` or left `max(base+3, -4)`, warming the setting precisely while body is rising. The proposal notices this contradiction in case C but leaves a complex condition that can still fire on stale `awake` data.

3. **Right proactive cooling can duplicate/fight the 86°F rail.**
   - Concrete input: right `body_left=84.5°F` for 10 minutes, room 71°F, controller steps colder; one tick later rail engages at 86°F and writes `-10`; hybrid release policy says resume at colder of computed and `-7`.
   - Unsafe action: v6 may write `-7` immediately after rail release to 82°F, causing a heat rebound and possible rail re-engagement cycles.

### 2. Coordination failures with `right_overheat_safety.py`

- “Yield when rail engages” needs a mutex or explicit flag. Without it, hybrid’s `_actuate_zone` can write after the rail.
- Hybrid adds its own proactive thresholds (`body_left >84°F`, morning cap, movement escalation). These are not wrong, but they create a second safety-like controller with different thresholds/cadence. That can cause rail-adjacent oscillation: v6 cools at 84, rail forces at 86, rail releases at 82, v6 resumes at -7, body climbs again.
- Rail suppression and hybrid BedJet detection both use occupancy onset; both inherit restart/re-entry bugs unless centralized.

### 3. Sustained overheat scenarios

- **Stale awake/stage:** Apple Health says `awake` from 20 minutes ago; hybrid warms for wake-transition while user is asleep and warming.
- **Right late-night no labels:** right has no late-cycle overrides. Hybrid’s absence-weighted floor `min(last_override,-5)` is helpful but not enough if right body_left sits 85.8°F (below rail) for hours at `-5`.
- **Room sensor stuck cold:** room reads 68°F when actual is 74°F; cold-room-comp warms left/right, potentially removing cooling during a true hot-room overheat.

### 4. Hardware thermal cycling

- Regime thresholds are discontinuous: room 68.9↔69.1°F, body_left 76.9↔77.1°F, trend 0.29↔0.31°F/min. Without hysteresis and dwell timers, the controller can flip regimes and targets every tick.
- “Fallback after 3 oscillations” detects the problem only after cycling has already happened.

### 5. BedJet conflicts

- BedJet-warming priority comes after initial-cool, so it is unreachable during the exact first-30-minute BedJet window when it matters most.
- Detection uses `body_center > body_left + 4`; if BedJet heats both sensors, the spread condition may fail and hybrid will cool aggressively.
- It does not use current `climate.bedjet_shar`; time-window detection will miss non-standard BedJet use.

### 6. Fail-safe on missing data

- Missing body or room can make regimes fail to `normal-cool`, which for right still writes body-feedback values. Safer behavior is no-write/v5.2 with rail verified.
- Presence flapping re-enters initial-cool and BedJet windows; may repeatedly force `-10` or suppress overheat handling.
- PG outage affects evaluation/retraining less because hybrid is deterministic, but logging gaps hide safety regressions.

### 7. Override path safety

- `override-respect` priority is below initial-cool/BedJet. A manual override during the first 30 minutes can be ignored by a higher-priority initial-cool force. Manual override must be the top priority except emergency overheat.
- Right “absence-weighted floor” is promising, but `min(last_override, -5)` can overcool after a warm override unless direction-specific logic is precise.

### 8. AppDaemon crash / hang behavior

- Hybrid often assumes RC handles normal-cool, but deployed left RC is off and right may be off per current production. If hybrid crashes after changing modes/settings, the actual firmware state may not match the proposal’s assumption. Dead-man must restore known v5.2/RC configuration, not just stop writing.

---

## opt-learned — conservative learned residual on v5.2

### 1. Top 3 safety / hardware failure modes

1. **Composite reward learns unsafe proxy correlations.**
   - Concrete input: right movement density low, `body_left_R=85.8°F`, center 94°F, asleep/no override, room 73°F.
   - Unsafe action: learned residual Δ=0 because no movement/override support, so v5.2 mild right setting persists just below rail. Sustained near-overheat continues for hours.

2. **Stuck sensor creates confident wrong residual.**
   - Concrete input: body_left sensor stuck at 78°F for multiple nights; body_center varies with real heat; model sees stable “comfortable” body_skin and low movement.
   - Unsafe action: residual warms or fails to cool, because Bayesian uncertainty shrinks on repeated stuck values unless explicit stuck-sensor tests exist.

3. **Model/version failure causes stale bad policy.**
   - Concrete input: nightly refit during PG partial outage writes model trained on incomplete right pressure/BedJet data; v6 loads it and GP/ridge happen to agree.
   - Unsafe action: bounded Δ still shifts by 1 step every eligible period all night. A 1-step warm shift can matter during sustained right overheat.

### 2. Coordination failures with `right_overheat_safety.py`

- The learned proposal says rail output always overrides model output. This must be implemented in the actuation wrapper, not merely in the learned residual. If the model computes `v5.2 + Δ` after rail application, it can undo `-10`.
- Subclassing v5 inherits v5’s two-app conflict surface and right-zone PG logging gap unless explicitly fixed.
- Rail engage/release events may appear as negative rewards or “overheat rail engagement event” in training. If the model learns to avoid contexts that trigger the rail by warming earlier, that is unsafe; rail events should be hard safety labels, not comfort labels.

### 3. Sustained overheat scenarios

- **Right LCB stays Δ=0:** The proposal admits right zone will behave like v5.2 for ~10 nights. If v5.2 is insufficient and rail is suppressed/disabled, v6 provides no added safety.
- **Override absence dominates:** Even with composite reward, quiet hot sleep can be misclassified as comfortable if movement is low.
- **Sleep-stage lag:** model may not know the user is in late REM/wake where thermal comfort changes; residual remains at stale value.

### 4. Hardware thermal cycling

- Conservative bound limits amplitude, not frequency. A sequence of Δ `0,+1,0,+1` every 30 minutes still cycles the blower. GP/ridge disagreement near threshold can also flip decisions.
- Need independent dwell and hysteresis: require same nonzero residual direction for two ticks and enforce per-night write budget.

### 5. BedJet conflicts

- BedJet state is represented by an initial-bed gate / BedJet-gated body_skin, not explicit `climate.bedjet_shar`. Heat outside expected windows becomes out-of-distribution; safe_residual may return 0, leaving v5.2 to fight or ignore the BedJet.
- If BedJet intentionally heats and model sees movement/overheat reward, it may learn to cool harder during future BedJet sessions, fighting user intent.

### 6. Fail-safe on missing data

- Sensors stale → Δ=0 is good only if v5.2 plus rail are safe. For right rail-disabled or RC-off cases, Δ=0 may not be sufficient.
- Model missing/corrupt → v5.2 path. Must verify v5.2’s configuration is actually active and 3-level/RC assumptions hold.
- PG outage → no retrain; OK if old model kept. Unsafe if partial retrain writes new model. Use atomic model promotion with validation.
- Presence flapping → initial gate mutes model repeatedly; may force `-10` or suppress learned corrections unpredictably.

### 7. Override path safety

- Proposal says after an override, force Δ=0 for rest of night. That respects not adding learned behavior, but v5.2 may still ignore a right cold override after 60 minutes. The wrapper must enforce per-zone manual floors itself, not rely on v5.
- Warm override must be a hard floor against colder writes for rest of night; cold override should be a hard ceiling against warmer writes for at least the rest of night on right zone given sparse feedback.

### 8. AppDaemon crash / hang behavior

- If the subclass crashes, v5 super loop may or may not continue depending on exception placement. A try/except returning v5 is not enough for hangs. Need external heartbeat/dead-man that restores v5.2 or firmware RC after missed cycles.

---

## Cross-cutting required safety wrapper for ANY v6

### Mandatory actuator wrapper

1. **Single writer / mutex for right zone.**
   - Implement one of: merge `right_overheat_safety.py` into the controller, or create an HA helper/lease such as `input_boolean.snug_right_rail_engaged` + `input_text.snug_right_writer_owner` with compare-and-set semantics.
   - If rail engaged, v6 must not write right setting until the rail releases and a cooldown period passes.

2. **Hard bounds and cooling-only.**
   - Clamp every write to integer `[-10, 0]`.
   - Never write positive settings.
   - If a value is NaN, None, non-integer, or out-of-range: no-write and fallback.

3. **Rate limits / anti-cycling.**
   - Normal operation: max 1 L-step per 15 minutes and max 2 L-steps per 30 minutes.
   - Emergency overheat may jump to `-10` immediately.
   - After emergency release, hold at a conservative value for at least 30 minutes; do not immediately restore a warm snapshot if body is still above a caution threshold (e.g. 83–84°F right body_left).
   - Require two consecutive ticks before reversing direction.

4. **Per-zone min/max policy bounds.**
   - Left: respect user warm-floor from manual warm overrides all night.
   - Right: after any cooler override, do not write warmer than that override for the rest of the night unless user later warms it.
   - Initial-bed `-10` must not override manual changes made after the user is in bed.

5. **Dead-man timer.**
   - Every v6 tick updates a heartbeat.
   - If heartbeat stale >10 minutes while sleep_mode is on, restore deterministic v5.2 settings or re-enable firmware RC (whichever is the approved safe fallback), and alert.
   - If rail heartbeat stale while right occupied, fail closed: set right to a conservative cooling baseline or disable v6 right writes.

6. **Sensor sanity / missing data fail-safe.**
   - Reject body outside 55–110°F, room outside 50–90°F.
   - Stuck sensor: if body_left variance is near zero for >30 minutes while other body channels or room change, mark invalid.
   - Cross-channel disagreement: if center/body_max exceeds body_left by >10°F post-BedJet, treat as hazard and avoid warming decisions.
   - Missing `sensor.bedroom_temperature_sensor_temperature`: no learned/MPC comfort optimization; fallback.
   - Presence flapping: debounce occupancy and use first-onset-of-night for BedJet suppression unless explicit BedJet state says otherwise.

7. **BedJet arbitration.**
   - Log and read `climate.bedjet_shar` state/mode/target/fan.
   - If BedJet heating is active, do not infer comfort from right body sensors without contamination flags.
   - Define priority explicitly: emergency overheat rail > manual override > BedJet user intent > initial pre-cool > optimizer.

8. **Deterministic v5.2 fallback on exception.**
   - Fallback must be a verified deterministic code path, not a stale import or shadow-only path.
   - Fallback must include right-zone rail coordination and right-zone override floors; raw current v5.2 is not enough for right safety unless patched.

## Least vs most safety-bug surface area

- **Least surface area: opt-learned**, if kept as a bounded residual on top of a patched v5.2 and if Δ defaults to 0 on any uncertainty. It adds model complexity, but the actuator authority is smallest (`|Δ|≤1` initially, ≤3 ever). It is still not safe until right-rail mutex, override floors, and dead-man are implemented.
- **Most surface area: opt-mpc.** It adds a plant model, stochastic optimizer, horizon/cost weights, L_active/L1 writing, firmware RC model, and right-zone rail hard constraints. Before deploy it needs deterministic optimization or fixed seed/no-jitter behavior, formal mutex with rail, hard actuator rate limiter outside the cost function, explicit BedJet state, and proof that model error cannot suppress emergency cooling.
- **Close second: opt-scratch.** It turns RC off and takes direct responsibility for both zones. That is deployable only with a robust safety wrapper because firmware RC modulation is no longer available as an inner loop.
- **opt-hybrid** is easier to inspect than MPC/scratch but has brittle threshold priority issues; it must fix priority ordering and add hysteresis before live use.

## Safety regression tests to add to `tools/v6_eval.py`

1. **Right rail mutex test:** start right `body_left=86.5°F`, rail engaged, v6 proposes `-6`; assert no v6 write occurs and final setting remains `-10` until body_left <82°F plus cooldown.
2. **Rail release rebound test:** rail releases at 81.9°F with snapshot `-4`; v6/fallback must not restore warmer than `-7` while body_left remains above caution threshold.
3. **BedJet explicit conflict test:** `climate.bedjet_shar=heat`, first 30 min, right body sensors 95°F; assert no optimizer fights BedJet except emergency rule after suppression expires.
4. **BedJet off-window test:** BedJet heat starts at 03:00, not occupancy onset; assert contamination flag activates from climate entity, not just time window.
5. **Sensor stuck-low overheat test:** right body_left constant 79°F for 60 minutes, center/right rise to 94°F, room 74°F; assert v6 marks sensor invalid and does not warm/hold mild settings.
6. **Room unavailable warm-night test:** room sensor unavailable, body_left rising; assert fallback does not remove cooling because `room_temp=None`.
7. **Presence flap test:** occupied on/off/on within 5 minutes at 03:00; assert BedJet suppression and initial-cool do not restart unless explicit BedJet/pre-bed state says so.
8. **Manual warm override floor test:** user changes left `-8 → -3`; for rest of night, non-emergency v6 never writes `< -3`.
9. **Manual cold override floor test (right):** user changes right `-4 → -5`; for rest of night, non-emergency v6 never writes `> -5` unless user later warms it.
10. **Rapid proposal sequence test:** optimizer raw proposals `[-5,-8,-5,-8,-6]` over 25 minutes; actuator wrapper must emit at most one direction change and obey step-rate limits.
11. **L_active/L1 test:** 3-level mode on, active dial L2, v6 writes L1; assert test fails. Passing behavior must either write L2 or force 3-level off before control.
12. **Positive-output test:** model/optimizer returns `+2`, `NaN`, or `11`; assert no positive or invalid HA write is made.
13. **PG outage/model corrupt test:** simulate missing PG/model file; assert deterministic v5.2 fallback and no partial model promotion.
14. **AppDaemon dead-man test:** no v6 tick for 12 minutes while sleep_mode on; assert fallback/RC restore action fires and is logged.
15. **Right safety app down test:** `right_overheat_safety` heartbeat stale while right occupied; assert v6 disables right live writes or fails closed to conservative cooling.

## Bottom line

No optimizer should deploy live until the safety wrapper exists. The right-zone 86/82°F rail must be treated as the final authority with an explicit mutex, not as a second app that “probably” wins races. The safest near-term path is a minimal residual/shadow deployment with zero right-zone authority beyond patched v5.2 + rail, while adding the regression tests above and logging explicit BedJet state.
