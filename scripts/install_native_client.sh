#!/usr/bin/env bash
set -euo pipefail
source "$(dirname -- "${BASH_SOURCE[0]}")/python_env.sh"
# Preserve the original optional positional serial while sharing the Python installer.
if [[ $# == 1 && "$1" != -* ]]; then
  set -- --serial "$1"
fi
exec "$REALMAN_INTERPRETER" -m teleop install-quest "$@"
