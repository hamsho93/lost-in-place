#!/usr/bin/env bash
# Check out the pinned PX4 commit in an existing PX4-Autopilot clone and apply our patches.
# Usage: px4/apply.sh <path-to-PX4-Autopilot>
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
px4="${1:?usage: $0 <path-to-PX4-Autopilot>}"
commit="$(cat "${here}/PX4_COMMIT")"

cd "${px4}"
if [[ -n "$(git status --porcelain --untracked-files=no)" ]]; then
  echo "error: ${px4} has uncommitted changes" >&2
  exit 1
fi
git fetch --quiet origin "${commit}" || true
git checkout --quiet "${commit}"
git submodule update --init --recursive --quiet
for patch in "${here}"/patches/*.patch; do
  echo "applying $(basename "${patch}")"
  git apply "${patch}"
done
