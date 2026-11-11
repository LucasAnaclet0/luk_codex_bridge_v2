#!/bin/sh
# Run ONCE from a normal SSH session with sudo (not from the no-new-privs bridge).
# Installs two systemd units for reboot durability, without restarting V2.
set -eu
if [ "$(id -u)" -ne 0 ]; then
  echo "REQUIRES_ROOT_SSH_SESSION; no changes made."
  exit 2
fi
BASE=/home/ubuntu/luk_codex_bridge_v2/luk_autopilot_v2/systemd
for name in luk-autopilot-v2-monitor.service luk-autopilot-v2-jobs.service; do
  [ -f "$BASE/$name" ] && [ ! -L "$BASE/$name" ] || exit 3
  /usr/bin/install -o root -g root -m 0644 "$BASE/$name" "/etc/systemd/system/$name"
done
/usr/bin/systemctl daemon-reload
/usr/bin/systemctl enable luk-autopilot-v2-monitor.service luk-autopilot-v2-jobs.service
echo 'AUTOPILOT_SYSTEMD_ENABLED_ON_NEXT_BOOT; current screen daemons unchanged.'
