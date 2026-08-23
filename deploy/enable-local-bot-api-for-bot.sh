#!/bin/bash
set -euo pipefail

PROJECT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
DROP_IN_DIRECTORY=/etc/systemd/system/zed-whisper-bot.service.d
BOT_ENV_FILE=/etc/zed-whisper-bot/bot.env

if [ "$(id -u)" -ne 0 ]; then
    echo "Run this cutover script with sudo."
    exit 1
fi

if ! systemctl is-active --quiet telegram-bot-api.service; then
    echo "Local Telegram Bot API service is not active."
    exit 1
fi

if [ ! -f "$BOT_ENV_FILE" ] || \
   [ "$(stat -c '%U:%G:%a' "$BOT_ENV_FILE")" != "root:root:600" ]; then
    echo "$BOT_ENV_FILE must exist with owner root:root and mode 600."
    exit 1
fi

install -d -o root -g root -m 0755 "$DROP_IN_DIRECTORY"
install -o root -g root -m 0644 \
    "$PROJECT_DIR/deploy/zed-whisper-bot-local-api.conf" \
    "$DROP_IN_DIRECTORY/local-api.conf"

systemctl daemon-reload
systemctl restart zed-whisper-bot.service

echo "Zed Whisper bot switched to the Local Telegram Bot API server."
