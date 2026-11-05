#!/usr/bin/env bash
set -euo pipefail
source "$(dirname -- "${BASH_SOURCE[0]}")/python_env.sh"
service_dir="$("$REALMAN_INTERPRETER" -m common.runtime_paths --service-dir)"
test -x "$service_dir/RoboticsServiceProcess" || { echo 'XRoboToolkit PC Service missing; see docs/native_runtime.md' >&2; exit 1; }
cd -- "$service_dir"
exec env -u PYTHONPATH LD_LIBRARY_PATH="$service_dir:$service_dir/lib:$service_dir/SDK/x64" \
  QT_PLUGIN_PATH="$service_dir/plugins" QT_QPA_PLATFORM=offscreen ./RoboticsServiceProcess
