#!/usr/bin/env bash
set -euo pipefail
source "$(dirname -- "${BASH_SOURCE[0]}")/python_env.sh"
exec "$REALMAN_INTERPRETER" -m teleop "$@"
