#!/usr/bin/env bash
# Resume / complete the BedJet HA config flow once the BedJet is powered on.
# Usage: bash bedjet_finish_setup.sh [BLE_MAC]
# Default MAC was discovered during install: EC:62:60:B5:EE:8E
set -e
MAC="${1:-EC:62:60:B5:EE:8E}"
TOKEN=$(ssh root@192.168.0.106 'cat /config/.ha_token')
HOST="http://192.168.0.106:8123"

echo ">>> Initiating BedJet config flow..."
INIT=$(curl -sk -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"handler":"bedjet","show_advanced_options":false}' \
  "$HOST/api/config/config_entries/flow")
FLOW_ID=$(echo "$INIT" | grep -oE '"flow_id":"[^"]+"' | head -1 | cut -d'"' -f4)
echo "    flow_id=$FLOW_ID"
[ -z "$FLOW_ID" ] && { echo "Failed to start flow: $INIT"; exit 1; }

echo ">>> Submitting address $MAC (real BLE connect; can take 10-30s)..."
RESULT=$(curl -sk --max-time 90 -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d "{\"address\":\"$MAC\"}" \
  "$HOST/api/config/config_entries/flow/$FLOW_ID")
echo "$RESULT" | head -c 1000; echo
if echo "$RESULT" | grep -q '"type":"create_entry"'; then
  echo "✅ BedJet added. Listing entities..."
  sleep 5
  curl -sk -H "Authorization: Bearer $TOKEN" "$HOST/api/states" \
    | grep -oE '"entity_id":"[^"]*bedjet[^"]*"' | sort -u
elif echo "$RESULT" | grep -q 'cannot_connect'; then
  echo "❌ cannot_connect — BedJet is advertising but refusing connections."
  echo "   Power the BedJet ON (press any mode button on its remote), wait 10s, re-run."
  exit 2
fi
unset TOKEN
