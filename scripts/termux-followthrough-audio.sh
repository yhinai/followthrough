#!/data/data/com.termux/files/usr/bin/bash
set -u

ENDPOINT="https://followthrough.alhinai.dev/api/webhooks/omi/audio"
DEVICE_ID="flip-termux-audio"
STATE_DIR="$HOME/.local/state/followthrough-audio"
mkdir -p "$STATE_DIR"
chmod 700 "$STATE_DIR"

trap 'termux-microphone-record -q >/dev/null 2>&1 || true; exit 0' TERM INT

while true; do
  stamp=$(date -u +%Y%m%dT%H%M%S%NZ)
  chunk="$STATE_DIR/$stamp.m4a"
  termux-microphone-record -f "$chunk" -l 5 >/dev/null 2>&1 || true
  sleep 6
  termux-microphone-record -q >/dev/null 2>&1 || true
  if [[ -s "$chunk" ]] && (( $(wc -c <"$chunk") > 1024 )); then
    code=$(curl --silent --show-error --output "$STATE_DIR/last-response.json" \
      --write-out '%{http_code}' --max-time 20 --request POST \
      "$ENDPOINT?uid=$DEVICE_ID&timestamp=$stamp" \
      --header 'Content-Type: audio/mp4' --data-binary "@$chunk" || true)
    printf '%s code=%s bytes=%s\n' "$stamp" "$code" "$(wc -c <"$chunk")" \
      >> "$STATE_DIR/receipts.log"
    chmod 600 "$STATE_DIR/receipts.log" "$STATE_DIR/last-response.json" 2>/dev/null || true
    if [[ "$code" == "202" ]]; then
      rm -f "$chunk"
    fi
  else
    printf '%s code=invalid_audio bytes=%s\n' "$stamp" "$(wc -c <"$chunk" 2>/dev/null || printf 0)" \
      >> "$STATE_DIR/receipts.log"
    rm -f "$chunk"
  fi
  sleep 1
done
