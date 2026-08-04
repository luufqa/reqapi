#!/usr/bin/env bash
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SERVICE_NAME="reqapi"
SERVICE_USER="reqapi"
HOST="127.0.0.1"
PORT="8765"
PYTHON_BIN="${PYTHON_BIN:-python3}"
INSTALL_HEALTHCHECK="yes"

usage() {
  cat <<USAGE
Usage: sudo ./scripts/install_vm.sh [options]

Options:
  --app-dir PATH          Project directory. Default: current project root.
  --service-name NAME     systemd service name. Default: reqapi.
  --user USER             Linux service user. Default: reqapi.
  --host HOST             Bind host. Default: 127.0.0.1.
  --port PORT             Bind port. Default: 8765.
  --no-healthcheck        Do not install healthcheck timer.
  -h, --help              Show this help.

The script preserves APP_DIR/data and does not remove runtime data.
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --app-dir)
      APP_DIR="$2"
      shift 2
      ;;
    --service-name)
      SERVICE_NAME="$2"
      shift 2
      ;;
    --user)
      SERVICE_USER="$2"
      shift 2
      ;;
    --host)
      HOST="$2"
      shift 2
      ;;
    --port)
      PORT="$2"
      shift 2
      ;;
    --no-healthcheck)
      INSTALL_HEALTHCHECK="no"
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown option: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

if [[ "$(id -u)" -ne 0 ]]; then
  echo "Run with sudo: sudo ./scripts/install_vm.sh" >&2
  exit 1
fi

if [[ ! -f "$APP_DIR/requirements.txt" || ! -d "$APP_DIR/reqapi" ]]; then
  echo "APP_DIR does not look like a REQAPI project: $APP_DIR" >&2
  exit 1
fi

APP_DIR="$(cd "$APP_DIR" && pwd)"
VENV_DIR="$APP_DIR/.venv"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
HEALTHCHECK_SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}-healthcheck.service"
HEALTHCHECK_TIMER_FILE="/etc/systemd/system/${SERVICE_NAME}-healthcheck.timer"

echo "Installing REQAPI from $APP_DIR"
echo "Service: $SERVICE_NAME"
echo "Runtime user: $SERVICE_USER"
echo "Bind: $HOST:$PORT"
echo

if ! id "$SERVICE_USER" >/dev/null 2>&1; then
  useradd --system --home "$APP_DIR" --shell /usr/sbin/nologin "$SERVICE_USER"
fi

mkdir -p "$APP_DIR/data"

if [[ ! -d "$VENV_DIR" ]]; then
  "$PYTHON_BIN" -m venv "$VENV_DIR"
fi

"$VENV_DIR/bin/python" -m pip install --upgrade pip
"$VENV_DIR/bin/python" -m pip install -r "$APP_DIR/requirements.txt"

chown -R "$SERVICE_USER:$SERVICE_USER" "$APP_DIR"
chmod 700 "$APP_DIR/data"

cat > "$SERVICE_FILE" <<SERVICE
[Unit]
Description=REQAPI
After=network.target

[Service]
Type=simple
WorkingDirectory=$APP_DIR
ExecStart=$VENV_DIR/bin/python -m reqapi --host $HOST --port $PORT
Restart=always
RestartSec=3
User=$SERVICE_USER
Group=$SERVICE_USER

[Install]
WantedBy=multi-user.target
SERVICE

if [[ "$INSTALL_HEALTHCHECK" == "yes" ]]; then
  cat > "$HEALTHCHECK_SERVICE_FILE" <<SERVICE
[Unit]
Description=REQAPI healthcheck

[Service]
Type=oneshot
ExecStart=/bin/sh -c '$VENV_DIR/bin/python $APP_DIR/scripts/healthcheck.py --url http://$HOST:$PORT/api/me || systemctl restart $SERVICE_NAME'
SERVICE

  cat > "$HEALTHCHECK_TIMER_FILE" <<TIMER
[Unit]
Description=Run REQAPI healthcheck every minute

[Timer]
OnBootSec=1min
OnUnitActiveSec=1min
Unit=${SERVICE_NAME}-healthcheck.service

[Install]
WantedBy=timers.target
TIMER
fi

systemctl daemon-reload
systemctl enable --now "$SERVICE_NAME"

if [[ "$INSTALL_HEALTHCHECK" == "yes" ]]; then
  systemctl enable --now "${SERVICE_NAME}-healthcheck.timer"
fi

echo
systemctl --no-pager --full status "$SERVICE_NAME" || true

echo
echo "REQAPI is installed."
echo "Use: systemctl restart $SERVICE_NAME"
echo "Logs: journalctl -u $SERVICE_NAME -f"
if [[ "$INSTALL_HEALTHCHECK" == "yes" ]]; then
  echo "Healthcheck timer: systemctl status ${SERVICE_NAME}-healthcheck.timer"
fi
echo "Runtime data is preserved in $APP_DIR/data"
