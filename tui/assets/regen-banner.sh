#!/usr/bin/env bash
# Regenerate the Zinthos ASCII banner that's hard-coded in src/main.rs (const BANNER).
# Requires `figlet` (provided by the `tui` devShell in flake.nix).
#
#   nix develop .#tui -c ./tui/assets/regen-banner.sh
#
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
figlet -f "Delta Corps Priest 1" -d "$here" -w 200 "${1:-Zinthos}"
