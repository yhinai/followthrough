#!/usr/bin/env bash
set -euo pipefail
duration="${1:-75}"
output="${2:-data/reports/followthrough-demo-$(date +%Y%m%d-%H%M%S).mp4}"
mkdir -p "$(dirname "$output")"
ffmpeg -y -f x11grab -framerate 30 -video_size 1280x720 -i :99.0 -t "$duration" -c:v libx264 -preset veryfast -crf 22 -pix_fmt yuv420p "$output"
printf '%s\n' "$output"
