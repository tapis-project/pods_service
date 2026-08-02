#!/usr/bin/env bash
# GATE 2 — cluster-free unit tests. ONLY the stdlib-only suites that import with
# no Tapis config, no DB, no minikube: the pure telemetry-utils parser and the
# whole agent test set (agent/ is stdlib-only). These are the suites whose
# imports were verified bare (node_telemetry_utils + pods_agent).
#
# The heavy in-cluster suites (make test / test_pods / volumes / templates) need
# postgres+rabbit+NFS+k8s and are NOT run here — they are a nightly/manual job
# against a dev cluster. To expand cluster-free coverage, run the config-needing
# pure suites INSIDE the built image instead (ci/README.md).
set -uo pipefail
cd "$(dirname "$0")/.."
source ci/lib.sh

PURE_TESTS=(
  tests/test_node_telemetry_utils.py
  tests/test_agent_addresses.py
  tests/test_agent_bench.py
  tests/test_agent_decommission.py
  tests/test_agent_k8s.py
  tests/test_agent_shell_rotate.py
  tests/test_agent_telemetry.py
  tests/test_agent_update.py
  tests/test_agent_watch.py
)

section "cluster-free unit tests"
# Get pytest with the LEAST intrusion, in this order:
#   1) ambient pytest — your shell already has it; nothing is created.
#   2) nix present — an ephemeral `nix-shell` provides pytest from the store: NO
#      venv, no pip, no network, nothing to create or clean up (safest on nix).
#   3) fallback (plain CI runner, no nix) — a throwaway pip venv in /tmp, loudly
#      announced and trap-removed on exit.
# NOTE (2): uses `nix-shell -p` (channels/NIX_PATH form). If your nix is
# flakes-only with no <nixpkgs>, we switch this to `nix shell nixpkgs#...` — a
# one-line change; we'll confirm which at demo time.
rc=0
note "python: $(python3 --version 2>&1)"
if python3 -c "import pytest" 2>/dev/null; then
  note "using ambient pytest (nothing created) — $(python3 -m pytest --version 2>&1 | head -1)"
  python3 -m pytest "${PURE_TESTS[@]}" -q --no-header; rc=$?
elif command -v nix >/dev/null 2>&1; then
  note "no ambient pytest — using an ephemeral \`nix shell\` (flakes) for pytest (no venv, nothing to clean up)"
  nix shell nixpkgs#python3Packages.pytest --command pytest "${PURE_TESTS[@]}" -q --no-header; rc=$?
else
  # Last resort: a pip venv. /tmp only (never the repo), trap-removed on exit so
  # it's TRULY thrown away. Removes ONLY the dir mktemp just made (guarded on
  # non-empty + exists). It never activates/deactivates anything and runs in this
  # subprocess, so a user's own .venv is never touched or "exited".
  VENV_ROOT="$(mktemp -d)"      # a fresh, unique temp dir — NEVER an existing venv
  trap '[ -n "$VENV_ROOT" ] && [ -d "$VENV_ROOT" ] && { rm -rf "$VENV_ROOT"; printf "  \033[2m· venv REMOVED: %s (thrown away)\033[0m\n" "$VENV_ROOT"; }' EXIT
  VENV="$VENV_ROOT/ci-venv"
  note "venv CREATED: $VENV_ROOT  (no ambient pytest and no nix; in /tmp, never the repo; auto-removed on exit)"
  # stderr intentionally NOT swallowed — when this path fails (as the first live
  # Actions run may have), the actual venv/pip error must reach the log.
  if python3 -m venv "$VENV" && "$VENV/bin/pip" install --quiet pytest; then
    note "$("$VENV/bin/python" -m pytest --version 2>&1 | head -1)"
    "$VENV/bin/python" -m pytest "${PURE_TESTS[@]}" -q --no-header; rc=$?
  else
    fail "could not obtain pytest (venv/pip error above; need pytest on PATH, nix-shell, or python3 -m venv)"; finish
  fi
fi

[ "$rc" -eq 0 ] && ok "${#PURE_TESTS[@]} pure suites passed" || fail "unit test failure above"
finish
