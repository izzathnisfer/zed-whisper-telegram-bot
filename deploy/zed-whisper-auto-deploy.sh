#!/usr/bin/env bash
set -Eeuo pipefail

REPO="/home/ubuntu/zed-whisper-telegram-bot"
SERVICE="zed-whisper-bot.service"
PROD_BRANCH="master"
READY_REF="refs/heads/deploy-ready"
VENV_ROOT="/home/ubuntu/zed-whisper-venvs"
STATE_DIR="/home/ubuntu/.local/state/zed-whisper-deploy"
LOCK_FILE="/home/ubuntu/.cache/zed-whisper-deploy.lock"

mkdir -p "$VENV_ROOT" "$STATE_DIR" "$(dirname "$LOCK_FILE")"
exec 9>"$LOCK_FILE"
flock -n 9 || exit 0

cd "$REPO"
target="$(git ls-remote origin "$READY_REF" | awk 'NR == 1 {print $1}')"
[[ -n "$target" ]] || exit 0

current="$(git rev-parse HEAD)"
[[ "$target" != "$current" ]] || exit 0

if [[ -n "$(git status --porcelain --untracked-files=no)" ]]; then
  echo "Refusing deployment: production checkout has tracked local changes." >&2
  exit 1
fi

git fetch --quiet origin "$PROD_BRANCH" "$READY_REF"
git merge-base --is-ancestor "$target" "origin/$PROD_BRANCH" || {
  echo "Refusing deployment: deploy-ready commit is not on $PROD_BRANCH." >&2
  exit 1
}

old_sha="$current"
old_venv="$(readlink -f "$REPO/venv")"
target_venv="$VENV_ROOT/$target"
changed=0

rollback() {
  local rc="$1"
  if [[ "$changed" -eq 1 ]]; then
    echo "Deployment failed; rolling back to $old_sha" >&2
    git reset --hard "$old_sha" || true
    ln -sfn "$old_venv" "$REPO/.venv-next" || true
    mv -Tf "$REPO/.venv-next" "$REPO/venv" || true
    sudo -n /usr/bin/systemctl restart "$SERVICE" || true
  fi
  exit "$rc"
}
trap 'rollback $?' ERR

echo "Preparing production commit $target"
git reset --hard "$target"
changed=1

if [[ ! -x "$target_venv/bin/python" ]]; then
  python3 -m venv "$target_venv"
  "$target_venv/bin/python" -m pip install --disable-pip-version-check -q --upgrade pip
  "$target_venv/bin/python" -m pip install --disable-pip-version-check -q -r requirements.txt
fi

"$target_venv/bin/python" -m compileall -q bot.py usage_tracker.py
"$target_venv/bin/python" -m unittest discover -v

restarts_before="$(systemctl show "$SERVICE" -p NRestarts --value)"
ln -sfn "$target_venv" "$REPO/.venv-next"
mv -Tf "$REPO/.venv-next" "$REPO/venv"
sudo -n /usr/bin/systemctl restart "$SERVICE"
sleep 8
systemctl is-active --quiet "$SERVICE"
restarts_after="$(systemctl show "$SERVICE" -p NRestarts --value)"

if (( restarts_after > restarts_before )); then
  echo "Health check failed: service restarted unexpectedly." >&2
  false
fi

printf '%s\n' "$target" > "$STATE_DIR/deployed.sha"
trap - ERR
echo "Deployment successful: $target"
