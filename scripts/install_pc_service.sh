#!/usr/bin/env bash
set -euo pipefail
root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
if [[ $# != 1 || "$1" == --help || "$1" == -h ]]; then
  echo 'Usage: bash scripts/install_pc_service.sh /path/to/XRoboToolkit_PC_Service_1.0.0_ubuntu_22.04_amd64.deb'
  exit 0
fi
deb="$(realpath -- "$1")"
expected=61961067eb4b41f81ed7cae35f4690dbb0ddfefb329a12b24e0b90ebc46ada91
actual="$(sha256sum -- "$deb")"
[[ "${actual%% *}" == "$expected" ]] || { echo 'Unexpected DEB SHA256; use the pinned official Ubuntu 22.04 release.' >&2; exit 1; }
mkdir -p "$root/.native/pc-service"
dpkg-deb --extract "$deb" "$root/.native/pc-service"
echo "Extracted to $root/.native/pc-service; no service or robot was started."
