#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="${HOME}/.local/share/lumus-ambient/.venv"
STATE_DIR="${HOME}/.local/state/agy-ambient"
SYSTEMD_DIR="${HOME}/.config/systemd/user"

echo "==> Creating Python virtualenv at ${VENV_DIR}"
mkdir -p "$(dirname "${VENV_DIR}")"
python3 -m venv "${VENV_DIR}"
"${VENV_DIR}/bin/pip" install --upgrade pip
"${VENV_DIR}/bin/pip" install faster-whisper edge-tts websockets aiohttp numpy

echo "==> Creating state directory at ${STATE_DIR}"
mkdir -p "${STATE_DIR}"
[ -f "${STATE_DIR}/user_profile.md" ] || cp "${REPO_DIR}/examples/state/user_profile.md.example" "${STATE_DIR}/user_profile.md"
[ -f "${STATE_DIR}/whatsapp_schedule.md" ] || cp "${REPO_DIR}/examples/state/whatsapp_schedule.md.example" "${STATE_DIR}/whatsapp_schedule.md"
[ -f "${STATE_DIR}/business_tracker.md" ] || cp "${REPO_DIR}/examples/state/business_tracker.md.example" "${STATE_DIR}/business_tracker.md"

if [ ! -f "${REPO_DIR}/token.txt" ]; then
  echo "==> Generating mobile PWA bearer token (${REPO_DIR}/token.txt)"
  python3 -c 'import secrets; print(secrets.token_hex(16))' > "${REPO_DIR}/token.txt"
  chmod 600 "${REPO_DIR}/token.txt"
fi

if [ ! -f "${REPO_DIR}/cert.pem" ] || [ ! -f "${REPO_DIR}/key.pem" ]; then
  echo "==> Generating self-signed TLS cert for mobile PWA HTTPS"
  openssl req -x509 -newkey rsa:2048 -keyout "${REPO_DIR}/key.pem" -out "${REPO_DIR}/cert.pem" \
    -days 3650 -nodes -subj "/CN=lumus-ambient" 2>/dev/null
  chmod 600 "${REPO_DIR}/key.pem"
fi

if [ ! -f "${REPO_DIR}/telegram.json" ]; then
  cp "${REPO_DIR}/examples/telegram.json.example" "${REPO_DIR}/telegram.json"
  chmod 600 "${REPO_DIR}/telegram.json"
  echo "!! Edit ${REPO_DIR}/telegram.json with your BotFather bot_token before starting the Telegram bridge."
fi

if [ -d "${REPO_DIR}/mcp-browser" ] && command -v npm >/dev/null 2>&1; then
  echo "==> Installing @playwright/mcp in mcp-browser/"
  (cd "${REPO_DIR}/mcp-browser" && npm install)
fi

echo "==> Installing systemd user units into ${SYSTEMD_DIR}"
mkdir -p "${SYSTEMD_DIR}"
cp "${REPO_DIR}/systemd/"*.service "${REPO_DIR}/systemd/"*.timer "${SYSTEMD_DIR}/"
systemctl --user daemon-reload

echo ""
echo "Done! Next steps:"
echo "  1. Put your Telegram bot token in ${REPO_DIR}/telegram.json"
echo "  2. (Optional) Merge ${REPO_DIR}/examples/GEMINI.md.example into ~/.gemini/GEMINI.md"
echo "  3. Log into WhatsApp / Keep / Gmail in dedicated Chrome profile:"
echo "     /opt/google/chrome/chrome --user-data-dir=~/.config/chrome-whatsapp --remote-debugging-port=9223 https://web.whatsapp.com/"
echo "  4. Enable & start services:"
echo "     systemctl --user enable --now agy-ambient.service agy-ambient-telegram.service agy-ambient-sleep.service"
echo "     systemctl --user enable --now agy-ambient-whatsapp.timer agy-ambient-dream.timer agy-ambient-lifeloop.timer"
