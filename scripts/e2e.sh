#!/usr/bin/env bash
# Start a throwaway GitLab CE container, run the e2e tests against it, and tear it down.
#
#   scripts/e2e.sh                  # full run, container removed afterwards
#   KEEP_GITLAB=1 scripts/e2e.sh    # leave GitLab running so the next run skips the ~4 min boot
#   scripts/e2e.sh -k merge_request # extra args go to pytest
set -euo pipefail
cd "$(dirname "$0")/.."

compose=(docker compose -f compose.e2e.yml)

cleanup() {
  if [[ "${KEEP_GITLAB:-0}" == "1" ]]; then
    echo "Leaving GitLab running. Stop it with: ${compose[*]} down -v"
  else
    "${compose[@]}" down -v
  fi
}
trap cleanup EXIT

echo "Starting GitLab (first boot takes a few minutes)..."
if ! "${compose[@]}" up -d --wait --wait-timeout 900; then
  "${compose[@]}" logs --tail 100 gitlab
  exit 1
fi

uv run pytest -m e2e tests/e2e --no-cov "$@"
