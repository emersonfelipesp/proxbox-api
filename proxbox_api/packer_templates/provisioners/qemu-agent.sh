#!/usr/bin/env sh
set -eu

if [ "$(id -u)" -eq 0 ]; then
  SUDO=""
else
  SUDO="sudo"
fi

export DEBIAN_FRONTEND=noninteractive

$SUDO apt-get update
$SUDO apt-get install -y --no-install-recommends qemu-guest-agent
$SUDO systemctl enable qemu-guest-agent >/dev/null 2>&1 || true

$SUDO cloud-init clean --logs --machine-id >/dev/null 2>&1 || true
$SUDO truncate -s 0 /etc/machine-id || true
$SUDO rm -f /var/lib/dbus/machine-id
$SUDO ln -sf /etc/machine-id /var/lib/dbus/machine-id

$SUDO rm -f /root/.bash_history
for history_file in /home/*/.bash_history; do
  [ -e "$history_file" ] || continue
  $SUDO rm -f "$history_file"
done

$SUDO find /var/log -type f -exec truncate -s 0 {} \; >/dev/null 2>&1 || true
history -c >/dev/null 2>&1 || true
