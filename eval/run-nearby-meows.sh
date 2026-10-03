#!/usr/bin/env bash
# Rerun the Nearby Meows eval cases: each exports KTCatPokemon at the case's `base`
# commit, applies its patch, builds, audits the Dex screen, and scores the findings.
#
#   eval/run-nearby-meows.sh [path/to/KTCatPokemon] [simulator-udid]
#
# Needs an iOS 26.5+ simulator (the app's deployment target). Run from anywhere.
set -euo pipefail

here="$(cd "$(dirname "$0")/.." && pwd)"
target="${1:-$here/../KTCatPokemon}"
udid="${2:-${SIGHTLINE_UDID:-27249C87-F693-4DFE-BE82-31E450E4133A}}"

cd "$here"
exec uv run sightline eval \
  --corpus eval/nearby-meows \
  --target-repo "$target" \
  --udid "$udid" \
  --workdir .sightline/run/eval-nearby-meows
