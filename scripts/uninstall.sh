#!/usr/bin/env bash
# Remove Frame. Uploaded media and settings in /var/lib/frame are KEPT unless
# --purge is given.
#
#   sudo ./scripts/uninstall.sh [--purge]
set -euo pipefail

PURGE=0
[[ "${1:-}" == "--purge" ]] && PURGE=1

if [[ $EUID -ne 0 ]]; then
  echo "Please run with sudo." >&2
  exit 1
fi

echo "Stopping and removing services..."
systemctl disable --now frame-player.service frame-web.service frame-netwatch.service   2>/dev/null || true
rm -f /etc/systemd/system/frame-player.service /etc/systemd/system/frame-web.service   /etc/systemd/system/frame-netwatch.service
systemctl daemon-reload
# The fallback hotspot (NetworkManager then rejoins the home Wi-Fi by itself).
nmcli connection delete frame-hotspot >/dev/null 2>&1 || true

rm -f /etc/tmpfiles.d/frame.conf /etc/avahi/services/frame.service \
  /etc/NetworkManager/conf.d/frame-wifi-powersave.conf \
  /etc/systemd/journald.conf.d/90-frame.conf
rm -rf /run/frame
rm -f /usr/local/bin/frame-doctor
rm -rf /opt/frame

# Give the HDMI console its login prompt back.
systemctl enable getty@tty1.service 2>/dev/null || true

if [[ $PURGE -eq 1 ]]; then
  echo "Purging media and settings in /var/lib/frame..."
  rm -rf /var/lib/frame
  userdel frame 2>/dev/null || true
  rm -f /etc/default/frame
else
  echo "Kept media and settings in /var/lib/frame (use --purge to delete them)."
fi

for BOOT in /boot/firmware /boot; do
  if [[ -f $BOOT/cmdline.txt.frame-backup ]]; then
    echo "Boot settings were changed by the installer. Originals are saved as:"
    echo "  $BOOT/cmdline.txt.frame-backup"
    [[ -f $BOOT/config.txt.frame-backup ]] && echo "  $BOOT/config.txt.frame-backup"
    echo "Copy them back over cmdline.txt / config.txt if you want the old boot screen."
    break
  fi
done
echo "Frame removed."
