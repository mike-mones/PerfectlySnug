#!/usr/bin/env bash
# Daily auto-retune for the PerfectlySnug smart-baseline constants.
#
# Re-fits cycle baselines and room-comp from the latest controller_readings
# in PostgreSQL and updates ml/state/fitted_baselines.json. The smart_baseline
# function in ml/features.py picks up the new constants on the next import
# (e.g., next AppDaemon reload) — no service restart required for the fit
# itself, only for the controller to pick up the new file.
#
# Schedule via cron (recommended: shortly after morning wake):
#
#   30 9 * * *  cd /Users/mike/HomeAssistant/PerfectlySnug && \
#               ./tools/auto_retune.sh >> /var/log/snug_retune.log 2>&1
#
# Or via Home Assistant shell_command + automation if running on the HA host.
#
# Idempotent: safe to run repeatedly. Only writes the JSON if the fit succeeds.
set -euo pipefail

cd "$(dirname "$0")/.."
REPO_ROOT="$(pwd)"

if [[ ! -x "$REPO_ROOT/.venv/bin/python" ]]; then
    echo "ERROR: $REPO_ROOT/.venv/bin/python not found." >&2
    echo "Bootstrap with: python3 -m venv .venv && .venv/bin/pip install -r ml/requirements.txt" >&2
    exit 1
fi

OUT="$REPO_ROOT/ml/state/fitted_baselines.json"
TMP="$(mktemp -t snug_fit.XXXXXX.json)"
trap 'rm -f "$TMP"' EXIT

# Run the fitter, capturing stderr for diagnostics
if ! "$REPO_ROOT/.venv/bin/python" tools/fit_baselines.py > "$TMP.stdout" 2>&1; then
    echo "[$(date -Iseconds)] auto-retune FAILED:" >&2
    cat "$TMP.stdout" >&2
    rm -f "$TMP.stdout"
    exit 1
fi

# fit_baselines.py writes directly to OUT; verify it's valid JSON
if ! "$REPO_ROOT/.venv/bin/python" -c "import json; json.load(open('$OUT'))" 2>/dev/null; then
    echo "[$(date -Iseconds)] auto-retune produced invalid JSON at $OUT" >&2
    cat "$TMP.stdout" >&2
    rm -f "$TMP.stdout"
    exit 1
fi

# Log a one-line summary
N_CYCLES=$("$REPO_ROOT/.venv/bin/python" -c "
import json
p = json.load(open('$OUT'))
diag = p.get('cycle_diagnostics', {})
total = sum(d.get('n_overrides', 0) for d in diag.values())
print(total)
")
echo "[$(date -Iseconds)] auto-retune OK — fitted from $N_CYCLES total override events"
rm -f "$TMP.stdout"
