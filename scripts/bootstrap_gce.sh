#!/usr/bin/env bash
set -euo pipefail

APP_DIR=""
SERVICE_NAME="allybot"
BOT_FILE="main_bot.py"
RUN_USER=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --app-dir)
      APP_DIR="$2"
      shift 2
      ;;
    --service)
      SERVICE_NAME="$2"
      shift 2
      ;;
    --python-file)
      BOT_FILE="$2"
      shift 2
      ;;
    --run-user)
      RUN_USER="$2"
      shift 2
      ;;
    *)
      echo "Unknown arg: $1"
      exit 1
      ;;
  esac
done

if [[ -z "$APP_DIR" ]]; then
  echo "Missing --app-dir"
  exit 1
fi

if [[ $EUID -ne 0 ]]; then
  echo "Run this script as root (use sudo)."
  exit 1
fi

if [[ -z "$RUN_USER" ]]; then
  if [[ -n "${SUDO_USER:-}" ]]; then
    RUN_USER="$SUDO_USER"
  else
    RUN_USER="$(id -un)"
  fi
fi

USER_HOME="$(getent passwd "$RUN_USER" | cut -d: -f6)"
if [[ -z "$USER_HOME" ]]; then
  echo "Could not resolve home for user: $RUN_USER"
  exit 1
fi

if [[ "${APP_DIR:0:2}" == "~/" ]]; then
  APP_DIR="${USER_HOME}/${APP_DIR:2}"
fi

apt-get update
apt-get install -y python3 python3-venv

mkdir -p "$APP_DIR"
chown -R "$RUN_USER":"$RUN_USER" "$APP_DIR"

if [[ ! -f "$APP_DIR/requirements_inatividade.txt" ]]; then
  echo "requirements_inatividade.txt not found in $APP_DIR"
  exit 1
fi

if [[ ! -f "$APP_DIR/$BOT_FILE" ]]; then
  echo "$BOT_FILE not found in $APP_DIR"
  exit 1
fi

if [[ ! -d "$APP_DIR/.venv" ]]; then
  sudo -u "$RUN_USER" python3 -m venv "$APP_DIR/.venv"
fi

sudo -u "$RUN_USER" "$APP_DIR/.venv/bin/pip" install --upgrade pip
sudo -u "$RUN_USER" "$APP_DIR/.venv/bin/pip" install -r "$APP_DIR/requirements_inatividade.txt"

if [[ ! -f "$APP_DIR/.env" ]]; then
  cat <<EOF
WARNING: $APP_DIR/.env not found.
Upload your real .env file and restart the service:
sudo systemctl restart ${SERVICE_NAME}.service
EOF
fi

cat >/etc/systemd/system/${SERVICE_NAME}.service <<EOF
[Unit]
Description=Discord AllyBot Service
After=network.target

[Service]
Type=simple
User=${RUN_USER}
WorkingDirectory=${APP_DIR}
ExecStart=${APP_DIR}/.venv/bin/python ${APP_DIR}/${BOT_FILE}
Restart=always
RestartSec=5
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemd-analyze verify "/etc/systemd/system/${SERVICE_NAME}.service"
systemctl enable "${SERVICE_NAME}.service"
systemctl restart "${SERVICE_NAME}.service"

echo "Deployment finished."
echo "Check service status: sudo systemctl status ${SERVICE_NAME}.service"
echo "Check logs: sudo journalctl -u ${SERVICE_NAME}.service -f"
