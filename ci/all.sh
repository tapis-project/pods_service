#!/usr/bin/env bash
# The failing waterfall. Runs the gates in cheapest→slowest order and STOPS at
# the first that fails (fail-fast): a syntax error never waits on semgrep. Each
# gate is also runnable alone (ci/compile.sh, ci/unit.sh, ci/security.sh).
#
# Runs identically on GitHub Actions, Gitea act_runner, or a dev shell:
#   bash ci/all.sh
set -uo pipefail
cd "$(dirname "$0")/.."

for gate in ci/compile.sh ci/unit.sh ci/security.sh; do
  printf '\n\033[1;34m### %s\033[0m\n' "$gate"
  bash "$gate" || { printf '\n\033[31mWaterfall halted at %s\033[0m\n' "$gate"; exit 1; }
done
printf '\n\033[1;32m### ALL GATES PASSED\033[0m\n'
