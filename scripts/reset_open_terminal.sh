#!/usr/bin/env bash
# Recreate only the terminal using installed images; never pull or rebuild.
set -euo pipefail
cd /opt/ai-stack
exec 9>/run/lock/ai-stack-install.lock
flock -n 9 || { echo 'Stack maintenance is running; terminal reset skipped.'; exit 0; }
compose() {
  env -i PATH="$PATH" /usr/bin/docker compose --project-name ai-stack \
    --env-file /opt/ai-stack/.env -f docker-compose.yml -f compose.image-lock.json "$@"
}
# An intentionally stopped stack must stay stopped.
[[ -n "$(compose ps --status running -q open-terminal)" ]] || exit 0
compose stop open-terminal
compose rm -f open-terminal
# The disposable container confines deletion to the terminal mount. Root also
# removes files created by package tools; symlinks are unlinked, never followed.
compose run --rm -T --no-deps --pull never --user 0 --entrypoint python open-terminal - <<'PYTHON'
import os
from pathlib import Path
import shutil

workspace = Path('/home/user')
if workspace.is_symlink() or not workspace.is_dir() or not os.path.ismount(workspace):
    raise RuntimeError('Expected /home/user to be the terminal workspace mount')
for entry in workspace.iterdir():
    if entry.is_symlink() or not entry.is_dir():
        entry.unlink()
    else:
        shutil.rmtree(entry)
os.chown(workspace, 1000, 1000)
os.chmod(workspace, 0o750)
PYTHON
compose up -d --no-deps --no-build --pull never --force-recreate --wait --wait-timeout 180 open-terminal
