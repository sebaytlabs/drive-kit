#!/usr/bin/env bash
# Record the screen or one window on Linux (X11): matches the display refresh rate, hardware H.264 (VAAPI)
# when the graphics card has it (plain x264 otherwise), about 20 Mbps, desktop audio included.
# Writes Matroska, so a crash still leaves a playable file.
# Usage: record.sh [--window | --full] [--no-audio] [--out DIR]     Stop with q or Ctrl-C.
# Default folder: $RECORD_DIR, else ~/Videos. Needs ffmpeg, xrandr, xwininfo and pactl.
set -euo pipefail

mode=window audio=1
out=${RECORD_DIR:-$HOME/Videos}
while [ $# -gt 0 ]; do
  case $1 in
    --window) mode=window ;;
    --full) mode=full ;;
    --no-audio) audio=0 ;;
    --out) out=$2; shift ;;
    -h|--help) sed -n '2,6p' "$0"; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
  shift
done

# Match the monitor's refresh rate (recording faster only duplicates frames); cap at 60.
rate=$(xrandr --current | awk '/\*/{for(i=2;i<=NF;i++)if($i~/\*/){sub(/[*+]/,"",$i);print int($i+.5);exit}}')
rate=${rate:-60}; [ "$rate" -gt 60 ] && rate=60

if [ "$mode" = window ]; then
  echo "Click the window to record..." >&2
  info=$(xwininfo)
  x=$(awk '/Absolute upper-left X/{print $4}' <<<"$info"); y=$(awk '/Absolute upper-left Y/{print $4}' <<<"$info")
  w=$(awk '/Width:/{print $2}' <<<"$info");               h=$(awk '/Height:/{print $2}' <<<"$info")
  x=$(( x < 0 ? 0 : x )); y=$(( y < 0 ? 0 : y )); w=$(( w / 2 * 2 )); h=$(( h / 2 * 2 ))  # encoder wants even sizes
  size=${w}x${h} offset=+$x,$y
else
  size=$(xrandr --current | awk '/\*/{print $1; exit}') offset=+0,0
fi

audio_in=() audio_enc=(-an)
if [ "$audio" = 1 ]; then
  audio_in=(-thread_queue_size 1024 -f pulse -i "$(pactl get-default-sink).monitor")
  audio_enc=(-c:a aac -b:a 160k)
fi

mkdir -p "$out"
file=$out/recording_$(date +%Y%m%d-%H%M%S).mkv
echo "Recording $size @ ${rate} fps to $file  (q or Ctrl-C to stop)" >&2
if [ -e /dev/dri/renderD128 ] && ffmpeg -hide_banner -encoders 2>/dev/null | grep -q h264_vaapi; then
  video=(-vaapi_device /dev/dri/renderD128 -vf format=nv12,hwupload -c:v h264_vaapi -profile:v high
         -rc_mode VBR -b:v 20M -maxrate 24M)
else
  video=(-c:v libx264 -preset veryfast -b:v 20M -maxrate 24M -bufsize 40M -pix_fmt yuv420p)
fi
exec ffmpeg -hide_banner -loglevel warning -stats \
  -f x11grab -framerate "$rate" -video_size "$size" -i "${DISPLAY:-:0}$offset" \
  "${audio_in[@]}" "${video[@]}" "${audio_enc[@]}" "$file"
