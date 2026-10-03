#!/usr/bin/env bash
# Frame diagnostics. Read-only: it never changes the system. Safe to run any time.
#
#   frame-doctor            (installed shortcut)
#   sudo ./scripts/doctor.sh   (from the repository; sudo shows a bit more)

APP_DIR=/opt/frame
DATA_DIR=/var/lib/frame
SOCK=/run/frame/mpv.sock
AUDIO_SOCK=/run/frame/audio.sock

h()  { printf '\n\033[1m== %s\033[0m\n' "$*"; }
kv() { printf '  %-22s %s\n' "$1" "$2"; }
ok()   { printf '  \033[32mOK\033[0m    %s\n' "$*"; }
bad()  { printf '  \033[31mFAIL\033[0m  %s\n' "$*"; }
note() { printf '  \033[33mNOTE\033[0m  %s\n' "$*"; }
have() { command -v "$1" >/dev/null 2>&1; }

h "System"
kv "Model" "$(tr -d '\0' 2>/dev/null </proc/device-tree/model || echo unknown)"
kv "OS" "$(. /etc/os-release 2>/dev/null; echo "${PRETTY_NAME:-unknown}")"
kv "Kernel" "$(uname -r) ($(uname -m))"
kv "Hostname" "$(hostname)"
kv "IP address(es)" "$(hostname -I 2>/dev/null || echo unknown)"
kv "Uptime" "$(uptime -p 2>/dev/null || cat /proc/uptime)"
kv "Memory" "$(free -m 2>/dev/null | awk '/^Mem:/ {print $3 " MB used / " $2 " MB total, " $7 " MB available"}')"
if have vcgencmd; then
  kv "Temperature" "$(vcgencmd measure_temp 2>/dev/null | cut -d= -f2)"
  THROTTLED="$(vcgencmd get_throttled 2>/dev/null | cut -d= -f2)"
  kv "Throttled flags" "${THROTTLED:-unknown}"
  if [[ -n "$THROTTLED" && "$THROTTLED" != "0x0" ]]; then
    note "non-zero throttled flags: under-voltage or overheating happened (0x50000 = past under-voltage)."
  fi
elif [[ -r /sys/class/thermal/thermal_zone0/temp ]]; then
  kv "Temperature" "$(awk '{printf "%.1f°C", $1/1000}' /sys/class/thermal/thermal_zone0/temp)"
fi

h "Software"
kv "mpv" "$(mpv --version 2>/dev/null | head -n1 || echo 'NOT INSTALLED')"
kv "Frame venv" "$("$APP_DIR/venv/bin/python" -c 'import frame,sys; print("frame", frame.__version__, "/ python", sys.version.split()[0])' 2>/dev/null || echo 'not installed')"

h "Display (DRM/KMS)"
BOOT=/boot/firmware; [[ -f $BOOT/config.txt ]] || BOOT=/boot
if grep -Eq '^dtoverlay=vc4-kms-v3d' "$BOOT/config.txt" 2>/dev/null; then
  ok "KMS driver enabled in $BOOT/config.txt"
else
  bad "dtoverlay=vc4-kms-v3d not found in $BOOT/config.txt"
fi
FOUND=0
for c in /sys/class/drm/card*-*; do
  [[ -f "$c/status" ]] || continue
  FOUND=1
  name="${c##*/}"
  status="$(cat "$c/status")"
  mode="$(head -n1 "$c/modes" 2>/dev/null)"
  if [[ "$status" == connected ]]; then
    ok "$name: connected (preferred mode ${mode:-?})"
  else
    kv "$name" "$status"
  fi
done
[[ $FOUND -eq 1 ]] || bad "no DRM connectors found in /sys/class/drm"
ls -l /dev/dri/ 2>/dev/null | sed 's/^/  /'

h "Audio"
if have aplay; then
  aplay -l 2>/dev/null | grep '^card' | sed 's/^/  /' || bad "no ALSA playback devices"
else
  note "aplay not installed (alsa-utils)"
fi
if have mpv; then
  echo "  mpv audio devices:"
  mpv --no-config --audio-device=help 2>/dev/null | grep -E "^\s+'" | sed 's/^/  /'
fi

h "Services"
for svc in frame-player frame-web avahi-daemon; do
  active="$(systemctl is-active "$svc" 2>/dev/null)"
  enabled="$(systemctl is-enabled "$svc" 2>/dev/null)"
  if [[ "$active" == active ]]; then ok "$svc: $active, $enabled"; else bad "$svc: $active, $enabled"; fi
done
RESTARTS="$(systemctl show frame-player -p NRestarts --value 2>/dev/null)"
[[ -n "$RESTARTS" ]] && kv "player svc restarts" "$RESTARTS"

h "Player"
if [[ -r /run/frame/player.json ]]; then
  kv "Supervisor status" "$(cat /run/frame/player.json)"
fi
if [[ -S $SOCK ]]; then
  ok "IPC socket exists: $SOCK"
  PY="$APP_DIR/venv/bin/python"
  QUERY='
import json
from frame.player import MpvIpc, PlayerError
players = [
    ("Artwork player", "'"$SOCK"'",
     ["pid", "path", "pause", "idle-active", "volume", "mute", "video-rotate", "aid",
      "hwdec-current", "audio-device", "current-ao", "current-vo",
      "width", "height", "estimated-vf-fps", "frame-drop-count"]),
    ("Soundtrack player", "'"$AUDIO_SOCK"'",
     ["pid", "path", "pause", "idle-active", "volume", "mute", "audio-device", "current-ao"]),
]
for title, sock, props in players:
    print("  " + title)
    ipc = MpvIpc(sock)
    try:
        for p in props:
            print("    %-22s %s" % (p, json.dumps(ipc.get(p))))
    except PlayerError as e:
        print("    FAIL  IPC query failed:", e)
'
  if [[ -r $SOCK && -w $SOCK ]]; then
    "$PY" -c "$QUERY" 2>&1
  elif [[ $EUID -eq 0 ]]; then
    runuser -u frame -- "$PY" -c "$QUERY" 2>&1
  else
    note "run with sudo to query the player over IPC"
  fi
else
  bad "IPC socket missing ($SOCK): mpv is not running (see logs below)"
fi

h "Media and settings"
if [[ -r $DATA_DIR/state.json ]]; then
  sed 's/^/  /' "$DATA_DIR/state.json"
else
  note "no readable $DATA_DIR/state.json (run with sudo, or nothing saved yet)"
fi
if [[ -r $DATA_DIR/media ]]; then
  kv "Media files" "$(find "$DATA_DIR/media" -maxdepth 1 -type f | wc -l)"
fi
kv "Disk" "$(df -h "$DATA_DIR" 2>/dev/null | awk 'NR==2 {print $4 " free of " $2 " (" $5 " used)"}')"

h "Network"
kv "Web UI" "http://$(hostname).local:8080"
for ip in $(hostname -I 2>/dev/null); do
  [[ "$ip" == *:* ]] && continue
  kv "" "http://$ip:8080"
done
if have curl; then
  if curl -fsS -m 3 -o /dev/null http://127.0.0.1:8080/api/status; then
    ok "web UI answers on port 8080"
  else
    bad "web UI not answering on port 8080"
  fi
fi

h "Recent warnings/errors (last 15)"
journalctl -u frame-player -u frame-web -p warning -n 15 --no-pager 2>/dev/null | sed 's/^/  /' \
  || note "run with sudo (or as a member of group adm) to read logs"

echo
