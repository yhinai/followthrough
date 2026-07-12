#!/usr/bin/env bash
set -euo pipefail

display="${DISPLAY:-:99}"
profile="${FOLLOWTHROUGH_DESKTOP_PROFILE:-/home/alhinai/browser-profiles/followthrough-desktop}"
mkdir -p "$profile"

cleanup() {
  trap - EXIT INT TERM
  kill "${vnc_pid:-}" "${browser_pid:-}" "${wm_pid:-}" "${xvfb_pid:-}" 2>/dev/null || true
  wait 2>/dev/null || true
}
trap cleanup EXIT INT TERM

Xvfb "$display" -screen 0 1280x720x24 -nolisten tcp -ac &
xvfb_pid=$!
for _ in $(seq 1 50); do
  DISPLAY="$display" xdpyinfo >/dev/null 2>&1 && break
  sleep 0.1
done

DISPLAY="$display" openbox --sm-disable &
wm_pid=$!
sleep 0.5

DISPLAY="$display" /snap/bin/chromium \
  --no-sandbox --disable-gpu --disable-dev-shm-usage \
  --disable-session-crashed-bubble --no-first-run --start-maximized \
  --user-data-dir="$profile" https://example.com &
browser_pid=$!

x11vnc -display "$display" -rfbport 5901 -localhost -forever -shared -nopw -quiet &
vnc_pid=$!

wait -n "$xvfb_pid" "$wm_pid" "$browser_pid" "$vnc_pid"
