#!/usr/bin/env bash
# Wire H Company computer-use agents into the Hermes worker.
# Usage: scripts/enable-hai.sh hk-your-key   (from platform.hcompany.ai/settings/api-keys)
set -euo pipefail
KEY="${1:-${HAI_API_KEY:-}}"
[ -n "$KEY" ] || { echo "usage: $0 hk-..."; exit 1; }

DEMOS="$HOME/hai/computer-use-agents-demos"
printf 'HAI_API_KEY=%s\n' "$KEY" > "$DEMOS/.env" && chmod 600 "$DEMOS/.env"

CONFIG="$HOME/.hermes/config.yaml"
if ! grep -q 'hai-agents-platform' "$CONFIG"; then
  # Hosted H agent platform: run_agent / wait_for_session / list_agents / ...
  # US endpoint: https://agp.hcompany.ai/mcp — EU (SDK default): agp.eu.hcompany.ai
  awk -v key="$KEY" '
    /^mcp_servers:$/ {
      print
      print "  hai-agents-platform:"
      print "    enabled: true"
      print "    url: https://agp.eu.hcompany.ai/mcp"
      print "    headers:"
      print "      Authorization: Bearer " key
      print "    timeout: 420"
      print "    connect_timeout: 60"
      next
    }
    { print }
  ' "$CONFIG" > "$CONFIG.tmp" && mv "$CONFIG.tmp" "$CONFIG"
  chmod 600 "$CONFIG"
  echo "hai-agents-platform added to $CONFIG"
else
  echo "hai-agents-platform already configured"
fi

systemctl --user restart followthrough-orchestrator
echo "Done. Say: 'Book the cheapest flight from SFO to Tokyo on Saturday' near the phone."
