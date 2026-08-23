#!/bin/bash
set -euo pipefail

DATA_DIRECTORY=/var/lib/telegram-bot-api

fail() {
    echo "Cache deletion request rejected." >&2
    exit 65
}

# The sensitive path is accepted only on stdin so it never appears in sudo's
# command log or the process list.
IFS= read -r requested_path || fail
if IFS= read -r _extra_line; then
    fail
fi

[ -n "$requested_path" ] || fail
[ "${#requested_path}" -le 4096 ] || fail
case "$requested_path" in
    /*) ;;
    *) fail ;;
esac

# A concurrent cleanup of the exact same Telegram file is already success.
[ -e "$requested_path" ] || exit 0

data_root=$(realpath -e -- "$DATA_DIRECTORY")
resolved_path=$(realpath -e -- "$requested_path")
case "$resolved_path" in
    "$data_root"/*) ;;
    *) fail ;;
esac

[ -f "$resolved_path" ] || fail
filename=${resolved_path##*/}
case "${filename,,}" in
    *.binlog|*.db|*.sqlite|*.sqlite3|*.sqlite-wal|*.sqlite-shm)
        fail
        ;;
esac

unlink -- "$resolved_path"
