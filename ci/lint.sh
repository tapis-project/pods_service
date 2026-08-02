#!/usr/bin/env bash
# T1 lint/format gate — ruff (formatter + linter in one binary; black-compatible
# format + flake8/isort/pyupgrade lints). Config in ruff.toml at the repo root.
#
# OFF BY DEFAULT: prints a skip note unless RUFF=1, so it never blocks today. When
# the tree is clean and you're ready to enforce, flip RUFF=1 (locally, or in the
# workflow env) and it gates like the other checks. `make fmt` applies formatting;
# `make lint` runs this with RUFF=1.
set -uo pipefail
cd "$(dirname "$0")/.."
source ci/lib.sh

section "lint/format (ruff)"

if [ "${RUFF:-0}" != "1" ]; then
  note "ruff lint OFF — advisory tier, not gating (set RUFF=1 to enable)"
  finish
  exit 0
fi

if ! have ruff; then
  note "ruff not installed — skipping (pip install ruff, or nix run nixpkgs#ruff)"
  finish
  exit 0
fi

# Formatting must match (mechanical — safe to hard-gate first). Then the linters.
run "ruff format --check ." ruff format --check .
run "ruff check ." ruff check .
finish
