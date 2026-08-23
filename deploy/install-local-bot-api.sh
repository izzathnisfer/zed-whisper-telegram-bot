#!/bin/bash
set -euo pipefail

PROJECT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
CREDENTIAL_FILE=/etc/zed-whisper-bot/telegram-bot-api.env
DATA_DIRECTORY=/var/lib/telegram-bot-api
BOT_USER=ubuntu

if [ "$(id -u)" -ne 0 ]; then
    echo "Run this installer with sudo."
    exit 1
fi

if [ ! -f "$CREDENTIAL_FILE" ]; then
    echo "Missing credential file: $CREDENTIAL_FILE"
    exit 1
fi

if [ "$(stat -c '%U:%G:%a' "$CREDENTIAL_FILE")" != "root:root:600" ]; then
    echo "Credential file must be owned by root:root with mode 600."
    exit 1
fi

for command in docker setfacl systemctl; do
    if ! command -v "$command" >/dev/null 2>&1; then
        echo "Missing required command: $command"
        exit 1
    fi
done

# The container runs Telegram's server as UID/GID 101. The bot user needs
# directory traversal, read, and delete access so each cached incoming media
# file can be unlinked immediately after it is copied to the private workspace.
# No access is granted to other local accounts.
install -d -o 101 -g 101 -m 0750 "$DATA_DIRECTORY"
setfacl -R -m "u:${BOT_USER}:rwX" "$DATA_DIRECTORY"
find "$DATA_DIRECTORY" -type d -exec \
    setfacl -m "d:u:${BOT_USER}:rwx" {} +

install -o root -g root -m 0644 \
    "$PROJECT_DIR/deploy/telegram-bot-api.service" \
    /etc/systemd/system/telegram-bot-api.service

systemctl daemon-reload
systemctl enable --now telegram-bot-api.service

echo "Local Telegram Bot API service installed."
echo "The Python bot has not been switched yet."
