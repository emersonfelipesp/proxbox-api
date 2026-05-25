#!/usr/bin/env sh
set -eu

GITEA_VERSION="1.23.7"

if [ "$(id -u)" -eq 0 ]; then
  SUDO=""
else
  SUDO="sudo"
fi

export DEBIAN_FRONTEND=noninteractive

$SUDO apt-get update
$SUDO apt-get install -y --no-install-recommends \
  ca-certificates \
  cloud-init \
  curl \
  git \
  qemu-guest-agent \
  sqlite3 \
  sudo

$SUDO adduser --system --shell /bin/bash --gecos 'Gitea' \
  --group --disabled-password --home /home/git git || true

$SUDO mkdir -p /var/lib/gitea/{custom,data,log}
$SUDO chown -R git:git /var/lib/gitea
$SUDO chmod -R 750 /var/lib/gitea
$SUDO mkdir -p /etc/gitea
$SUDO chown root:git /etc/gitea
$SUDO chmod 770 /etc/gitea

$SUDO curl -fsSL -o /usr/local/bin/gitea \
  "https://dl.gitea.com/gitea/${GITEA_VERSION}/gitea-${GITEA_VERSION}-linux-amd64"
$SUDO chmod +x /usr/local/bin/gitea

cat <<'UNIT' | $SUDO tee /etc/systemd/system/gitea.service >/dev/null
[Unit]
Description=Gitea (Git with a cup of tea)
After=syslog.target network.target

[Service]
RestartSec=2s
Type=simple
User=git
Group=git
WorkingDirectory=/var/lib/gitea
ExecStart=/usr/local/bin/gitea web --config /etc/gitea/app.ini
Restart=always
Environment=USER=git HOME=/home/git GITEA_WORK_DIR=/var/lib/gitea

[Install]
WantedBy=multi-user.target
UNIT

$SUDO systemctl daemon-reload
$SUDO systemctl enable gitea >/dev/null 2>&1 || true
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
