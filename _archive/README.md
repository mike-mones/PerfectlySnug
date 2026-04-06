# Archived Code

Dead code moved here to declutter the project while preserving git history.
Everything in this directory is **not in use** and should not be imported.

## What's here and why

| File | Reason archived |
|------|----------------|
| `ml/controller.py` | Parallel PID controller — never connected to AppDaemon |
| `ml/serve.py` | HTTP prediction server — never called by any integration |
| `ml/train.py` | Manual CLI training script — never automated |
| `ml/train_stage_classifier.py` | Sleep stage classifier — garbage training data, results unusable |
| `ml/state/controller_state.json` | State file for the dead `ml/controller.py` |
| `ml/state/recovered_training_data.json` | Only 23 samples, mostly invalid |
| `config/ha_apple_health_automation.json` | v1 automation with wrong field names — silently drops all data |
| `appdaemon/sleep_controller.py` | v1 controller, replaced by v3 |
| `appdaemon/sleep_controller_v2.py` | v2 controller, replaced by v3 |
| `tests/test_controller_v2.py` | Tests for the dead v2 controller |

## Tools that referenced archived code

These tools in `tools/` still contain imports from archived modules:

- **`tools/test_controller_smoke.py`** — smoke test for v2 controller (will fail at import)
- **`tools/backtest_controller.py`** — imports `ml.controller` (will fail at import)
- **`tools/simulate_night.py`** — imports `ml.controller` but handles `ImportError` gracefully

## What's still active (do NOT archive)

- `ml/sleep_curve.py` — science-backed temperature curves
- `ml/schema.py` — data schema reference
- `appdaemon/sleep_controller_v3.py` — active controller
- `tests/test_controller_v3.py` — active tests
- `custom_components/` — active HA integration
- `health_receiver/` — pending deployment
- `config/apple_health_automation_v2.yaml` — active v2 automation
- `tools/` — diagnostic tools
- `server.py` — manual debugging server
