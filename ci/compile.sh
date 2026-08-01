#!/usr/bin/env bash
# GATE 1 — syntax. Byte-compiles every tracked .py without importing (no deps,
# no config, no cluster). Catches syntax errors and, cheaply, the whole file set
# before anything slower runs. Does NOT catch import-time errors (that needs the
# image — see ci/README.md "deeper gate").
set -uo pipefail
cd "$(dirname "$0")/.."
source ci/lib.sh

section "py_compile (syntax sweep)"
mapfile -t files < <(git ls-files '*.py' 2>/dev/null || find . -name '*.py' -not -path './.git/*')
note "${#files[@]} python files"
if python3 -m py_compile "${files[@]}"; then
  ok "all files compile"
else
  fail "syntax error(s) above"
fi
finish
