#!/usr/bin/env bash
# One-shot setup for a fresh Ubuntu 24.04 box. Run as root.
#
#   scp -r gl-radar root@YOUR_SERVER:/tmp/
#   ssh root@YOUR_SERVER 'bash /tmp/gl-radar/deploy/install-vps.sh'
#
# Afterwards, edit /opt/gl-radar/.env with your keys and restart:
#   systemctl restart gl-radar
set -euo pipefail

APP_DIR=/opt/gl-radar
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

echo "Installing GL Radar to ${APP_DIR}"
apt-get update -qq
apt-get install -y -qq python3-venv python3-pip rsync

id -u glradar &>/dev/null || useradd --system --home "${APP_DIR}" --shell /usr/sbin/nologin glradar
mkdir -p "${APP_DIR}"
rsync -a --exclude '.venv' --exclude 'data' --exclude '__pycache__' "${SRC_DIR}/" "${APP_DIR}/"
mkdir -p "${APP_DIR}/data"

python3 -m venv "${APP_DIR}/.venv"
"${APP_DIR}/.venv/bin/pip" install -q -r "${APP_DIR}/requirements.txt"

[ -f "${APP_DIR}/.env" ] || cp "${APP_DIR}/.env.example" "${APP_DIR}/.env"
chown -R glradar:glradar "${APP_DIR}"

cp "${APP_DIR}/deploy/gl-radar.service" /etc/systemd/system/
systemctl daemon-reload
systemctl enable gl-radar

cat <<'MSG'

Installed. Two things left:

  1. nano /opt/gl-radar/.env          add your API keys
  2. systemctl start gl-radar         start it

The dashboard binds to 127.0.0.1 only. Reach it without exposing it:

  ssh -L 8787:127.0.0.1:8787 root@YOUR_SERVER

then open http://127.0.0.1:8787 on your own machine. Do not change the bind
address to 0.0.0.0 — the dashboard has no login, and your ClickUp token and
review queue would be on the open internet.

MSG
