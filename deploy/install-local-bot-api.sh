#!/bin/bash
set -euo pipefail

PROJECT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
CREDENTIAL_FILE=/etc/zed-whisper-bot/telegram-bot-api.env
DATA_DIRECTORY=/var/lib/telegram-bot-api
BOT_USER=ubuntu
CACHE_DELETE_HELPER=/usr/local/libexec/zed-whisper-delete-cache
CACHE_DELETE_SUDOERS=/etc/sudoers.d/zed-whisper-cache-cleanup

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

for command in docker realpath setfacl systemctl visudo; do
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
    setfacl -m "u:${BOT_USER}:rwx,m::rwx,d:u:${BOT_USER}:rwx,d:m::rwx" {} +

# Telegram creates some cache children with mode 0750, which restricts an
# inherited named-user ACL to r-x. Install a root-owned, no-argument helper for
# deleting only a validated file inside the private Local Bot API directory.
visudo -cf "$PROJECT_DIR/deploy/zed-whisper-cache-cleanup.sudoers"
install -d -o root -g root -m 0755 /usr/local/libexec
install -o root -g root -m 0755 \
    "$PROJECT_DIR/deploy/delete-local-bot-api-cache.sh" \
    "$CACHE_DELETE_HELPER"
install -o root -g root -m 0440 \
    "$PROJECT_DIR/deploy/zed-whisper-cache-cleanup.sudoers" \
    "$CACHE_DELETE_SUDOERS"
visudo -cf "$CACHE_DELETE_SUDOERS"

install -o root -g root -m 0644 \
    "$PROJECT_DIR/deploy/telegram-bot-api.service" \
    /etc/systemd/system/telegram-bot-api.service

systemctl daemon-reload
systemctl enable --now telegram-bot-api.service

echo "Local Telegram Bot API service installed."
echo "The Python bot has not been switched yet."
