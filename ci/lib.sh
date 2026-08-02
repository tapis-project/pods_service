#!/usr/bin/env bash
# Shared CI helpers. Sourced by the ci/*.sh gates. Portable: no GitHub/Gitea
# specifics here — these run identically on GHA, Gitea act_runner, or locally.
set -uo pipefail

# A gate accumulates failures and reports them all at the end (so one run shows
# every problem), but the WATERFALL between gates is fail-fast: ci/all.sh stops
# at the first gate that exits non-zero.
_FAILED=0

section() { printf '\n\033[1m== %s ==\033[0m\n' "$*"; }
ok()      { printf '  \033[32m✓\033[0m %s\n' "$*"; }
fail()    { printf '  \033[31m✗ FAIL\033[0m %s\n' "$*"; _FAILED=1; }
note()    { printf '  \033[33m·\033[0m %s\n' "$*"; }

# run "<label>" <cmd...> — runs cmd, marks fail on non-zero, keeps going.
run() {
  local label="$1"; shift
  if "$@"; then ok "$label"; else fail "$label"; fi
}

finish() {
  if [ "$_FAILED" -ne 0 ]; then
    printf '\n\033[31mGATE FAILED\033[0m\n'; exit 1
  fi
  printf '\n\033[32mGATE PASSED\033[0m\n'
}

have() { command -v "$1" >/dev/null 2>&1; }
