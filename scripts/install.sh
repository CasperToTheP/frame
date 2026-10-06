#!/usr/bin/env bash
# Install or upgrade Frame on Raspberry Pi OS Lite (64-bit).
#
#   sudo ./scripts/install.sh [--hostname NAME] [--no-boot-config]
#
# Safe to re-run: it upgrades the application in /opt/frame and never touches
# uploaded media or saved settings in /var/lib/frame.
set -euo pipefail

APP_DIR=/opt/frame
DATA_DIR=/var/lib/frame
FRAME_USER=frame
SET_HOSTNAME=""
BOOT_CONFIG=1

usage() {
  sed -n '2,8p' "$0" | sed 's/^# \{0,1\}//'
  cat <<'EOF'
Options:
  --hostname NAME    set the Pi's hostname (the UI is then at http://NAME.local:8080).
                     By default the hostname is changed to "frame" only if it is
                     still the Raspberry Pi OS default "raspberrypi".
  --no-boot-config   don't hide the console/cursor/splash at boot
                     (leaves cmdline.txt, config.txt and getty@tty1 untouched).
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --hostname) SET_HOSTNAME="${2:-}"; shift 2 ;;
    --no-boot-config) BOOT_CONFIG=0; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown option: $1" >&2; usage; exit 2 ;;
  esac
done

step() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
info() { printf '    %s\n' "$*"; }
warn() { printf '    \033[33mWARNING:\033[0m %s\n' "$*"; }

if [[ $EUID -ne 0 ]]; then
  echo "Please run with sudo: sudo ./scripts/install.sh" >&2
  exit 1
fi
if ! command -v apt-get >/dev/null; then
  echo "This installer is for Raspberry Pi OS / Debian (apt not found)." >&2
  exit 1
fi

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [[ ! -f "$REPO_DIR/frame/web.py" ]]; then
  echo "Run this from a checkout of the frame repository." >&2
  exit 1
fi

MODEL="$(tr -d '\0' 2>/dev/null </proc/device-tree/model || echo unknown)"
step "Installing Frame on: $MODEL"
info "source: $REPO_DIR"

# --- packages -----------------------------------------------------------------
step "Installing system packages (mpv, Python, Avahi, ALSA tools, SVG renderer, hotspot)"
export DEBIAN_FRONTEND=noninteractive
apt-get update -q
apt-get install -y -q --no-install-recommends \
  mpv python3 python3-venv avahi-daemon alsa-utils rsync ca-certificates librsvg2-bin   dnsmasq-base iw
info "$(mpv --version | head -n1)"

PY_OK=$(python3 -c 'import sys; print(int(sys.version_info >= (3, 11)))')
if [[ "$PY_OK" != 1 ]]; then
  echo "Python 3.11+ is required (found $(python3 --version)). Use Raspberry Pi OS Bookworm or newer." >&2
  exit 1
fi

# --- user and groups ----------------------------------------------------------
step "Creating service user '$FRAME_USER'"
if ! id "$FRAME_USER" >/dev/null 2>&1; then
  useradd --system --user-group --home-dir "$DATA_DIR" --no-create-home \
    --shell /usr/sbin/nologin "$FRAME_USER"
  info "created user $FRAME_USER"
else
  info "user exists"
fi
# video: /dev/dri/card* (display)  render: /dev/dri/renderD* (GPU)  audio: /dev/snd/*
for group in video render audio; do
  if getent group "$group" >/dev/null; then
    usermod -aG "$group" "$FRAME_USER"
  else
    warn "group '$group' does not exist; skipping"
  fi
done
info "groups: $(id -nG "$FRAME_USER")"

# --- directories --------------------------------------------------------------
step "Creating data directories (existing media and settings are kept)"
install -d -o "$FRAME_USER" -g "$FRAME_USER" -m 0750 \
  "$DATA_DIR" "$DATA_DIR/media" "$DATA_DIR/media/.incoming" "$DATA_DIR/tmp"
# Fix ownership of files copied in by hand (e.g. scp as root). Never deletes.
chown -R "$FRAME_USER:$FRAME_USER" "$DATA_DIR/media"
[[ -f $DATA_DIR/state.json ]] && chown "$FRAME_USER:$FRAME_USER" "$DATA_DIR/state.json"
install -d -m 0755 "$APP_DIR"
install -m 0644 "$REPO_DIR/systemd/frame-tmpfiles.conf" /etc/tmpfiles.d/frame.conf
systemd-tmpfiles --create /etc/tmpfiles.d/frame.conf
info "media:    $DATA_DIR/media ($(find "$DATA_DIR/media" -maxdepth 1 -type f | wc -l) files)"
info "settings: $DATA_DIR/state.json"

# --- application --------------------------------------------------------------
step "Installing application to $APP_DIR"
rsync -a --delete --exclude '__pycache__' \
  "$REPO_DIR/frame" "$REPO_DIR/pyproject.toml" "$REPO_DIR/requirements.txt" \
  "$REPO_DIR/README.md" "$APP_DIR/app/"
install -d "$APP_DIR/scripts"
install -m 0755 "$REPO_DIR/scripts/doctor.sh" "$REPO_DIR/scripts/uninstall.sh" "$APP_DIR/scripts/"
ln -sf "$APP_DIR/scripts/doctor.sh" /usr/local/bin/frame-doctor
chown -R root:root "$APP_DIR"

if [[ ! -x "$APP_DIR/venv/bin/python" ]]; then
  python3 -m venv "$APP_DIR/venv"
  info "created virtualenv"
fi
"$APP_DIR/venv/bin/pip" install -q --disable-pip-version-check -r "$APP_DIR/app/requirements.txt"
"$APP_DIR/venv/bin/pip" install -q --disable-pip-version-check --no-deps --force-reinstall "$APP_DIR/app"
"$APP_DIR/venv/bin/python" -c 'import frame, flask, waitress; print("    frame", frame.__version__)'

if [[ ! -f /etc/default/frame ]]; then
  cat >/etc/default/frame <<'EOF'
# Overrides for Frame (read by frame-player and frame-web). Restart after editing:
#   sudo systemctl restart frame-player frame-web
#FRAME_PORT=8080
#FRAME_MAX_UPLOAD_MB=4096
#FRAME_LOG_LEVEL=INFO
EOF
  info "created /etc/default/frame"
fi
if ! grep -q '^FRAME_HOTSPOT_PASSWORD=' /etc/default/frame; then
  HOTSPOT_PW="$(python3 -c 'import secrets; a = "abcdefghjkmnpqrstuvwxyz23456789"; print("".join(secrets.choice(a) for _ in range(10)))')"
  cat >>/etc/default/frame <<EOF

# The frame's own Wi-Fi, started when the home Wi-Fi can't be reached. Change the
# password here (8-63 characters), then re-run the installer.
FRAME_HOTSPOT_SSID=Frame
FRAME_HOTSPOT_PASSWORD=$HOTSPOT_PW
EOF
  info "generated a password for the Wi-Fi hotspot"
fi
# It holds the hotspot password. systemd reads it as root.
chmod 0600 /etc/default/frame

# --- systemd ------------------------------------------------------------------
step "Installing systemd services"
install -m 0644 "$REPO_DIR/systemd/frame-player.service" /etc/systemd/system/
install -m 0644 "$REPO_DIR/systemd/frame-web.service" /etc/systemd/system/
install -m 0644 "$REPO_DIR/systemd/frame-netwatch.service" /etc/systemd/system/
systemctl daemon-reload
systemctl enable frame-player.service frame-web.service frame-netwatch.service
for svc in frame-player frame-web; do
  if systemctl is-active --quiet "$svc"; then
    systemctl restart "$svc"
    info "restarted $svc (upgrade)"
  fi
done

# --- Wi-Fi hotspot fallback ------------------------------------------------------
step "Configuring the Wi-Fi hotspot (used when the home Wi-Fi is out of reach)"
HOTSPOT_SSID="$(. /etc/default/frame; echo "${FRAME_HOTSPOT_SSID:-Frame}")"
HOTSPOT_PW="$(. /etc/default/frame; echo "${FRAME_HOTSPOT_PASSWORD:-}")"
HOTSPOT_OK=0
WIFI_DEV=""
if command -v nmcli >/dev/null && systemctl is-active --quiet NetworkManager; then
  WIFI_DEV="$(nmcli -t -f DEVICE,TYPE device | awk -F: '$2 == "wifi" {print $1; exit}')"
fi
if [[ -z "$WIFI_DEV" ]]; then
  warn "no Wi-Fi device managed by NetworkManager; skipping the hotspot"
elif (( ${#HOTSPOT_PW} < 8 || ${#HOTSPOT_PW} > 63 )); then
  warn "FRAME_HOTSPOT_PASSWORD in /etc/default/frame must be 8-63 characters; skipping the hotspot"
else
  # Autoconnect off: only frame-netwatch turns it on. 2.4 GHz reaches furthest.
  # The Pi's Wi-Fi chip doesn't do PMF as an access point, so it is disabled.
  HOTSPOT_PROPS=(connection.interface-name "$WIFI_DEV" connection.autoconnect no
    802-11-wireless.ssid "$HOTSPOT_SSID" 802-11-wireless.mode ap 802-11-wireless.band bg
    ipv4.method shared ipv6.method disabled
    wifi-sec.key-mgmt wpa-psk wifi-sec.proto rsn wifi-sec.pairwise ccmp wifi-sec.group ccmp
    wifi-sec.pmf disable wifi-sec.psk "$HOTSPOT_PW")
  # Modify rather than recreate, so a phone connected to the hotspot (maybe the
  # one running this installer over SSH) stays connected.
  if nmcli -t -f NAME connection show | grep -qx frame-hotspot; then
    nmcli connection modify frame-hotspot "${HOTSPOT_PROPS[@]}"
  else
    nmcli connection add type wifi con-name frame-hotspot "${HOTSPOT_PROPS[@]}" >/dev/null
  fi
  HOTSPOT_OK=1
  info "hotspot \"$HOTSPOT_SSID\" on $WIFI_DEV (web UI there: http://10.42.0.1:8080)"
fi
systemctl restart frame-netwatch.service

# --- mDNS (frame.local) ------------------------------------------------------
step "Configuring mDNS (Avahi)"
CURRENT_HOST="$(hostname)"
if [[ -z "$SET_HOSTNAME" && "$CURRENT_HOST" == "raspberrypi" ]]; then
  SET_HOSTNAME=frame
fi
if [[ -n "$SET_HOSTNAME" && "$SET_HOSTNAME" != "$CURRENT_HOST" ]]; then
  if [[ ! "$SET_HOSTNAME" =~ ^[a-zA-Z0-9]([a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?$ ]]; then
    echo "invalid hostname: $SET_HOSTNAME" >&2
    exit 2
  fi
  hostnamectl set-hostname "$SET_HOSTNAME"
  if grep -q '^127\.0\.1\.1' /etc/hosts; then
    sed -i "s/^127\.0\.1\.1.*/127.0.1.1\t$SET_HOSTNAME/" /etc/hosts
  else
    printf '127.0.1.1\t%s\n' "$SET_HOSTNAME" >>/etc/hosts
  fi
  info "hostname changed: $CURRENT_HOST -> $SET_HOSTNAME"
  CURRENT_HOST="$SET_HOSTNAME"
fi
install -d /etc/avahi/services
install -m 0644 "$REPO_DIR/systemd/frame.avahi-service" /etc/avahi/services/frame.service
systemctl enable avahi-daemon.service >/dev/null 2>&1 || true
systemctl restart avahi-daemon.service || warn "could not restart avahi-daemon"

# Wi-Fi power saving makes the Pi slow to answer (or drop off) mDNS and the web
# UI. Turn it off for NetworkManager-managed Wi-Fi (Raspberry Pi OS Bookworm+).
if [[ -d /etc/NetworkManager/conf.d ]]; then
  cat >/etc/NetworkManager/conf.d/frame-wifi-powersave.conf <<'EOF'
# Added by Frame installer: keep Wi-Fi responsive for the management UI.
[connection]
wifi.powersave = 2
EOF
  info "disabled Wi-Fi power saving (takes effect after reboot)"
fi

# --- boot appearance ------------------------------------------------------------
if [[ $BOOT_CONFIG -eq 1 ]]; then
  step "Configuring a quiet boot (no console text, cursor or rainbow splash)"
  BOOT=/boot/firmware
  [[ -f $BOOT/cmdline.txt ]] || BOOT=/boot
  if [[ -f $BOOT/cmdline.txt ]]; then
    [[ -f $BOOT/cmdline.txt.frame-backup ]] || cp "$BOOT/cmdline.txt" "$BOOT/cmdline.txt.frame-backup"
    CMDLINE="$(head -n1 "$BOOT/cmdline.txt")"
    for token in consoleblank=0 vt.global_cursor_default=0 logo.nologo quiet; do
      if [[ " $CMDLINE " != *" $token "* ]]; then
        CMDLINE="$CMDLINE $token"
        info "cmdline.txt: added $token"
      fi
    done
    # cmdline.txt must stay a single line.
    printf '%s\n' "$CMDLINE" >"$BOOT/cmdline.txt"
  else
    warn "cmdline.txt not found; skipping"
  fi
  if [[ -f $BOOT/config.txt ]]; then
    if ! grep -q '^disable_splash=1' "$BOOT/config.txt"; then
      [[ -f $BOOT/config.txt.frame-backup ]] || cp "$BOOT/config.txt" "$BOOT/config.txt.frame-backup"
      printf '\n# Added by Frame installer: no rainbow splash at boot\ndisable_splash=1\n' >>"$BOOT/config.txt"
      info "config.txt: added disable_splash=1"
    fi
    if ! grep -Eq '^dtoverlay=vc4-kms-v3d' "$BOOT/config.txt"; then
      warn "config.txt has no 'dtoverlay=vc4-kms-v3d'. Frame needs the KMS display driver"
      warn "(the Raspberry Pi OS default). Add it under [all] and reboot."
    fi
  fi
  # The player owns the HDMI screen; no login prompt on it. (SSH is unaffected.)
  systemctl disable getty@tty1.service >/dev/null 2>&1 || true
  info "disabled login prompt on the HDMI console (tty1)"
fi

# --- done -----------------------------------------------------------------------
IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
step "Frame installed successfully"
cat <<EOF

    Reboot now:            sudo reboot
    Then open:             http://$CURRENT_HOST.local:8080
    or by IP address:      http://${IP:-<pi-ip-address>}:8080
    Diagnostics:           frame-doctor
    Logs:                  journalctl -u frame-player -u frame-web -f

    The web UI has no password. Use it only on a trusted home network.

EOF
if [[ $HOTSPOT_OK -eq 1 ]]; then
  cat <<EOF
    No home Wi-Fi in reach? After about 1.5 minutes the frame starts its own:
      Wi-Fi network:       $HOTSPOT_SSID
      Password:            $HOTSPOT_PW
      Then open:           http://10.42.0.1:8080
    (The password is in /etc/default/frame and in the web UI under Display > Advanced.)

EOF
fi
